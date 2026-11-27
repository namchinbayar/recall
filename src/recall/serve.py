"""`recall serve`: answer "what do I know about this?" over a local socket.

Some programs want recall on every turn, not as a tool the model may or may
not call: Hermes's memory provider asks before each reply. Starting recall for
each question costs a Proton Pass secret lookup and a few seconds, and handing those
programs the database keys would spread secrets around. So one long-running
process holds the connections and listens on a Unix socket that only this
user can open ($XDG_RUNTIME_DIR/recall.sock).

One JSON object per line in, one per line out:

    {"op": "ping"}
    {"op": "context", "query": "...", "limit": 4, "min_score": 0.55,
     "max_chars": 400, "include_events": true, "session_id": "...",
     "exclude_tags": ["hermes-memory", "desktop-notification", "digest"]}

`context` runs the normal hybrid search with a small rerank pool, keeps only
hits the reranker scores at or above `min_score`, and returns them as short
excerpts: {"ok": true, "count": 2, "text": "...", "scores": [0.91, 0.74]};
count 0 means nothing relevant enough, which is the common and correct answer
for small talk. With `include_events`, past Hermes exchanges can come back
too; `exclude_tags` keeps whole kinds of material (notifications, say) out of
the search, and `session_id` leaves out exchanges from the conversation that
is asking, which it already has in front of it. The encrypted vault is never
searched.

    {"op": "store", "message": "...", "response": "...", "session_id": "...", "platform": "cli"}

`store` saves one finished exchange as an event in this server's own database,
without Kafka. It is refused unless RECALL_SERVE_STORE is set: the main
instance gets its Hermes exchanges from the gateway hook and the stream, so
only an instance with no stream (the private one) turns this on.

Two more fields make `context` behave like memory in a long conversation:
`seen` lists the ids of items this conversation was already given (each reply
carries its items' `ids`), so the same excerpt is not handed over on every
turn; and `previous` is the user's message before this one, which is searched
together with a short follow-up ("and the other one?") that says nothing alone.

    {"op": "turn", "message": "...", "response": "...", "session_id": "...", "platform": "cli"}
    {"op": "profile"}
    {"op": "status"}

`turn` keeps one finished exchange, whole, for `recall learn` (learn.py); it is
not searched and costs no embedding. `profile` returns the standing profile
that `recall learn` maintains, and `status` a few counts for a status line.

`context` replies also carry `items`, the same hits as structured objects
({label, date, source, text, score}) for callers that render them rather than
paste them into a prompt (the private homepage's detail drawers).

    {"op": "open_items", "days": 14, "limit": 30}

`open_items` reads the session notes of the last `days` days (default 14, at
most 90), newest first, and returns the bullets under each note's "## Still
open" heading, without the ones a later session settled (openitems.py):
{"ok": true, "notes": [{source, title, date, folder, kind, items: [...],
ids: [...]}]}. Notes with nothing open are left out. It only reads.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socketserver
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import openitems, search
from .openitems import rejoin, still_open  # noqa: F401  (their old home; other modules and tests reach them here)
from .agent.tools import excerpt
from .config import Settings
from .stream import spool

MAX_QUERY = 1_000
MAX_STORE = 6_000    # characters kept from each side of a stored exchange
POOL = 12            # candidates sent to the reranker: a third of a normal search, a third of the tokens
LIMITS = {"limit": (1, 10, 4), "max_chars": (100, 2_000, 400)}   # (min, max, default)
MIN_SCORE = 0.55   # measured: relevant prompts score 0.6+ at the top, unrelated ones 0.3 to 0.45
FOLLOW_UP_WORDS = 8  # a message shorter than this is searched together with the one before it
SHORT_WORDS, SHORT_EXTRA = 4, 0.10   # a question this short says little, so a hit has to score this much more
MAX_SEEN = 400


def socket_path() -> Path:
    if path := os.environ.get("RECALL_SOCKET"):
        return Path(path)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(runtime) if runtime else spool.STATE_DIR) / "recall.sock"


def _clamp(req: dict, key: str) -> int:
    low, high, default = LIMITS[key]
    try:
        return min(max(int(req.get(key, default)), low), high)
    except (TypeError, ValueError):
        return default


def hit_id(h: search.Hit) -> str:
    return f"{h.source}#{h.chunk_index}"


def context(settings: Settings, req: dict, find: Callable[..., list[search.Hit]] | None = None) -> dict:
    empty = {"ok": True, "count": 0, "text": "", "scores": [], "ids": [], "items": []}
    query = " ".join(str(req.get("query") or "").split())[:MAX_QUERY]
    if not query:
        return empty
    previous = " ".join(str(req.get("previous") or "").split())[:300]
    if previous and previous != query and len(query.split()) < FOLLOW_UP_WORDS:
        query = f"{previous} {query}"[:MAX_QUERY]
    limit, max_chars = _clamp(req, "limit"), _clamp(req, "max_chars")
    try:
        min_score = float(req.get("min_score", MIN_SCORE))
    except (TypeError, ValueError):
        min_score = MIN_SCORE
    if len(query.split()) < SHORT_WORDS:        # "and why?" matches a little of everything
        min_score += SHORT_EXTRA
    skip = {str(t) for t in req.get("exclude_tags") or []}
    session = str(req.get("session_id") or "")
    seen = {str(i) for i in (req.get("seen") or [])[:MAX_SEEN]}
    hits = (find or search.search)(
        settings, query, limit=limit * 2 + min(len(seen), limit), include_events=bool(req.get("include_events")),
        pool=POOL, exclude_tags=sorted(skip), cap=2,
    )
    keep, per_source = [], {}
    for h in hits:                      # best first; at most two excerpts from one source
        if h.score < min_score or skip & set(h.tags) or per_source.get(h.source, 0) >= 2:
            continue
        if session and h.session == session:    # the asking conversation's own earlier turns
            continue
        if hit_id(h) in seen:                   # already in front of this conversation
            continue
        per_source[h.source] = per_source.get(h.source, 0) + 1
        keep.append(h)
        if len(keep) == limit:
            break
    lines, items = [], []
    for h in keep:
        day = f"{h.created_at.astimezone():%Y-%m-%d}" if h.created_at else ""
        text = h.text
        if "memory" in h.tags:
            label = "learned from a chat" if "learned" in h.tags else "memory"
        elif {"event", "hermes"} <= set(h.tags):
            label, text = "earlier Hermes chat", text.partition("\n")[2] or text    # drop the "[time] hermes / platform" line
        else:
            label = h.title
        when = f", {day}" if day and day not in label else ""
        clip = ' '.join(excerpt(text, query, max_chars).split())
        lines.append(f"- {label}{when}: {clip}")
        items.append({"label": label, "date": day or None, "source": h.source, "text": clip, "score": round(h.score, 3)})
    return {"ok": True, "count": len(keep), "text": "\n".join(lines), "scores": [round(h.score, 3) for h in keep],
            "ids": [hit_id(h) for h in keep], "items": items}


def turn(settings: Settings, req: dict, record: Callable[[dict], str] | None = None) -> dict:
    """Keep one finished exchange for `recall learn`. An exchange that looks like it holds a
    secret is not kept: the event stream's 500-character copy is all recall has of it."""
    from . import learn, sync

    doc = learn.turn_doc(str(req.get("session_id") or ""), str(req.get("message") or ""), str(req.get("response") or ""),
                         datetime.now(timezone.utc), str(req.get("platform") or "") or None, str(req.get("agent") or "") or None)
    if doc is None:
        return {"ok": True, "kept": "empty"}
    if sync.find_secret(f"{doc['message']}\n{doc['response']}"):
        return {"ok": True, "kept": "secret"}
    return {"ok": True, "kept": (record or learn.Store(settings).record_turn)(doc)}


