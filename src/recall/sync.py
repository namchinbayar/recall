"""Keep chosen files and folders in step with recall.

`recall add` saves a file once; when the file changes, recall keeps answering
from the old copy. `recall sync` reads ~/.config/recall/sync.toml (or
$RECALL_SYNC), saves every listed file that is new or changed, and removes the
ones that are gone. Each saved chunk carries the file's hash, so a pass only
touches what changed.

sync.toml:

    [[watch]]
    path = "~/.claude/projects/-home-you/memory"   # a folder or one file
    title = "Claude memory"          # folder: label before the file name; file: the title
    tags = ["claude", "memory"]
    include = ["*.md"]               # folders only; default *.md and *.txt
    exclude = ["MEMORY.md", "drafts/"]   # a trailing slash means a folder
    recursive = true                 # default
    max_kb = 500                     # default; larger files are skipped

What sync never saves, whatever the config says: key and env files, binary
files, and any file whose text looks like it holds a secret (an API key, a
private key, a database password, an SSN or EIN). Everything in recall can
be read by the models that search it, so a file like that is skipped and
named in the report instead. Inside a watched folder, hidden files and
folders and symlinks are skipped too; to save a dotfile, give it its own
watch entry.

A file saved earlier with `recall add` keeps its title and tags unless the
watch sets its own. Files that leave a watched folder are removed from
recall; a watch entry that leaves sync.toml keeps its files until `--prune`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import db, ingest, sources
from .config import Settings

CONFIG = Path(os.environ.get("RECALL_SYNC") or Path.home() / ".config" / "recall" / "sync.toml")
DEFAULT_INCLUDE = ("*.md", "*.txt")
NEVER = (  # by name, even for a file with its own watch entry
    "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "*.age", "*.gpg", "*.cred",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.env", ".env*", "env",
)
IN_FOLDERS = (".*", ".*/", "node_modules/", "__pycache__/", "venv/")  # skipped while walking a folder
MAX_KB = 500
SCAN_VERSION = 2   # part of every hash: raise it when SECRETS changes, so every file is checked again

# High-precision patterns only: a miss here means a secret becomes searchable by
# every model with the recall tools, a false hit only means one file is skipped.
SECRETS: tuple[tuple[str, re.Pattern], ...] = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("API key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}|\bpa-[A-Za-z0-9_-]{30,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("AWS key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("1Password token", re.compile(r"\bops_[A-Za-z0-9_-]{40,}")),
    ("Proton Pass token", re.compile(r"\bpst_[A-Za-z0-9_-]{10,}::\S{10,}")),
    ("Airtable token", re.compile(r"\bpat[A-Za-z0-9]{14}\.[a-f0-9]{40,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.")),
    ("password in a URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]{3,}@")),
    ("SSN", re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")),
    ("EIN", re.compile(r"(?<![\d-])\d{2}-\d{7}(?![\d-])")),
)
# `token = <long random value>`; _random() keeps paths, ids and kebab-case names out of it.
ASSIGNED = re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|auth)\w*[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9+/_=-]{24,})")


def _random(value: str) -> bool:
    if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
        return True
    return "/" not in value and all(re.search(p, value) for p in ("[a-z]", "[A-Z]", "[0-9]"))


@dataclass(frozen=True)
class Watch:
    path: Path                       # resolved
    title: str | None = None
    tags: tuple[str, ...] = ()
    include: tuple[str, ...] = DEFAULT_INCLUDE
    exclude: tuple[str, ...] = ()
    recursive: bool = True
    max_bytes: int = MAX_KB * 1000

    @property
    def root(self) -> str:
        return str(self.path)


@dataclass
class Item:
    path: Path
    source: str
    title: str
    tags: list[str]
    root: str
    digest: str
    text: str = ""


@dataclass
class Plan:
    save: list[Item] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    missing: list[Path] = field(default_factory=list)
    unchanged: int = 0

    @property
    def changes(self) -> bool:
        return bool(self.save or self.remove)


def load(path: Path | None = None) -> list[Watch]:
    path = path or CONFIG
    if not path.is_file():
        raise SystemExit(f"no sync config at {path}; see contrib/sync.example.toml")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{path}: {exc}") from None
    watches = []
    for i, w in enumerate(data.get("watch", []), 1):
        unknown = set(w) - {"path", "title", "tags", "include", "exclude", "recursive", "max_kb"}
        if "path" not in w or unknown:
            raise SystemExit(f"{path}: watch #{i} needs `path`" + (f"; unknown keys: {sorted(unknown)}" if unknown else ""))
        watches.append(Watch(
            path=Path(w["path"]).expanduser().resolve(),
            title=w.get("title"),
            tags=tuple(w.get("tags", ())),
            include=tuple(w.get("include", DEFAULT_INCLUDE)),
            exclude=tuple(w.get("exclude", ())),
            recursive=bool(w.get("recursive", True)),
            max_bytes=int(w.get("max_kb", MAX_KB)) * 1000,
        ))
    return watches


def _matches(patterns: tuple[str, ...], rel: Path) -> bool:
    """A pattern ending in / names a folder anywhere on the way; others match the
    file name or the whole relative path."""
    for pat in patterns:
        if pat.endswith("/"):
            if any(fnmatch.fnmatch(part, pat[:-1]) for part in rel.parts[:-1]):
                return True
        elif fnmatch.fnmatch(rel.name, pat) or ("/" in pat and fnmatch.fnmatch(rel.as_posix(), pat)):
            return True
    return False


def candidates(watch: Watch) -> list[Path]:
    """Files a watch selects by name, before their content is looked at."""
    if watch.path.is_file():
        return [] if _matches(NEVER, Path(watch.path.name)) else [watch.path]
    skip = NEVER + IN_FOLDERS + watch.exclude
    found = []
    for folder, dirs, files in os.walk(watch.path):
        rel_dir = Path(folder).relative_to(watch.path)
        dirs[:] = sorted(
            d for d in dirs
            if watch.recursive and not _matches(skip, rel_dir / d / "x")
        )
        for name in sorted(files):
            rel = rel_dir / name
            path = watch.path / rel
            if _matches(watch.include, rel) and not _matches(skip, rel) and not path.is_symlink():
                found.append(path)
    return found


def find_secret(text: str) -> str | None:
    """'API key, line 12' for the first thing that looks like a secret, else None."""
    hits = [(m.start(), kind) for kind, pattern in SECRETS if (m := pattern.search(text))]
    hits += [(m.start(), "secret value") for m in ASSIGNED.finditer(text) if _random(m.group(1))]
    if not hits:
        return None
    at, kind = min(hits)
    return f"{kind}, line {text.count(chr(10), 0, at) + 1}"


def refuse_secret(text: str) -> None:
    """For what an assistant or the app saves directly (a memory, a note): raise rather than store a secret."""
    if found := find_secret(text):
        raise ValueError(
            f"not saved: this looks like it holds a secret ({found}). Everything in recall can be read by "
            "the assistants that search it; the vault is the place for it."
        )


def _digest(data: bytes, watch: Watch) -> str:
    """Content plus the title and tags from the config, so retagging a watch re-saves its files."""
    h = hashlib.sha256(data)
    h.update(f"\0{watch.title or ''}\0{','.join(watch.tags)}\0{SCAN_VERSION}".encode())
    return h.hexdigest()


def _read(path: Path, data: bytes) -> str | None:
    """The file's text, or None for a binary file."""
    if path.suffix.lower() == ".pdf":
        return sources.load(str(path))[2]
    if b"\0" in data[:8192]:
        return None
    return data.decode(errors="replace")


