"""recall as an MCP server (stdio), for Claude Code, Codex, Kiro, Copilot, Hermes.

`recall mcp` starts it; each client launches its own copy. The tools are thin
wrappers over the same code the `recall chat` agent uses (agent/tools.py), so
results read the same everywhere. The encrypted vault is never reachable from
here: search excludes it and no tool touches it. The online server, behind the
owner's sign-in, adds three vault tools (vault_list, vault_get, vault_save).

Settings load on the first call, not at import, so a client can list the
tools even when the env file is missing; the call then reports what's wrong.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import config, ingest, vault
from .agent import tools as agent_tools
from .metrics import store as mstore

INSTRUCTIONS = """recall is the user's personal knowledge base: saved articles, notes and files, \
durable memories, recent events (desktop notifications and Hermes conversations), and \
machine metrics, shared by all their assistants. When you do not already know them, call \
profile once at the start; call catch_up to see what they and their other assistants worked \
on lately and what was left open. When they say they did something that is on that list, or \
that it no longer matters, call open_items for its id and take it off with close_open_item. \
Search it before answering questions about their setup, \
projects, past decisions, or anything they saved; time words in a query ("yesterday", \
"last week") limit it to that window. When they state a lasting fact or preference, call \
remember. Save useful pages or notes they ask to keep with save_url_or_file or save_note. \
Encrypted vault entries are not reachable here; for those, tell them to run \
`recall vault get "<words>"` in a terminal."""

ONLINE_INSTRUCTIONS = INSTRUCTIONS.replace(
    """Encrypted vault entries are not reachable here; for those, tell them to run \
`recall vault get "<words>"` in a terminal.""",
    """His encrypted vault (IDs, documents, account details) is reachable only through the \
vault_* tools, never through search: vault_list shows what is there, vault_get decrypts one \
entry, vault_save stores a new one. Open a vault entry only when they ask for that information, \
and quote from it only what the task needs.""",
)
assert ONLINE_INSTRUCTIONS != INSTRUCTIONS

mcp = MCPServer("recall", instructions=INSTRUCTIONS)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
EXCERPT_CHARS = 700
OPEN_NOTES = 100         # sessions in one open_items answer: enough for a model to read, not the whole fortnight

ADD = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

# The permission an online caller needs for each kind of tool (see online.py).
READ_SCOPE, WRITE_SCOPE, FORGET_SCOPE = "recall:read", "recall:write", "recall:forget"

# Every tool as (function, online permission, registration options). online.py registers the
# same functions on its own server; a tool whose permission is None is never offered online.
TOOLS: list[tuple[Callable[..., str], str | None, dict]] = []


def tool(scope: str | None, online_only: bool = False, **options):
    """Register a tool here (stdio) and record it for the online server.
    An online_only tool is offered only online, behind the owner's sign-in."""
    def register(fn):
        TOOLS.append((fn, scope, options))
        return fn if online_only else mcp.tool(**options)(fn)
    return register


@cache
def _settings() -> config.Settings:
    try:
        return config.load()
    except SystemExit as exc:
        raise ToolError(f"recall is not configured: {exc}") from None


def _run(name: str, args: dict) -> str:
    content, is_error = agent_tools.run(_settings(), name, {k: v for k, v in args.items() if v is not None})
    if is_error:
        raise ToolError(content)
    return content


@tool(READ_SCOPE, title="Search recall", annotations=READ)
def search(
    query: Annotated[str, Field(description="What to look for, in natural language or exact words.")],
    limit: Annotated[int, Field(ge=1, le=20, description="Max results.")] = 5,
    tags: Annotated[list[str] | None, Field(description="Only chunks with any of these tags.")] = None,
    include_events: Annotated[bool, Field(description="Also search notifications and Hermes conversations.")] = False,
    full: Annotated[bool, Field(description="Return whole chunks (about 1,500 characters each) instead of excerpts.")] = False,
    since: Annotated[str | None, Field(description="Only things from this date on, YYYY-MM-DD (their local time).")] = None,
    until: Annotated[str | None, Field(description="Only things up to and including this date, YYYY-MM-DD.")] = None,
) -> str:
    """Hybrid search (meaning plus exact words, reranked) over saved material and memories.
    Scores near 1 are strong matches; below about 0.3 is weak. Each hit is a 700-character
    excerpt around the matching words, with its date; search again with full=true and a
    small limit when you need a whole chunk. Turn on include_events for questions about
    what happened or was said recently. Time words in the query (yesterday, last week,
    3 days ago, in October, since 2026-11-20) limit results to that window, and a query that is only
    a time ("what did I do yesterday") lists that window newest first; since/until set
    the window exactly."""
    return _run("search_knowledge", {
        "query": query, "limit": limit, "tags": tags, "include_events": include_events,
        "max_chars": None if full else EXCERPT_CHARS, "since": since, "until": until,
    })