_profile_cache: dict = {"at": 0.0, "value": None}


def profile(settings: Settings, load: Callable[[Settings], dict | None] | None = None) -> dict:
    """The standing profile, read from the database at most once a minute."""
    from . import learn

    now = time.monotonic()
    if load is not None or now - _profile_cache["at"] > 60:
        doc = (load or learn.profile)(settings)
        value = {"text": doc["text"], "written": doc["written"].isoformat()} if doc else {"text": ""}
        if load is not None:
            return {"ok": True, **value}
        _profile_cache.update(at=now, value=value)
    return {"ok": True, **_profile_cache["value"]}


def status(settings: Settings) -> dict:
    """Counts for a status line: what recall has learned and when it last looked."""
    from . import db

    chunks = db.chunks(settings)
    last = db.state(settings).find_one({"_id": {"$regex": "^session:"}}, sort=[("at", -1)])
    prof = db.state(settings).find_one({"_id": "profile"}) or {}
    return {
        "ok": True,
        "memories": chunks.count_documents({"tags": "memory"}),
        "learned": chunks.count_documents({"tags": {"$all": ["memory", "learned"]}}),
        "retired": db.retired(settings).estimated_document_count(),
        "unread_turns": db.turns(settings).count_documents({"learned": False}),
        "last_read": last["at"].isoformat() if last and last.get("at") else None,
        "profile_written": prof["written"].isoformat() if prof.get("written") else None,
    }


