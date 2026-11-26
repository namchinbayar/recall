"""Tool definitions the model sees, and the dispatch that runs them.

Order is fixed so the tools block is byte-stable for prompt caching.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from pymongo.errors import OperationFailure
from voyageai.error import RateLimitError

from .. import ingest, search
from ..config import Settings
from ..metrics import store as mstore
from . import memory

TOOLS: list[dict] = [
    {
        "name": "search_knowledge",
        "description": (
            "Semantic search over everything the user has saved: articles, notes, files, and "
            "memories from past conversations. Call this before answering anything about the "
            "user's life, setup, past decisions, or saved material. Returns the best matching "
            "chunks with scores; memories carry the tag 'memory'. Stream events (desktop "
            "notifications, Hermes exchanges) are left out unless include_events is true or tags "
            "are given; turn it on for questions about what happened or was said recently. Time "
            "words in the query (yesterday, last week, in October, since 2026-11-20) limit results "
            "to that window; with nothing else in the query, the window is listed newest first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, in natural language."},
                "limit": {"type": "integer", "description": "Max results, default 8.", "minimum": 1, "maximum": 20},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Only chunks with any of these tags."},
                "include_events": {"type": "boolean", "description": "Also search stream events. Default false."},
                "since": {"type": "string", "description": "Only things from this date on (YYYY-MM-DD, local time)."},
                "until": {"type": "string", "description": "Only things before the end of this date (YYYY-MM-DD, local time)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Store one durable fact about the user or their world so future conversations can "
            "find it. Use for preferences, decisions, facts about their setup or life, and "
            "corrections. One fact per call, phrased as a standalone sentence. Do not store "
            "transient chit-chat or anything already in the knowledge base."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "A standalone sentence, e.g. 'The user runs niri on CachyOS and prefers Chrome Beta.'"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional topic tags."},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "forget",
        "description": "Delete a memory by its source id (memory:xxxx), e.g. when the user says a stored fact is wrong.",
        "input_schema": {
            "type": "object",
            "properties": {"source": {"type": "string"}},
            "required": ["source"],
        },
    },
    {
        "name": "list_memories",
        "description": "List the most recent stored memories with their source ids.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "required": [],
        },
    },
    {
        "name": "list_sources",
        "description": "List what is in the knowledge base: each source with title, chunk count, and tags.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_source",
        "description": "Ingest a URL or local file path into the knowledge base so it becomes searchable.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "A URL or an absolute file path."},
                "tags": {"type": "array", "items": {"type": "string"}},
                "title": {"type": "string"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "metrics_summary",
        "description": (
            "System metrics from the user's machines, averaged per time bucket: CPU %, load, "
            "memory %, CPU temperature, and sample count. Use for questions like 'how busy was "
            "my machine today' or 'is it running hot'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "Window like 90m, 24h, 7d. Default 24h."},
                "bucket": {"type": "string", "enum": ["minute", "hour", "day"], "description": "Default hour."},
                "host": {"type": "string", "description": "Hostname filter, e.g. desktop."},
            },
            "required": [],
        },
    },
    {
        "name": "metrics_manual",
        "description": "Manually recorded metrics like sleep_hours or weight_kg, with timestamps and notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Metric name; omit for all."},
                "since": {"type": "string", "description": "Window like 7d, 30d. Default 30d."},
            },
            "required": [],
        },
    },
    {
        "name": "record_metric",
        "description": "Record a manual metric the user states, e.g. 'I slept 7 hours' -> sleep_hours=7.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "snake_case name with unit, e.g. sleep_hours, weight_kg."},
                "value": {"type": "number"},
                "note": {"type": "string"},
                "at": {"type": "string", "description": "ISO timestamp if not now."},
            },
            "required": ["name", "value"],
        },
    },
]


def excerpt(text: str, query: str, max_chars: int) -> str:
    """A window of text around the first query word found, for compact results."""
    if len(text) <= max_chars:
        return text
    low = text.lower()
    found = [i for w in re.findall(r"\w{4,}", query.lower()) if (i := low.find(w)) >= 0]
    start = max(min(found) - max_chars // 4, 0) if found else 0
    start = min(start, len(text) - max_chars)
    piece = text[start : start + max_chars]
    return ("... " if start else "") + piece + (" ..." if start + max_chars < len(text) else "")


def day_bound(value: str | None, end: bool = False) -> datetime | None:
    """A YYYY-MM-DD (or full ISO time) as UTC; a bare date as the start of that local day,
    or with end=True the start of the next one, so `until` takes in the day it names."""
    if not value:
        return None
    from datetime import timedelta

    from .. import when

    text = str(value).strip()
    if len(text) == 10:
        start = datetime.fromisoformat(text).replace(tzinfo=when.zone())
        return (start + timedelta(days=1) if end else start).astimezone(timezone.utc)
    at = datetime.fromisoformat(text)
    return (at if at.tzinfo else at.replace(tzinfo=when.zone())).astimezone(timezone.utc)


def _fmt_hits(hits: list[search.Hit], query: str = "", max_chars: int | None = None) -> str:
    window = getattr(hits, "window", None)
    head = f"time window: {window.describe()}\n\n" if window else ""
    if not hits:
        return f"{head}no results"
    out = []
    for h in hits:
        tags = f" [{', '.join(h.tags)}]" if h.tags else ""
        body = excerpt(h.text, query, max_chars) if max_chars else h.text
        day = f"  {h.created_at.astimezone():%Y-%m-%d}" if h.created_at else ""
        out.append(f"### {h.title}{tags}  (score {h.score:.3f}){day}\nsource: {h.source} #{h.chunk_index}\n{body}")
    return head + "\n\n".join(out)


def run(settings: Settings, name: str, args: dict) -> tuple[str, bool]:
    """Execute a tool. Returns (content, is_error)."""
    try:
        if name == "search_knowledge":
            limit = int(args.get("limit") or 8)
            kw = dict(limit=limit, tags=args.get("tags"), include_events=bool(args.get("include_events")), when=True,
                      since=day_bound(args.get("since")), until=day_bound(args.get("until"), end=True))
            try:
                hits = search.search(settings, args["query"], **kw)
            except RateLimitError:
                time.sleep(22)  # one retry after the per-minute window moves on
                hits = search.search(settings, args["query"], **kw)
            return _fmt_hits(hits, args["query"], args.get("max_chars")), False

        if name == "remember":
            source = memory.remember(settings, args["fact"], args.get("tags"))
            return f"stored as {source}", False

        if name == "forget":
            n = memory.forget(settings, args["source"])
            return f"removed {n} memory", False

        if name == "list_memories":
            rows = memory.recent(settings, int(args.get("limit") or 20))
            if not rows:
                return "no memories stored", False
            return "\n".join(f"{r['source']}  {r['created_at']:%Y-%m-%d}  {r['text']}" for r in rows), False

        if name == "list_sources":
            rows = ingest.list_sources(settings)
            if not rows:
                return "knowledge base is empty", False
            return "\n".join(
                f"{r['chunks']:>3} chunks  {r['title']}  [{', '.join(r.get('tags') or [])}]  {r['_id']}" for r in rows
            ), False

        if name == "save_source":
            source, n = ingest.ingest(settings, args["target"], title=args.get("title"), tags=args.get("tags"))
            return f"stored {n} chunk(s) from {source}", False

        if name == "metrics_summary":
            rows = mstore.summary(settings, args.get("since") or "24h", args.get("host"), args.get("bucket") or "hour")
            if not rows:
                return "no samples in that window", False
            lines = ["bucket (local)      cpu%  load1  mem%  temp  n"]
            for r in rows:
                temp = f"{r['cpu_temp']:.0f}" if r.get("cpu_temp") is not None else "-"
                lines.append(
                    f"{r['_id'].astimezone():%Y-%m-%d %H:%M}  {r['cpu_pct']:>5.0f}  {r['load1']:>5.2f}  "
                    f"{r['mem_pct']:>4.0f}  {temp:>4}  {r['samples']}"
                )
            return "\n".join(lines), False

        if name == "metrics_manual":
            rows = mstore.manual(settings, args.get("since") or "30d", args.get("name"))
            if not rows:
                return "no manual entries", False
            return "\n".join(
                f"{r['ts'].astimezone():%Y-%m-%d %H:%M}  {r['meta']['name']}={r['value']}"
                + (f"  ({r['note']})" if r.get("note") else "")
                for r in rows
            ), False

        if name == "record_metric":
            ts = datetime.fromisoformat(args["at"]) if args.get("at") else datetime.now(timezone.utc)
            if ts.tzinfo is None:
                ts = ts.astimezone()
            import socket
            doc = {
                "ts": ts.astimezone(timezone.utc),
                "meta": {"host": socket.gethostname(), "kind": "manual", "name": args["name"]},
                "value": float(args["value"]),
            }
            if args.get("note"):
                doc["note"] = args["note"]
            mstore.insert(settings, doc)
            return f"recorded {args['name']}={args['value']} at {doc['ts']:%Y-%m-%d %H:%M} UTC", False

        return f"unknown tool {name}", True

    except RateLimitError as exc:
        return f"embedding rate limit hit; tell the user to wait a minute and try again. {str(exc).splitlines()[0]}", True
    except OperationFailure as exc:
        return f"database error: {exc.details.get('errmsg') if exc.details else exc}", True
    except (KeyError, ValueError, TypeError, SystemExit) as exc:
        return f"bad input: {exc}", True
    except Exception as exc:  # a page that will not load, the database or Voyage out of reach
        return f"{name} failed: {type(exc).__name__}: {(str(exc).splitlines() or [''])[0][:200]}", True


def describe_call(name: str, args: dict) -> str:
    """One-line rendering for the terminal."""
    short = {k: (v if not isinstance(v, str) or len(v) <= 60 else v[:57] + "...") for k, v in args.items()}
    return f"{name}({json.dumps(short, ensure_ascii=False)[1:-1]})"