def stored(settings: Settings) -> dict[str, dict]:
    """Every file source in recall: {source: {title, tags, sync}} (sync is None for `recall add` saves)."""
    pipeline = [
        {"$match": {"source": {"$regex": "^file:"}, "tags": {"$nin": ["vault", "event"]}}},
        {"$group": {"_id": "$source", "title": {"$first": "$title"}, "tags": {"$first": "$tags"}, "sync": {"$first": "$sync"}}},
    ]
    return {r["_id"]: r for r in db.chunks(settings).aggregate(pipeline)}


def plan(watches: list[Watch], have: dict[str, dict]) -> Plan:
    """Compare the watched files with what recall holds. Reads files, changes nothing."""
    out = Plan()
    eligible: set[str] = set()
    live_roots: set[str] = set()
    for watch in watches:
        if not watch.path.exists():
            out.missing.append(watch.path)
            continue
        live_roots.add(watch.root)
        single = watch.path.is_file()
        for path in candidates(watch):
            source = f"file:{path}"
            if source in eligible:
                continue  # an earlier watch already covers this file
            try:
                size = path.stat().st_size
                if size > watch.max_bytes:
                    out.skipped.append((path, f"larger than {watch.max_bytes // 1000} KB"))
                    continue
                data = path.read_bytes()
            except OSError as exc:
                out.skipped.append((path, f"unreadable: {exc.strerror}"))
                continue
            digest = _digest(data, watch)
            prior = have.get(source)
            if prior and (prior.get("sync") or {}).get("hash") == digest and prior["sync"].get("root") == watch.root:
                eligible.add(source)
                out.unchanged += 1
                continue
            text = _read(path, data)
            if text is None or not text.strip():
                out.skipped.append((path, "binary" if text is None else "empty"))
                continue
            secret = find_secret(text)
            if secret:
                out.skipped.append((path, f"looks like it holds a secret ({secret})"))
                continue
            eligible.add(source)
            rel = path.name if single else path.relative_to(watch.path).as_posix()
            if single:
                title = watch.title or (prior or {}).get("title") or path.name
            else:
                title = f"{watch.title or watch.path.name}: {rel}"
            tags = list(watch.tags) or list((prior or {}).get("tags") or [])
            out.save.append(Item(path, source, title, tags, watch.root, digest, text))

    configured = {w.root for w in watches}
    for source, row in sorted(have.items()):
        root = (row.get("sync") or {}).get("root")
        if root is None or source in eligible:
            continue
        if root not in configured:
            out.orphans.append(source)
        elif root in live_roots:
            out.remove.append(source)
    return out