OPEN_DAYS = (1, 90, 14)      # (min, max, default) for open_items
OPEN_LIMIT = (1, 100, 30)
_TITLE_DAY = re.compile(r"\s*\(\d{4}-\d{2}-\d{2}\)$")


def _bounded(req: dict, key: str, bounds: tuple[int, int, int]) -> int:
    low, high, default = bounds
    try:
        return min(max(int(req.get(key, default)), low), high)
    except (TypeError, ValueError):
        return default


def open_items(settings: Settings, req: dict, now: datetime | None = None,
               load: Callable[[Settings, datetime], list[dict]] | None = None,
               closed: Callable[[Settings, list[str]], set[str]] | None = None) -> dict:
    """What recent session notes left open and no later session settled (openitems.py). Read
    only; vault entries are never session notes. `ids` runs parallel to `items`."""
    from datetime import timedelta

    days, limit = _bounded(req, "days", OPEN_DAYS), _bounded(req, "limit", OPEN_LIMIT)
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    sessions = openitems.read_notes((load or _note_chunks)(settings, since))
    ids = [i.id for s in sessions for i in s.items]
    gone = (closed or openitems.closed_ids)(settings, ids) if ids else set()
    notes = []
    for s in sessions:
        left = [i for i in s.items if i.id not in gone]
        if not left:
            continue
        notes.append({
            "source": s.source,
            "title": _TITLE_DAY.sub("", s.title.removeprefix("Session: ")).strip() or s.source,
            "date": s.when.isoformat() if s.when else None,
            "folder": s.folder,
            "kind": s.kind,
            "items": [i.text for i in left],
            "ids": [i.id for i in left],
        })
    notes.sort(key=lambda n: n["date"] or "", reverse=True)
    return {"ok": True, "days": days, "notes": notes[:limit]}


def catch_up(settings: Settings, days: int = 7, now: datetime | None = None, with_profile: bool = True,
             load: Callable[[Settings, datetime], list[dict]] | None = None,
             load_profile: Callable[[Settings], dict | None] | None = None,
             closed: Callable[[Settings, list[str]], set[str]] | None = None) -> str:
    """Plain text for an assistant picking up where the user's others left off: the profile,
    then every session note of the last `days` days (Claude Code and Hermes alike),
    newest first, each with what it left open."""
    from datetime import timedelta

    from . import learn

    days = min(max(int(days), 1), 60)
    now = now or datetime.now(timezone.utc)
    rows = (load or _note_chunks)(settings, now - timedelta(days=days))
    opened = {n["source"]: n for n in open_items(settings, {"days": days, "limit": 500}, now, lambda s, d: rows, closed)["notes"]}
    firsts: dict[str, dict] = {}
    for r in rows:
        if r["source"] not in firsts or r.get("chunk_index", 0) < firsts[r["source"]].get("chunk_index", 0):
            firsts[r["source"]] = r
    out = []
    if with_profile:
        doc = (load_profile or learn.profile)(settings)
        if doc:
            out.append(f"# Who the user is (profile written {doc['written']:%Y-%m-%d})\n{doc['text'].strip()}")
    lines = []
    for r in sorted(firsts.values(), key=lambda r: r.get("created_at") or now, reverse=True):
        made = r.get("created_at")
        if isinstance(made, datetime) and made.tzinfo is None:
            made = made.replace(tzinfo=timezone.utc)
        day = f"{made.astimezone():%Y-%m-%d}" if isinstance(made, datetime) else "?"
        title = _TITLE_DAY.sub("", (r.get("title") or "").removeprefix("Session: ")).strip() or r["source"]
        where = "Hermes" if "hermes" in (r.get("tags") or []) else (
            ((r.get("session") or {}).get("cwd") or "").replace(str(Path.home()), "~") or "Claude Code")
        lines.append(f"- {day}  {title}  ({where}; {r['source']})")
        note = opened.get(r["source"]) or {}
        for item, item_id in list(zip(note.get("items", []), note.get("ids", [])))[:4]:
            lines.append(f"    open: {item}  [{item_id}]")
    head = f"# Sessions in the last {days} day{'s' if days != 1 else ''}, newest first"
    out.append(f"{head}\n" + ("\n".join(lines) if lines else "none"))
    out.append("Search recall with a session's source id, or its title words, for the whole note. "
               "An open item that is done or dropped comes off the list by its id (close_open_item, or `recall open --done`).")
    return "\n\n".join(out)