@tool(READ_SCOPE, title="Who the user is", annotations=READ)
def profile() -> str:
    """The standing profile of the user that recall keeps for all their assistants: who they are,
    how they like to work, their machines and tools, their projects, and what they did lately.
    Rewritten from their assistants' notes and memories when they change."""
    from . import learn

    doc = learn.profile(_settings())
    if not doc:
        return "no profile yet: `recall learn` writes one from their notes and memories"
    return f"(written {doc['written']:%Y-%m-%d %H:%M} UTC from {doc.get('sources', '?')} notes and memories)\n\n{doc['text']}"


@tool(READ_SCOPE, title="Catch up", annotations=READ)
def catch_up(
    days: Annotated[int, Field(ge=1, le=60, description="How far back to look.")] = 7,
    with_profile: Annotated[bool, Field(description="Start with the profile.")] = True,
) -> str:
    """Pick up where their other assistants left off: the profile, then every Claude Code and
    Hermes session of the last few days, newest first, with what each left open. Use it at
    the start of work that continues something, or when they say 'where were we'."""
    from . import serve

    return serve.catch_up(_settings(), days, with_profile=with_profile)


@tool(READ_SCOPE, title="Open items", annotations=READ)
def open_items(
    days: Annotated[int, Field(ge=1, le=90, description="How far back to look.")] = 14,
    kind: Annotated[Literal["step", "todo", "waiting", "check", "idea"] | None, Field(
        description="Only one kind: step (only they can do it), todo (unfinished work), waiting (on someone or a date), "
                    "check (not verified), idea (optional).")] = None,
    everything: Annotated[bool, Field(description="Also the unverified caveats and ideas older than a week.")] = False,
) -> str:
    """What their sessions left open and nothing has closed since: each session with its items,
    their kind and their ids. Items a later session settled are already gone. "yours" marks a
    step only they can take; ask for kind="step" when they want to know what needs them. Use it
    when they ask what is left to do, or to find the id of an item they say is done."""
    from . import serve

    from . import openitems

    req = {"days": days, "limit": OPEN_NOTES, "all": everything, "kinds": [kind] if kind else None}
    out = serve.open_items(_settings(), req)
    if not out["notes"]:
        return f"nothing is open from the last {out['days']} days" + (f" of kind {kind}" if kind else "")
    lines = [f"{sum(len(n['items']) for n in out['notes'])} open item(s) from {len(out['notes'])} session(s) in the last {out['days']} days"]
    if len(out["notes"]) == OPEN_NOTES:
        lines[0] += f" (the {OPEN_NOTES} most recent sessions; ask for fewer days or one kind to see further back)"
    for n in out["notes"]:
        lines.append(f"\n{(n['date'] or '')[:10]}  {n['title']}  ({n['folder'] or n['kind']}; {n['source']})")
        lines.extend(f"- ({openitems.KIND_NAMES.get(k, k)}) {text}  [{item_id}]" for item_id, text, k in zip(n["ids"], n["items"], n["kinds"]))
    return "\n".join(lines)


@tool(WRITE_SCOPE, title="Close an open item", annotations=ADD)
def close_open_item(
    item: Annotated[str, Field(description="The item's id from open_items or catch_up, open:xxxxxxxxxxxx.")],
    how: Annotated[Literal["done", "dropped"], Field(description="done: it was done. dropped: decided against, or it no longer applies.")] = "done",
    why: Annotated[str | None, Field(description="One sentence on what they said or did, in their words where possible.")] = None,
) -> str:
    """Take one item off the open list because they say it is done, decided against, or no
    longer applies. Close only what they told you; do not guess that something was done. The
    session note it came from is not changed, and reopen_open_item puts it back."""
    from . import openitems

    try:
        found = openitems.close_by_hand(_settings(), item, how, why or "", by="assistant")
    except (LookupError, ValueError) as exc:
        raise ToolError(str(exc)) from None
    return f"closed ({how}): {found.text}"


@tool(WRITE_SCOPE, title="Reopen an item", annotations=ADD)
def reopen_open_item(item: Annotated[str, Field(description="The id of a closed item, open:xxxxxxxxxxxx.")]) -> str:
    """Put an item back on the open list after it was closed by mistake."""
    from . import openitems

    if not openitems.ITEM_ID.match(item):
        raise ToolError("an open item's id looks like open:0123456789ab")
    return f"{item} is open again" if openitems.Store(_settings()).reopen(item) else f"{item} was not closed"


