"""recall's web API: the JSON routes the recall app calls, served next to the online MCP server.

The MCP tools answer in text written for a model. An app needs the same things as data, so
these routes return JSON. They sit on the same server (`recall mcp-online`), under /api/v1, and
follow the same rules: every request carries an access token, which is checked the same way
(signature, issuer, audience, expiry, allow-list), and each route needs its permission:
reading needs recall:read, saving recall:write, deleting recall:forget.

The app's own pages never hold the token. The Worker in front signs the user in, keeps the
token, and adds it to the requests it forwards here (see deploy/cloudflare).

The encrypted vault is out of reach here as everywhere else: search leaves it out and no
route returns a chunk tagged `vault`.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime

import anyio
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import db, ingest, mcp_server, openitems, search
from .agent import memory
from .agent.tools import excerpt
from .metrics import store as mstore

log = logging.getLogger("recall.api")

PREFIX = "/api/v1"
READ, WRITE, FORGET = mcp_server.READ_SCOPE, mcp_server.WRITE_SCOPE, mcp_server.FORGET_SCOPE

MAX_BODY = 400_000       # bytes of one request body
MAX_QUERY = 1_000
MAX_FACT = 2_000
MAX_NOTE = 200_000
MAX_TITLE = 200
MAX_TAGS = 12
EXCERPT_CHARS = 700
SOURCE_CHUNKS = 200      # chunks of one source returned at most
_SINCE = re.compile(r"^\d{1,4}[mhd]$")
_MEMORY_ID = re.compile(r"^memory:[0-9a-f]{12}$")
_TAG = re.compile(r"^[\w][\w .:/+-]{0,39}$")
HIDDEN = "vault"         # never returned by any route


class Refused(Exception):
    """A request this API will not serve; becomes a JSON error with this status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


# ----- reading the request ---------------------------------------------------