def _note_chunks(settings: Settings, since: datetime) -> list[dict]:
    from . import db

    return list(db.chunks(settings).find(
        {"tags": "session-note", "created_at": {"$gte": since}},
        {"_id": 0, "source": 1, "title": 1, "chunk_index": 1, "text": 1, "created_at": 1, "tags": 1, "session": 1},
    ))


def store_enabled() -> bool:
    return os.environ.get("RECALL_SERVE_STORE", "").strip().lower() in ("1", "true", "yes", "on")


def exchange_event(req: dict, now_ms: int | None = None) -> dict | None:
    """The request as a stream event (the shape the gateway hook spools), or None when it is empty."""
    message = str(req.get("message") or "").strip()[:MAX_STORE]
    response = str(req.get("response") or "").strip()[:MAX_STORE]
    if not (message or response):
        return None
    now_ms = now_ms or int(time.time() * 1000)
    session_id = str(req.get("session_id") or "")
    platform = str(req.get("platform") or "") or None
    return {
        "event_id": hashlib.sha256(f"hermes\0{session_id}\0{message}\0{now_ms}".encode()).hexdigest()[:32],
        "source": "hermes",
        "kind": platform,
        "actor": None,
        "occurred_at": now_ms,
        "body": f"Me: {message}\nHermes: {response}",
        "meta": json.dumps({"session_id": session_id, "platform": platform}),
    }


def store(settings: Settings, req: dict, save: Callable[[Settings, list[dict]], int] | None = None) -> dict:
    if not store_enabled():
        return {"ok": False, "error": "store is off for this instance (RECALL_SERVE_STORE is not set)"}
    event = exchange_event(req)
    if event is None:
        return {"ok": True, "stored": 0}
    if save is None:
        from .stream import sink     # pulls in the Kafka client; only an instance that stores needs it
        save = sink.store
    return {"ok": True, "stored": save(settings, [event])}


def answer(
    settings: Settings, line: bytes | str, find: Callable[..., list[search.Hit]] | None = None,
    save: Callable[[Settings, list[dict]], int] | None = None,
) -> dict:
    """One request line to one response. Never raises: a bad request is an answer too."""
    try:
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError("expected a JSON object")
    except ValueError as exc:
        return {"ok": False, "error": f"bad request: {exc}"}
    try:
        if req.get("op") == "ping":
            return {"ok": True}
        if req.get("op") == "context":
            return context(settings, req, find)
        if req.get("op") == "store":
            return store(settings, req, save)
        if req.get("op") == "turn":
            return turn(settings, req)
        if req.get("op") == "profile":
            return profile(settings)
        if req.get("op") == "status":
            return status(settings)
        if req.get("op") == "open_items":
            return open_items(settings, req)
        return {"ok": False, "error": f"unknown op {req.get('op')!r}"}
    except Exception as exc:  # a database or Voyage error must not take the server down
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def run(settings: Settings, path: Path | None, echo: Callable[[str], None]) -> None:
    path = path or socket_path()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            for line in self.rfile:
                if line.strip():
                    self.wfile.write(json.dumps(answer(settings, line)).encode() + b"\n")
                    self.wfile.flush()

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)            # left behind by a crash; a second live server would fail on bind anyway
    old = os.umask(0o177)                   # the socket is born private: only this user may connect
    try:
        server = Server(str(path), Handler)
    finally:
        os.umask(old)
    echo(f"listening on {path}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        path.unlink(missing_ok=True)