@tool(WRITE_SCOPE, title="Remember a fact", annotations=ADD)
def remember(
    fact: Annotated[str, Field(description=config.personal("One standalone sentence, e.g. '{Owner} runs niri on CachyOS.'"))],
    tags: Annotated[list[str] | None, Field(description="Optional topic tags.")] = None,
) -> str:
    """Store one durable fact (a preference, decision, correction, or fact about their setup)."""
    return _run("remember", {"fact": fact, "tags": tags})


@tool(FORGET_SCOPE, title="Forget a memory", annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
def forget(source: Annotated[str, Field(description="Memory id, memory:xxxxxxxxxxxx.")]) -> str:
    """Delete one memory by id, e.g. when a stored fact is wrong. Only memory:* ids."""
    return _run("forget", {"source": source})


@tool(READ_SCOPE, title="List memories", annotations=READ)
def list_memories(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> str:
    """The most recent memories with their ids."""
    return _run("list_memories", {"limit": limit})


@tool(READ_SCOPE, title="List sources", annotations=READ)
def list_sources() -> str:
    """Everything saved: each source with title, chunk count, tags, and id."""
    return _run("list_sources", {})


# Local only: it makes this machine fetch any address or read any file path it is given, so an
# online caller could use it to reach local services and files.
@tool(None, title="Save a URL or file", annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
def save_url_or_file(
    target: Annotated[str, Field(description="An http(s) URL or an absolute file path on the desktop.")],
    title: Annotated[str | None, Field(description="Override the title.")] = None,
    tags: Annotated[list[str] | None, Field(description="Topic tags.")] = None,
) -> str:
    """Fetch and save a web page, PDF, or local file so it becomes searchable.
    Saving the same URL or path again replaces the earlier copy."""
    return _run("save_source", {"target": target, "title": title, "tags": tags})


@tool(WRITE_SCOPE, title="Save a note", annotations=ADD)
def save_note(
    text: Annotated[str, Field(description="The note, snippet, or summary to keep.")],
    title: Annotated[str, Field(description="A short title.")],
    tags: Annotated[list[str] | None, Field(description="Topic tags.")] = None,
) -> str:
    """Save a piece of text as its own source, e.g. a decision summary or a snippet."""
    try:
        source, n = ingest.ingest_text(_settings(), text, title, tags)
    except (SystemExit, ValueError) as exc:
        raise ToolError(str(exc)) from None
    return f"stored {n} chunk(s) as {source}"


def format_events(rows: list[dict], limit: int) -> str:
    """Compact timeline, newest first. Notifications that share a title (a Claude session's
    progress updates, a feed) fold into one line with a count, the time span, and the latest
    detail; conversations stay separate with a short excerpt."""
    lines: list[tuple] = []           # (latest time, text)
    groups: dict[str, dict] = {}      # notification title -> {first, last, n, detail}
    for r in rows:                    # rows arrive newest first
        at = r["created_at"].astimezone()
        body = r["text"].split("\n", 1)[-1]
        if r["event"]["source"] != "desktop-notification":
            lines.append((at, f"{at:%m-%d %H:%M}  {r['title']}\n    {' / '.join(body.splitlines())[:240]}"))
            continue
        g = groups.get(r["title"])
        if g is None:
            detail = " ".join(body.splitlines()[1:])[:140]
            groups[r["title"]] = {"last": at, "first": at, "n": 1, "detail": detail}
        else:
            g["first"], g["n"] = at, g["n"] + 1
    for title, g in groups.items():
        span = f"{g['last']:%m-%d %H:%M}" if g["n"] == 1 else f"{g['first']:%m-%d %H:%M}-{g['last']:%H:%M}"
        count = f" x{g['n']}" if g["n"] > 1 else ""
        detail = f"  ({'latest: ' if g['n'] > 1 else ''}{g['detail']})" if g["detail"] else ""
        lines.append((g["last"], f"{span}  {title}{count}{detail}"))
    lines.sort(key=lambda x: x[0], reverse=True)
    out = [text for _, text in lines[:limit]]
    if len(lines) > limit:
        out.append(f"... {len(lines) - limit} older line(s) not shown; narrow `since` or raise `limit`")
    return "\n".join(out)


@tool(READ_SCOPE, title="Recent events", annotations=READ)
def recent_events(
    since: Annotated[str, Field(pattern=r"^\d+[mhd]$", description="Window like 90m, 24h, 7d.")] = "24h",
    source: Annotated[Literal["desktop-notification", "hermes"] | None, Field(description="Only this stream.")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Max lines after grouping.")] = 40,
) -> str:
    """A timeline of notifications and Hermes conversations, newest first, without a search.
    Repeated notifications fold into one line with a count. For 'what was I doing today' or
    'what did Hermes say earlier'; use search with include_events for a specific topic."""
    rows = ingest.recent_events(_settings(), mstore.parse_since(since), source, 1000)
    if not rows:
        return "no events in that window"
    return format_events(rows, limit)


@tool(READ_SCOPE, title="Machine metrics", annotations=READ)
def metrics_summary(
    since: Annotated[str, Field(pattern=r"^\d+[mhd]$", description="Window like 90m, 24h, 7d.")] = "24h",
    bucket: Literal["minute", "hour", "day"] = "hour",
    host: Annotated[str | None, Field(description="Hostname, e.g. desktop.")] = None,
) -> str:
    """CPU, load, memory and temperature of their machines, averaged per bucket."""
    return _run("metrics_summary", {"since": since, "bucket": bucket, "host": host})


@tool(READ_SCOPE, title="Manual metrics", annotations=READ)
def metrics_manual(
    name: Annotated[str | None, Field(description="e.g. sleep_hours; omit for all.")] = None,
    since: Annotated[str, Field(pattern=r"^\d+[mhd]$")] = "30d",
) -> str:
    """Hand-recorded metrics like sleep_hours or weight_kg."""
    return _run("metrics_manual", {"name": name, "since": since})


@tool(WRITE_SCOPE, title="Record a metric", annotations=ADD)
def record_metric(
    name: Annotated[str, Field(description="snake_case with unit, e.g. sleep_hours, weight_kg.")],
    value: float,
    note: str | None = None,
    at: Annotated[str | None, Field(description="ISO timestamp if not now.")] = None,
) -> str:
    """Record a measurement they state, e.g. 'slept 7 hours' -> sleep_hours=7."""
    return _run("record_metric", {"name": name, "value": value, "note": note, "at": at})


def main() -> None:
    mcp.run()


# ----- vault: online only --------------------------------------------------

VAULT_TEXT_CHARS = 20_000


def _vault_entry(query: str) -> dict:
    s = _settings()
    if query.startswith("vault:"):
        entry = vault.get_by_source(s, query)
        if entry is None:
            raise ToolError(f"no vault entry {query}")
        return entry
    hits = vault.find(s, query, limit=1)
    if not hits:
        raise ToolError("the vault is empty or nothing matches")
    return hits[0]


@tool(READ_SCOPE, online_only=True, title="List vault entries", annotations=READ)
def vault_list() -> str:
    """Titles of the encrypted vault entries (IDs, documents, account details), newest first.
    Only titles and notes; nothing is decrypted."""
    rows = vault.list_entries(_settings())
    if not rows:
        return "the vault is empty"
    return "\n".join(
        f"{r['source']}  {r['created_at']:%Y-%m-%d}  {r['title']}  ({(r.get('vault') or {}).get('filename') or 'text'}, "
        f"{(r.get('vault') or {}).get('size', 0):,} bytes)" for r in rows)


@tool(READ_SCOPE, online_only=True, title="Open a vault entry", annotations=READ)
def vault_get(
    query: Annotated[str, Field(description="Title words, or an exact vault:xxxxxxxxxxxx id from vault_list.")],
) -> str:
    """Decrypt one vault entry and return its text. Use only when the user asks for what it holds."""
    entry = _vault_entry(query.strip())
    try:
        data, text = vault.decrypt(entry)
    except vault.NoKey:
        raise ToolError("the vault key is not set up on this server") from None
    except SystemExit as exc:
        raise ToolError(str(exc)) from None
    v = entry["vault"]
    head = f"{entry['title']}  [{entry['source']}, {v.get('filename') or 'text'}, {v['size']:,} bytes, {v['content_type']}]"
    if not text:
        return f"{head}\nThis entry is a file with no text to show; open it on the desktop with `recall vault get {entry['source']} --out <file>`."
    more = f"\n[... {len(text) - VAULT_TEXT_CHARS:,} more characters ...]" if len(text) > VAULT_TEXT_CHARS else ""
    return f"{head}\n\n{text[:VAULT_TEXT_CHARS]}{more}"


@tool(WRITE_SCOPE, online_only=True, title="Save to the vault", annotations=ADD)
def vault_save(
    title: Annotated[str, Field(description="How they will look for it, e.g. 'Passport number'. Stored in plain text.")],
    text: Annotated[str, Field(description="The secret content. Stored encrypted only.")],
    note: Annotated[str | None, Field(description="Extra searchable words. Stored in plain text, so nothing secret.")] = None,
) -> str:
    """Encrypt and store a piece of text in the vault, when the user asks to keep something sensitive."""
    if not title.strip() or not text.strip():
        raise ToolError("a vault entry needs a title and some text")
    try:
        source = vault.store(_settings(), text.encode(), None, "text/plain", title.strip(), note, create_key=False)
    except vault.NoKey:
        raise ToolError("the vault key is not set up on this server") from None
    except SystemExit as exc:
        raise ToolError(str(exc)) from None
    return f"stored {source} ({title.strip()})"