def _int(request: Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return min(max(int(raw), low), high)
    except ValueError:
        raise Refused(400, f"{name} must be a number") from None


def _flag(request: Request, name: str) -> bool:
    return request.query_params.get(name, "").lower() in ("1", "true", "yes", "on")


def _tags(value: object) -> list[str]:
    """Tags from a query string ("a,b") or a JSON list; anything odd is refused, not cleaned."""
    if value is None or value == "":
        return []
    parts = value.split(",") if isinstance(value, str) else value
    if not isinstance(parts, list) or len(parts) > MAX_TAGS:
        raise Refused(400, f"tags must be a list of at most {MAX_TAGS}")
    out: list[str] = []
    for part in parts:
        tag = part.strip() if isinstance(part, str) else ""
        if not _TAG.match(tag):
            raise Refused(400, "a tag is 1 to 40 letters, digits, spaces or . : / + -")
        if tag not in out:
            out.append(tag)
    return out


def _text(body: dict, name: str, limit: int, required: bool = True) -> str:
    value = body.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise Refused(400, f"{name} is missing")
    if len(value) > limit:
        raise Refused(413, f"{name} is longer than {limit:,} characters")
    return value.strip()


async def _body(request: Request) -> dict:
    if "application/json" not in request.headers.get("content-type", ""):
        raise Refused(415, "send JSON (Content-Type: application/json)")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise Refused(413, "the request is too large")
    try:
        body = json.loads(raw)
    except ValueError:
        raise Refused(400, "the request is not valid JSON") from None
    if not isinstance(body, dict):
        raise Refused(400, "the request must be a JSON object")
    return body


# ----- shaping the answers ---------------------------------------------------

def _when(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def kind_of(source: str, tags: list[str]) -> str:
    """What a chunk is, in the app's words."""
    have = set(tags)
    if "memory" in have:
        return "learned" if "learned" in have else "memory"
    if "event" in have:
        if "digest" in have:
            return "digest"
        return "chat" if "hermes" in have else "notification"
    if "session-note" in have:
        return "session"
    if source.startswith("note:"):
        return "note"
    if source.startswith(("http://", "https://")):
        return "page"
    return "file"


def hit_json(h: search.Hit, query: str, full: bool) -> dict:
    return {
        "id": f"{h.source}#{h.chunk_index}", "source": h.source, "chunk": h.chunk_index,
        "title": h.title, "kind": kind_of(h.source, h.tags), "tags": h.tags,
        "text": h.text if full else excerpt(h.text, query, EXCERPT_CHARS),
        "score": round(h.score, 3), "at": _when(h.created_at),
    }


def memory_json(row: dict) -> dict:
    tags = row.get("tags") or []
    out = {
        "id": row["source"], "text": row["text"], "kind": kind_of(row["source"], tags),
        "tags": [t for t in tags if t != "memory"], "at": _when(row.get("created_at")),
    }
    if stamp := row.get("retired"):
        out["retired"] = {"at": _when(stamp.get("at")), "why": stamp.get("why") or "", "by": stamp.get("by")}
    return out


# ----- the work, one function per route (blocking; run in a thread) ----------

def do_search(q: str, limit: int, tags: list[str], events: bool, full: bool) -> dict:
    hits = search.search(mcp_server._settings(), q, limit=limit, tags=tags or None, include_events=events, when=True)
    out = {"query": q, "hits": [hit_json(h, q, full) for h in hits]}
    if hits.window:
        out["window"] = hits.window.describe()
    return out


def do_memories(limit: int, learned_only: bool) -> dict:
    settings = mcp_server._settings()
    if not learned_only:
        return {"memories": [memory_json(r) for r in memory.recent(settings, limit)]}
    rows = (db.chunks(settings)
            .find({"tags": {"$all": ["memory", "learned"]}}, {"_id": 0, "source": 1, "text": 1, "tags": 1, "created_at": 1})
            .sort("created_at", -1).limit(limit))
    return {"memories": [memory_json(r) for r in rows]}


def do_remember(fact: str, tags: list[str]) -> dict:
    return {"id": memory.remember(mcp_server._settings(), fact, tags or None)}


def do_forget(source: str) -> dict:
    return {"removed": memory.forget(mcp_server._settings(), source)}


def do_retired(limit: int) -> dict:
    return {"memories": [memory_json(r) for r in memory.retired(mcp_server._settings(), limit)]}


def do_restore(source: str) -> dict:
    return {"restored": memory.restore(mcp_server._settings(), source)}


def do_note(title: str, text: str, tags: list[str]) -> dict:
    try:
        source, chunks = ingest.ingest_text(mcp_server._settings(), text, title, tags or None)
    except SystemExit as exc:
        raise Refused(400, str(exc)) from None
    return {"id": source, "chunks": chunks}


def do_sources(limit: int) -> dict:
    rows = ingest.list_sources(mcp_server._settings())
    kinds = Counter(kind_of(r["_id"], r.get("tags") or []) for r in rows)
    return {"total": len(rows), "kinds": dict(kinds.most_common()), "sources": [{
        "id": r["_id"], "title": r["title"], "kind": kind_of(r["_id"], r.get("tags") or []),
        "tags": r.get("tags") or [], "chunks": r["chunks"], "at": _when(r.get("created_at")),
    } for r in rows[:limit]]}


def do_source(source: str) -> dict:
    rows = list(db.chunks(mcp_server._settings())
                .find({"source": source, "tags": {"$ne": HIDDEN}},
                      {"_id": 0, "title": 1, "text": 1, "tags": 1, "created_at": 1, "chunk_index": 1})
                .sort("chunk_index", 1).limit(SOURCE_CHUNKS))
    if not rows:
        raise Refused(404, "nothing saved under that id")
    first = rows[0]
    return {
        "id": source, "title": first["title"], "kind": kind_of(source, first.get("tags") or []),
        "tags": first.get("tags") or [], "at": _when(first.get("created_at")),
        "chunks": [r["text"] for r in rows],
    }


def do_events(since: str, source: str | None, limit: int) -> dict:
    rows = ingest.recent_events(mcp_server._settings(), mstore.parse_since(since), source, limit)
    return {"events": [{
        "title": r["title"], "text": r["text"], "at": _when(r.get("created_at")),
        "kind": kind_of("", r.get("tags") or ["event"]), "source": (r.get("event") or {}).get("source"),
    } for r in rows]}


def do_event_counts(since: str) -> dict:
    """How many events arrived since then, by kind, without fetching them."""
    rows = db.chunks(mcp_server._settings()).aggregate([
        {"$match": {"tags": "event", "created_at": {"$gte": mstore.parse_since(since)}}},
        {"$group": {"_id": "$tags", "n": {"$sum": 1}}},
    ])
    kinds: Counter = Counter()
    for row in rows:
        kinds[kind_of("", row["_id"] or ["event"])] += row["n"]
    return {"since": since, "total": sum(kinds.values()), "kinds": dict(kinds.most_common())}


def do_open(days: int) -> dict:
    """What sessions left open and nothing has closed, newest session first."""
    from . import serve

    out = serve.open_items(mcp_server._settings(), {"days": days, "limit": serve.OPEN_LIMIT[1], "all": True})
    notes = [{
        "source": n["source"], "title": n["title"], "at": n["date"], "folder": n["folder"], "kind": n["kind"],
        "items": [{"id": i, "text": t, "kind": k} for i, t, k in zip(n["ids"], n["items"], n["kinds"])],
    } for n in out["notes"]]
    kinds = Counter(item["kind"] for n in notes for item in n["items"])
    return {"days": out["days"], "total": sum(kinds.values()), "notes": notes,
            "kinds": {k: kinds[k] for k in openitems.KIND_ORDER if kinds[k]}}


def do_open_done(item: str, how: str, why: str) -> dict:
    try:
        found = openitems.close_by_hand(mcp_server._settings(), item, how, why, by="app")
    except LookupError as exc:
        raise Refused(404, str(exc)) from None
    return {"closed": {"id": found.id, "text": found.text}}


def do_open_reopen(item: str) -> dict:
    return {"reopened": openitems.Store(mcp_server._settings()).reopen(item)}


def do_open_closed(limit: int) -> dict:
    """The latest closures: what came off the list, how, why, and what closed it."""
    return {"closed": [{
        "id": r["_id"], "text": r.get("text", ""), "how": r.get("how", "done"), "why": r.get("why", ""),
        "quote": r.get("quote", ""), "by": r.get("closed_by", ""), "at": _when(r.get("closed_at")),
        "title": (r.get("title") or "").removeprefix("Session: "),
    } for r in openitems.Store(mcp_server._settings()).recent(limit)]}


def do_profile() -> dict:
    from . import learn

    doc = learn.profile(mcp_server._settings())
    return {"text": doc["text"], "written": _when(doc.get("written"))} if doc else {"text": "", "written": None}


def do_status() -> dict:
    from . import serve

    counts = serve.status(mcp_server._settings())
    counts.pop("ok", None)
    return counts


# ----- routes ----------------------------------------------------------------

Handler = Callable[[Request], Awaitable[dict]]


def register(server, verifier) -> None:
    """Add the API's routes to the online server. `verifier` is the same token checker the
    MCP endpoint uses, so one sign-in opens both and the same people are refused at both."""

    async def signed_in(request: Request, scope: str | None) -> AccessToken:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        access = await verifier.verify_token(token.strip()) if scheme.lower() == "bearer" and token.strip() else None
        if access is None:
            raise Refused(401, "sign in first")
        if (access.claims or {}).get("agent"):  # an agent key (online.py) gets MCP and /serve, not the app's API
            raise Refused(403, "the app's API is only for the owner's own sign-in, not an agent key")
        if scope is not None and scope not in access.scopes:
            raise Refused(403, f"this sign-in does not include the {scope} permission")
        return access

    def route(path: str, method: str, scope: str | None, status: int = 200):
        def decorate(handler: Handler):
            async def endpoint(request: Request) -> JSONResponse:
                headers = {"Cache-Control": "no-store"}
                try:
                    request.state.access = await signed_in(request, scope)
                    return JSONResponse(await handler(request), status_code=status, headers=headers)
                except Refused as exc:
                    if exc.status == 401:
                        headers["WWW-Authenticate"] = "Bearer"
                    return JSONResponse({"error": exc.message}, status_code=exc.status, headers=headers)
                except ValueError as exc:       # the data layer's own refusals (an empty fact, a wrong id)
                    return JSONResponse({"error": str(exc)}, status_code=400, headers=headers)
                except Exception:
                    log.exception("%s %s failed", method, path)
                    return JSONResponse({"error": "recall could not do that just now"}, status_code=500, headers=headers)
            server.custom_route(PREFIX + path, methods=[method], name=f"api:{method}:{path}")(endpoint)
            return handler
        return decorate

    run = anyio.to_thread.run_sync

    @route("/me", "GET", None)
    async def me(request: Request) -> dict:
        access = request.state.access
        return {"user": access.subject, "scopes": [s for s in access.scopes if s.startswith("recall:")]}

    @route("/status", "GET", READ)
    async def status(request: Request) -> dict:
        return await run(do_status)

    @route("/search", "GET", READ)
    async def find(request: Request) -> dict:
        q = " ".join(request.query_params.get("q", "").split())
        if not q:
            raise Refused(400, "q is missing")
        if len(q) > MAX_QUERY:
            raise Refused(413, f"q is longer than {MAX_QUERY:,} characters")
        return await run(do_search, q, _int(request, "limit", 8, 1, 20), _tags(request.query_params.get("tags")),
                         _flag(request, "events"), _flag(request, "full"))

    @route("/memories", "GET", READ)
    async def memories(request: Request) -> dict:
        return await run(do_memories, _int(request, "limit", 30, 1, 200), _flag(request, "learned"))

    @route("/memories", "POST", WRITE, status=201)
    async def remember(request: Request) -> dict:
        body = await _body(request)
        return await run(do_remember, _text(body, "fact", MAX_FACT), _tags(body.get("tags")))

    @route("/memories/{id}", "DELETE", FORGET)
    async def forget(request: Request) -> dict:
        return await run(do_forget, _memory_id(request))

    @route("/retired", "GET", READ)
    async def retired(request: Request) -> dict:
        return await run(do_retired, _int(request, "limit", 30, 1, 200))

    @route("/retired/{id}/restore", "POST", WRITE)
    async def restore(request: Request) -> dict:
        return await run(do_restore, _memory_id(request))

    @route("/notes", "POST", WRITE, status=201)
    async def note(request: Request) -> dict:
        body = await _body(request)
        text = _text(body, "text", MAX_NOTE)
        title = _text(body, "title", MAX_TITLE, required=False) or text.splitlines()[0][:72]
        return await run(do_note, title, text, _tags(body.get("tags")))

    @route("/sources", "GET", READ)
    async def sources(request: Request) -> dict:
        return await run(do_sources, _int(request, "limit", 100, 1, 1000))

    @route("/source", "GET", READ)
    async def source(request: Request) -> dict:
        ident = request.query_params.get("id", "").strip()
        if not ident or len(ident) > 2_000:
            raise Refused(400, "id is missing")
        return await run(do_source, ident)

    def since_of(request: Request) -> str:
        since = request.query_params.get("since", "24h")
        if not _SINCE.match(since):
            raise Refused(400, "since looks like 90m, 24h or 7d")
        return since

    @route("/events/counts", "GET", READ)
    async def event_counts(request: Request) -> dict:
        return await run(do_event_counts, since_of(request))

    @route("/events", "GET", READ)
    async def events(request: Request) -> dict:
        since = since_of(request)
        stream = request.query_params.get("source") or None
        if stream not in (None, "desktop-notification", "hermes"):
            raise Refused(400, "source is desktop-notification or hermes")
        return await run(do_events, since, stream, _int(request, "limit", 100, 1, 500))

    @route("/profile", "GET", READ)
    async def profile(request: Request) -> dict:
        return await run(do_profile)

    @route("/open", "GET", READ)
    async def open_list(request: Request) -> dict:
        return await run(do_open, _int(request, "days", 14, 1, 90))

    @route("/open/closed", "GET", READ)
    async def open_closed(request: Request) -> dict:
        return await run(do_open_closed, _int(request, "limit", 30, 1, 200))

    @route("/open/{id}/done", "POST", WRITE)
    async def open_done(request: Request) -> dict:
        body = await _body(request) if await request.body() else {}
        how = body.get("how", "done")
        if how not in openitems.HAND_HOWS:
            raise Refused(400, f"how is one of: {', '.join(openitems.HAND_HOWS)}")
        return await run(do_open_done, _open_id(request), how, _text(body, "why", openitems.MAX_WHY, required=False))

    @route("/open/{id}/reopen", "POST", WRITE)
    async def open_reopen(request: Request) -> dict:
        return await run(do_open_reopen, _open_id(request))


def _open_id(request: Request) -> str:
    ident = request.path_params["id"]
    if not openitems.ITEM_ID.match(ident):
        raise Refused(400, "an open item's id looks like open:0123456789ab")
    return ident


def _memory_id(request: Request) -> str:
    ident = request.path_params["id"]
    if not _MEMORY_ID.match(ident):
        raise Refused(400, "a memory id looks like memory:0123456789ab")
    return ident