def apply(settings: Settings, todo: Plan, prune: bool = False) -> tuple[int, int, list[tuple[Path, str]]]:
    """Carry out a plan. Returns (saved, removed, failures); a failure leaves that
    file for the next pass."""
    saved, failed = 0, []
    for item in todo.save:
        try:
            ingest.store(settings, item.source, item.title, item.text, item.tags,
                         extra={"sync": {"hash": item.digest, "root": item.root}})
            saved += 1
        except SystemExit as exc:
            failed.append((item.path, str(exc)))
        except Exception as exc:  # network, rate limit: keep going, retry next pass
            failed.append((item.path, f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"))
    removed = 0
    for source in todo.remove + (todo.orphans if prune else []):
        removed += 1 if ingest.remove(settings, source) else 0
    return saved, removed, failed


def fingerprint(watches: list[Watch]) -> tuple:
    """Cheap picture of the watched files (names, sizes, change times), so the
    loop only opens files and the database when something moved."""
    seen = []
    for watch in watches:
        for path in candidates(watch) if watch.path.exists() else []:
            try:
                st = path.stat()
                seen.append((str(path), st.st_mtime_ns, st.st_size))
            except OSError:
                pass
    return tuple(seen)


def report(todo: Plan, home: Path | None = None) -> list[str]:
    """Lines describing a plan, for the dry run and the log."""
    home = home or Path.home()

    def short(p: Path | str) -> str:
        s = str(p).removeprefix("file:")
        return "~" + s[len(str(home)):] if s.startswith(str(home) + "/") else s

    lines = [f"  save     {short(i.path)}  ->  {i.title}" for i in todo.save]
    lines += [f"  remove   {short(s)}  (file is gone or no longer selected)" for s in todo.remove]
    lines += [f"  skip     {short(p)}  ({why})" for p, why in todo.skipped]
    lines += [f"  orphan   {short(s)}  (its watch left sync.toml; --prune removes it)" for s in todo.orphans]
    lines += [f"  missing  {short(p)}  (watch path not found; nothing removed)" for p in todo.missing]
    return lines


FULL_CHECK = 3600   # seconds; the loop compares with the database at least this often


def run_loop(settings: Settings, interval: int, config: Path | None, echo, prune: bool = False) -> None:
    """Check every `interval` seconds. A pass that finds the same file names, sizes and
    change times as the last one stops there; once an hour it compares with the
    database anyway, which catches a source removed with `recall rm`."""
    last, checked = None, 0.0
    while True:
        try:
            watches = load(config)
            now = (fingerprint(watches), (config or CONFIG).stat().st_mtime_ns)
            if now != last or time.monotonic() - checked > FULL_CHECK:
                checked = time.monotonic()
                todo = plan(watches, stored(settings))
                saved, removed, failed = apply(settings, todo, prune)
                if saved or removed or failed:
                    echo(f"sync: {saved} saved, {removed} removed, {len(failed)} failed, {len(todo.skipped)} skipped")
                    for path, why in failed:
                        echo(f"  failed   {path}  ({why})")
                last = now if not failed else None
        except SystemExit as exc:       # bad config: say so, keep running, pick it up when fixed
            echo(f"sync: {exc}")
            last = None
        except Exception as exc:        # database or network hiccup
            echo(f"sync: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            last = None
        time.sleep(interval)
