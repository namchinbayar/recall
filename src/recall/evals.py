"""`recall eval`: does search still find what it should?

A question set (~/.config/recall/eval.toml) holds questions someone would really ask, each with
the source that answers it. `recall eval` puts every question through the same search the
assistants use and reports how often the answer came first, how often it was among the results,
and which questions missed. Without this a change to chunking, ranking or the recency bonus is a
guess; with a saved baseline it is a before and an after.

    [[case]]
    q = "where do the services get their secrets"
    expect = "file:~/notes/secrets.md"          # one source, or a list: any of them counts
    avoid = "file:~/notes/secrets-old.md"       # a replaced note that must not rank above it

    [[case]]
    q = "best sourdough hydration"
    expect = []                                 # nothing stored answers this: stay quiet

`expect` and `avoid` are source ids as `recall list` prints them; `*` matches any run of
characters and `file:~/` is the home folder. A question whose expected source is not stored
(removed, renamed) is reported apart from the misses: search cannot find what is not there.

The question set is personal, so it lives outside the repository; see contrib/eval.example.toml.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

from . import db
from .config import Settings
from .search import Hit
from .serve import MIN_SCORE
from .stream import spool

CONFIG = Path(os.environ.get("RECALL_EVAL", "~/.config/recall/eval.toml")).expanduser()
BASELINE = spool.STATE_DIR / "eval.json"
QUIET_BELOW = MIN_SCORE     # under this the Hermes provider hands nothing over: that is "quiet"
KEYS = {"q", "expect", "avoid", "tags", "events", "note"}


@dataclass(frozen=True)
class Case:
    q: str
    expect: tuple[str, ...]             # empty: nothing should match
    avoid: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    events: bool = False
    note: str = ""


@dataclass
class Outcome:
    case: Case
    kind: str                   # found | missed | absent | quiet | noisy
    rank: int | None = None     # place of the first expected source, from 1
    stale: bool = False         # an `avoid` source ranked above the answer, or in its place
    hits: tuple[Hit, ...] = ()

    @property
    def top(self) -> float:
        return self.hits[0].score if self.hits else 0.0


def _pattern(text: str) -> str:
    return "file:" + str(Path.home()) + text[6:] if text.startswith("file:~/") else text


def _list(value, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise SystemExit(f"{where} must be a source id or a list of them")
    return tuple(value)


def load(path: Path | None = None) -> list[Case]:
    path = path or CONFIG
    if not path.is_file():
        raise SystemExit(f"no question set at {path}; see contrib/eval.example.toml")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{path}: {exc}") from None
    cases, seen = [], set()
    for i, c in enumerate(data.get("case", []), 1):
        where = f"{path}: case #{i}"
        if not isinstance(c.get("q"), str) or not c["q"].strip() or "expect" not in c or set(c) - KEYS:
            unknown = sorted(set(c) - KEYS)
            raise SystemExit(f"{where} needs `q` and `expect`" + (f"; unknown keys: {unknown}" if unknown else ""))
        q = c["q"].strip()
        if q in seen:
            raise SystemExit(f"{where} repeats the question {q!r}")
        seen.add(q)
        cases.append(Case(
            q=q,
            expect=tuple(_pattern(p) for p in _list(c["expect"], f"{where}: expect")),
            avoid=tuple(_pattern(p) for p in _list(c.get("avoid", []), f"{where}: avoid")),
            tags=_list(c.get("tags", []), f"{where}: tags"),
            events=bool(c.get("events", False)),
            note=str(c.get("note", "")),
        ))
    if not cases:
        raise SystemExit(f"{path} has no [[case]] entries")
    return cases


def stored_sources(settings: Settings) -> set[str]:
    """Every source search could return (the vault is never one)."""
    found: set[str] = set()
    for coll in db.search_collections(settings):
        found.update(coll.distinct("source", {"tags": {"$nin": ["vault"]}}))
    return found


def _matches(source: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(source, p) for p in patterns)


def judge(case: Case, hits: list[Hit], stored: set[str] | None = None, quiet_below: float = QUIET_BELOW) -> Outcome:
    """What one question's results amount to. `stored` (when given) tells a miss from a question
    whose answer is no longer in recall."""
    hits = tuple(hits)
    if not case.expect:
        top = hits[0].score if hits else 0.0
        return Outcome(case, "quiet" if top < quiet_below else "noisy", hits=hits)
    if stored is not None and not any(_matches(s, case.expect) for s in stored):
        return Outcome(case, "absent", hits=hits)
    rank = next((i for i, h in enumerate(hits, 1) if _matches(h.source, case.expect)), None)
    above = hits[: rank - 1] if rank else hits
    stale = any(_matches(h.source, case.avoid) for h in above)
    return Outcome(case, "found" if rank else "missed", rank, stale, hits)


def run(cases: list[Case], search_fn: Callable[[Case], list[Hit]], stored: set[str] | None = None,
        quiet_below: float = QUIET_BELOW, progress: Callable[[int], None] | None = None) -> list[Outcome]:
    out = []
    for i, case in enumerate(cases, 1):
        out.append(judge(case, search_fn(case), stored, quiet_below))
        if progress:
            progress(i)
    return out


@dataclass(frozen=True)
class Summary:
    asked: int          # questions with an answer that is stored
    first: int          # ... answered by the first result
    found: int          # ... answered among the results
    mrr: float          # mean of 1/rank (0 for a miss)
    stale: int          # ... where a replaced note ranked above the answer
    quiet: int          # questions that should find nothing, and did
    silent: int         # questions that should find nothing
    absent: int         # questions whose expected source is not stored

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def summarize(outcomes: list[Outcome]) -> Summary:
    answered = [o for o in outcomes if o.kind in ("found", "missed")]
    silent = [o for o in outcomes if o.kind in ("quiet", "noisy")]
    return Summary(
        asked=len(answered),
        first=sum(o.rank == 1 for o in answered),
        found=sum(o.rank is not None for o in answered),
        mrr=round(sum(1 / o.rank for o in answered if o.rank) / len(answered), 3) if answered else 0.0,
        stale=sum(o.stale for o in answered),
        quiet=sum(o.kind == "quiet" for o in silent),
        silent=len(silent),
        absent=sum(o.kind == "absent" for o in outcomes),
    )


# ----- the baseline: one saved run to compare the next one with ------------------

def _score(o: Outcome) -> int:
    """One number per question, higher is better: 0 for a miss or noise, else counting down from the top."""
    if o.kind in ("quiet", "noisy"):
        return int(o.kind == "quiet")
    return 0 if o.rank is None else 1000 - o.rank


def snapshot(outcomes: list[Outcome], how: dict, now: datetime | None = None) -> dict:
    return {
        "at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "how": how,
        "summary": summarize(outcomes).as_dict(),
        "cases": {o.case.q: {"kind": o.kind, "rank": o.rank, "score": _score(o)} for o in outcomes if o.kind != "absent"},
    }


def load_baseline(path: Path | None = None) -> dict | None:
    try:
        return json.loads((path or BASELINE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_baseline(snap: dict, path: Path | None = None) -> None:
    path = path or BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=1))
    tmp.replace(path)


def compare(outcomes: list[Outcome], baseline: dict) -> tuple[list[Outcome], list[Outcome]]:
    """(better, worse) than the baseline, over the questions both runs asked."""
    better, worse = [], []
    for o in outcomes:
        before = baseline.get("cases", {}).get(o.case.q)
        if before is None or o.kind == "absent":
            continue
        if _score(o) > before["score"]:
            better.append(o)
        elif _score(o) < before["score"]:
            worse.append(o)
    return better, worse


# ----- the report -----------------------------------------------------------------

def _place(rank: int | None) -> str:
    return "missed" if rank is None else f"#{rank}"


def _got(o: Outcome, n: int = 3, indent: str = "      ") -> list[str]:
    """The first results, one per line with the source id (what `expect` and `avoid` take)."""
    if not o.hits:
        return [f"{indent}got    no results"]
    return [f"{indent}{'got   ' if i == 1 else '      '} {i}. {h.title[:64]} ({h.score:.2f})  {h.source}"
            for i, h in enumerate(o.hits[:n], 1)]


def _share(part: int, whole: int) -> str:
    return f"{part} of {whole}" + (f"  ({part / whole:.0%})" if whole else "")


def report(outcomes: list[Outcome], limit: int, baseline: dict | None = None, verbose: bool = False) -> list[str]:
    s = summarize(outcomes)
    lines = [
        f"answer first          {_share(s.first, s.asked)}",
        f"answer in the top {limit:<3} {_share(s.found, s.asked)}",
        f"mean reciprocal rank  {s.mrr:.3f}",
    ]
    if s.silent:
        lines.append(f"quiet when it should  {_share(s.quiet, s.silent)}")
    if any(o.case.avoid for o in outcomes):
        lines.append(f"replaced note on top  {s.stale}")

    def block(title: str, rows: list[Outcome], line: Callable[[Outcome], list[str]]) -> None:
        if rows:
            lines.extend(["", f"{title} ({len(rows)})"])
            for o in rows:
                lines.extend(line(o))

    block("missed", [o for o in outcomes if o.kind == "missed"],
          lambda o: [f"  {o.case.q}", f"      wanted {', '.join(o.case.expect)}", *_got(o)])
    block("answer not first", [o for o in outcomes if o.kind == "found" and o.rank != 1],
          lambda o: [f"  #{o.rank}  {o.case.q}", *_got(o, 1)])
    block("a replaced note ranked above the answer", [o for o in outcomes if o.stale],
          lambda o: [f"  {o.case.q}", *_got(o)])
    block("not quiet", [o for o in outcomes if o.kind == "noisy"],
          lambda o: [f"  {o.case.q}", *_got(o, 1)])
    block("expected source is not stored", [o for o in outcomes if o.kind == "absent"],
          lambda o: [f"  {o.case.q}", f"      wanted {', '.join(o.case.expect)}"])
    if verbose:
        block("every question", outcomes, lambda o: [f"  {o.kind:<7} {_place(o.rank) if o.case.expect else '':<7} {o.top:.2f}  {o.case.q}"])

    if baseline:
        before = baseline.get("summary", {})
        better, worse = compare(outcomes, baseline)
        lines.extend(["", f"against the baseline of {baseline.get('at', '?')[:10]}: "
                          f"first {before.get('first', '?')} → {s.first}, top {limit} {before.get('found', '?')} → {s.found}, "
                          f"mean reciprocal rank {before.get('mrr', '?')} → {s.mrr}"])
        if baseline.get("how") and baseline["how"].get("limit") != limit:
            lines.append(f"  (the baseline counted the top {baseline['how'].get('limit')})")
        for title, rows in (("better", better), ("worse", worse)):
            for o in rows:
                was = baseline["cases"][o.case.q]
                was_text = was["kind"] if was["kind"] in ("quiet", "noisy") else _place(was["rank"])
                now_text = o.kind if o.kind in ("quiet", "noisy") else _place(o.rank)
                lines.append(f"  {title:<6} {was_text} → {now_text}  {o.case.q}")
        if not better and not worse:
            lines.append("  no question moved")
    return lines
