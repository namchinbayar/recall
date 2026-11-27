"""recall command line."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Annotated

import typer
from pymongo.errors import OperationFailure
from voyageai.error import RateLimitError

from . import config, db, ingest, search
from . import doctor as doctor_mod
from . import sessions as sessions_mod
from . import sync as sync_mod
from .metrics.cli import app as metrics_app
from .agent.chat import CALL_ERRORS, Session
from . import helptext
from .vault_cli import app as vault_app
from .stream.cli import app as stream_app

app = typer.Typer(
    help="Personal knowledge base on MongoDB Atlas with semantic search.",
    no_args_is_help=True,
)

app.add_typer(metrics_app, name="metrics")
app.add_typer(vault_app, name="vault")
app.add_typer(stream_app, name="stream")

Tags = Annotated[list[str] | None, typer.Option("--tag", "-t", help="Tag (repeatable).")]


@app.command(name="help")
def help_cmd() -> None:
    """Cheat sheet of everyday commands."""
    typer.echo(helptext.TEXT)


@app.command()
def init() -> None:
    """Create the vector search index on the chunks collection."""
    settings = config.load()
    typer.echo(db.ensure_indexes(settings))


@app.command()
def add(
    target: Annotated[str, typer.Argument(help="URL, file path, or '-' for stdin.")],
    title: Annotated[str | None, typer.Option("--title", help="Override the title.")] = None,
    tags: Tags = None,
) -> None:
    """Ingest a URL, file, or stdin. Re-adding the same source replaces it."""
    settings = config.load()
    source, n = ingest.ingest(settings, target, title=title, tags=tags)
    typer.echo(f"stored {n} chunk(s) from {source}")


@app.command(name="search")
def search_cmd(
    query: Annotated[str, typer.Argument(help="What are you looking for?")],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 5,
    tags: Tags = None,
    full: Annotated[bool, typer.Option("--full", help="Print whole chunks.")] = False,
    events: Annotated[bool, typer.Option("--events", "-e", help="Include stream events (notifications, Hermes).")] = False,
    since: Annotated[str | None, typer.Option("--since", help="From this date on, YYYY-MM-DD.")] = None,
    until: Annotated[str | None, typer.Option("--until", help="Up to and including this date, YYYY-MM-DD.")] = None,
) -> None:
    """Semantic search across everything stored (events only with --events or a --tag).
    Time words ("yesterday", "last week", "in October") limit it to that window."""
    from .agent.tools import day_bound

    settings = config.load()
    try:
        hits = search.search(settings, query, limit=limit, tags=tags, include_events=events, when=True,
                             since=day_bound(since), until=day_bound(until, end=True))
    except OperationFailure as exc:
        if "HostUnreachable" in str(exc) or exc.code == 6:
            raise SystemExit(
                "search is not ready yet: the Atlas search process or index is "
                "still starting. Give it a minute and try again."
            )
        raise
    if hits.window:
        typer.secho(f"time window: {hits.window.describe()}", fg="bright_black")
    if not hits:
        typer.echo("no results")
        raise typer.Exit()

    for i, hit in enumerate(hits, 1):
        tag_str = f"  [{', '.join(hit.tags)}]" if hit.tags else ""
        day = f"  {hit.created_at.astimezone():%Y-%m-%d}" if hit.created_at else ""
        typer.secho(f"{i}. {hit.title}  ({hit.score:.3f}){day}{tag_str}", bold=True)
        typer.secho(f"   {hit.source} #{hit.chunk_index}", fg="bright_black")
        body = hit.text if full else textwrap.shorten(hit.text, 300, placeholder=" ...")
        typer.echo(textwrap.indent(body, "   "))
        typer.echo()


@app.command(name="eval")
def eval_cmd(
    file: Annotated[Path | None, typer.Option("--file", help="Question set (default ~/.config/recall/eval.toml).")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many results count as found.")] = 5,
    no_rerank: Annotated[bool, typer.Option("--no-rerank", help="Rank by retrieval order alone.")] = False,
    pool: Annotated[int | None, typer.Option("--pool", help="Candidates the reranker reads.")] = None,
    save: Annotated[bool, typer.Option("--save", help="Keep this run as the baseline later runs are compared with.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="List every question, not only the ones that went wrong.")] = False,
) -> None:
    """Ask every question in the question set and report how often search found the answer."""
    from . import evals

    cases = evals.load(file)
    settings = config.load()
    rerank = not no_rerank and bool(settings.rerank_model)
    how = {"limit": limit, "rerank": settings.rerank_model if rerank else None, "pool": pool, "model": settings.voyage_model}
    typer.secho(f"{len(cases)} questions, top {limit}, embeddings {settings.voyage_model}, "
                f"rerank {how['rerank'] or 'off'}" + (f", pool {pool}" if pool else ""), fg="bright_black")

    ignore = evals.load_ignore(file)

    def ask(case: evals.Case) -> list:
        return search.search(settings, case.q, limit=limit + len(ignore), tags=list(case.tags) or None,
                             include_events=case.events, rerank=rerank, pool=pool, when=True)

    with typer.progressbar(length=len(cases), label="asking") as bar:
        outcomes = evals.run(cases, ask, evals.stored_sources(settings), progress=lambda i: bar.update(1),
                             ignore=ignore, limit=limit)
    baseline = evals.load_baseline()
    for line in evals.report(outcomes, limit, baseline, verbose):
        typer.echo(line)
    if not rerank:
        typer.secho("without the reranker a score is a place in the list, not relevance: "
                    "ignore the quiet questions in this run", fg="bright_black")
    if save:
        evals.save_baseline(evals.snapshot(outcomes, how))
        typer.secho(f"saved as the baseline ({evals.BASELINE})", fg="bright_black")
    elif baseline is None:
        typer.secho("no baseline yet: run it again with --save to keep this one", fg="bright_black")


@app.command(name="list")
def list_cmd() -> None:
    """List stored sources."""
    settings = config.load()
    rows = ingest.list_sources(settings)
    if not rows:
        typer.echo("nothing stored yet")
        raise typer.Exit()
    for row in rows:
        tag_str = f"  [{', '.join(row['tags'])}]" if row.get("tags") else ""
        typer.echo(f"{row['chunks']:>4}  {row['title']}{tag_str}")
        typer.secho(f"      {row['_id']}", fg="bright_black")


def _api_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0][:200]}"


@app.command()
def ask(
    question: Annotated[str, typer.Argument()],
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Hide tool calls.")] = False,
    no_save: Annotated[bool, typer.Option("--no-save", help="Do not store the session.")] = False,
) -> None:
    """One question to the agent, with tools."""
    settings = config.load()
    session = Session.start(settings, show_tools=not quiet)
    try:
        session.turn(question)
    except CALL_ERRORS as exc:
        raise SystemExit(f"the model call failed: {_api_error(exc)}") from None
    if not no_save:
        session.save()


@app.command()
def chat(
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Hide tool calls.")] = False,
    no_save: Annotated[bool, typer.Option("--no-save", help="Skip the memory wrap-up and session save.")] = False,
) -> None:
    """Interactive conversation with the agent. Ctrl-D or /quit to end."""
    settings = config.load()
    session = Session.start(settings, show_tools=not quiet)
    typer.secho(f"recall chat  ({session.model}, effort {session.effort}). Ctrl-D or /quit to end.", fg="bright_black")
    while True:
        try:
            line = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo()
            break
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            break
        typer.echo()
        try:
            session.turn(line)
        except KeyboardInterrupt:
            typer.echo("\n[interrupted]")
        except CALL_ERRORS as exc:   # the chat goes on; what was said so far is kept
            typer.echo(f"\n[the model call failed: {_api_error(exc)}]")
    if not no_save and session.messages:
        typer.secho("\nwrapping up...", fg="bright_black")
        try:
            session.wrap_up()
            typer.secho("session saved", fg="bright_black")
        except (*CALL_ERRORS, KeyboardInterrupt) as exc:
            session.save()      # no summary and no last memories, but the conversation is not lost
            why = "interrupted" if isinstance(exc, KeyboardInterrupt) else _api_error(exc)
            typer.secho(f"session saved without a wrap-up ({why})", fg="bright_black")


@app.command(name="sync")
def sync_cmd(
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Show what would change, change nothing.")] = False,
    prune: Annotated[bool, typer.Option("--prune", help="Also remove files whose watch entry left sync.toml.")] = False,
    loop: Annotated[bool, typer.Option("--loop", help="Keep running and re-check on an interval.")] = False,
    interval: Annotated[int, typer.Option("--interval", help="Seconds between checks with --loop.")] = 60,
    config_path: Annotated[Path | None, typer.Option("--config", help="Sync config (default ~/.config/recall/sync.toml).")] = None,
) -> None:
    """Save new and changed files from the folders in sync.toml; remove the ones that are gone."""
    settings = config.load()
    if loop:
        sync_mod.run_loop(settings, interval, config_path, typer.echo, prune)
        return
    todo = sync_mod.plan(sync_mod.load(config_path), sync_mod.stored(settings))
    for line in sync_mod.report(todo):
        typer.echo(line)
    gone = len(todo.remove) + (len(todo.orphans) if prune else 0)
    if dry_run:
        typer.echo(f"dry run: {len(todo.save)} to save, {gone} to remove, {todo.unchanged} unchanged, {len(todo.skipped)} skipped")
        return
    saved, removed, failed = sync_mod.apply(settings, todo, prune)
    for path, why in failed:
        typer.echo(f"  failed   {path}  ({why})")
    typer.echo(f"sync: {saved} saved, {removed} removed, {todo.unchanged} unchanged, {len(todo.skipped)} skipped"
               + (f", {len(failed)} failed" if failed else ""))
    if failed:
        raise typer.Exit(1)


@app.command(name="sessions")
def sessions_cmd(
    since: Annotated[str | None, typer.Option("--since", help="Also take sessions last changed since then: 36h, 30d, or 2026-10-01.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Show what would be summarized and the estimated cost.")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="At most this many sessions in one run.")] = None,
    idle: Annotated[int, typer.Option("--idle", help="Minutes a transcript must be quiet before it is summarized.")] = sessions_mod.IDLE_MINUTES,
    loop: Annotated[bool, typer.Option("--loop", help="Keep running and re-check on an interval.")] = False,
    interval: Annotated[int, typer.Option("--interval", help="Seconds between checks with --loop.")] = 300,
) -> None:
    """Save a short note (done, decided, still open) for each finished Claude Code session."""
    from datetime import datetime, timezone

    settings = config.load()
    if loop:
        sessions_mod.run_loop(settings, interval, idle, typer.echo)
        return
    now = datetime.now(timezone.utc)
    start = sessions_mod.parse_since(since, now) if since else None
    if dry_run:
        state = sessions_mod.load_state()
        todo = sessions_mod.plan(sessions_mod.candidates(), state,
                                 start or datetime.fromisoformat(state.get("start", now.isoformat())), now, idle)
        import os
        model = os.environ.get("RECALL_SUMMARY_MODEL") or os.environ.get("RECALL_MODEL", "claude-opus-5-5")
        todo.due = todo.due[:limit]
        n, tokens, cost = sessions_mod.estimate(todo, state, model)
        price = "" if cost is None else f", about ${cost:.2f} on {model}"
        typer.echo(f"dry run: {len(todo.due)} quiet session(s) to read, {n} with enough said to summarize "
                   f"(about {tokens:,} tokens in{price}); {len(todo.active)} still active, "
                   f"{todo.unchanged} unchanged, {todo.older} older than the cutoff")
        return
    summarize, usage, model = sessions_mod.make_summarizer()
    counts = sessions_mod.run_pass(settings, summarize, typer.echo, start, idle, limit)
    done = ", ".join(f"{n} {what}" for what, n in sorted(counts.items())) or "nothing to do"
    typer.echo(f"sessions: {done} ({usage['calls']} model call(s), {usage['input']:,} tokens in, {usage['output']:,} out, {model})")
    if counts.get("failed"):
        raise typer.Exit(1)


@app.command(name="open")
def open_cmd(
    days: Annotated[int, typer.Option("--days", help="How far back to look for open items.")] = 14,
    closed: Annotated[bool, typer.Option("--closed", help="List the latest closures: what was settled, by which session, and why.")] = False,
    done: Annotated[str | None, typer.Option("--done", help="Close one item by hand (its open:… id).")] = None,
    why: Annotated[str | None, typer.Option("--why", help="With --done: the reason to record.")] = None,
    reopen: Annotated[str | None, typer.Option("--reopen", help="Put a closed item back on the list.")] = None,
    settle: Annotated[bool, typer.Option("--settle", help="Ask which open items the session notes since --since settled.")] = False,
    since: Annotated[str, typer.Option("--since", help="With --settle: notes since then, 36h, 30d or 2026-10-01.")] = "3d",
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="With --settle: how many notes would be asked about, and the estimated cost.")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="With --settle: at most this many notes.")] = None,
) -> None:
    """What sessions left open and no later session settled. The sessions service keeps it current."""
    from datetime import datetime, timedelta, timezone

    from . import openitems, serve

    settings = config.load()
    store = openitems.Store(settings)
    now = datetime.now(timezone.utc)
    if reopen:
        typer.echo(f"{reopen} is open again" if store.reopen(reopen) else f"{reopen} was not closed")
        return
    if done:
        found = [i for s in openitems.read_notes(serve._note_chunks(settings, now - timedelta(days=90)))
                 for i in s.items if i.id == done]
        if not found:
            raise SystemExit(f"no open item {done} in the session notes of the last 90 days")
        store.close(found[0], "done", why or "closed by hand", "hand", now)
        typer.echo(f"closed: {found[0].text}")
        return
    if settle:
        start = sessions_mod.parse_since(since, now)
        if dry_run:
            counts = openitems.settle_pending(settings, store, None, start, typer.echo, now, limit)
            model = openitems_model()
            price = sessions_mod.PRICES.get(model)
            tokens = int(counts["chars"] / openitems.CHARS_PER_TOKEN) + counts["asked"] * openitems.REVIEW_TOKENS
            cost = "" if price is None else f", about ${(tokens * price[0] + counts['asked'] * openitems.REPLY_TOKENS * price[1]) / 1e6:.2f} on {model}"
            typer.echo(f"dry run: {counts['asked']} note(s) to ask about (about {tokens:,} tokens in{cost}); "
                       f"{counts['nothing']} with no earlier open item to settle")
            return
        settler = openitems.Settler(settings)
        counts = settler.run(typer.echo, start, limit)
        typer.echo(f"open items: {counts['closed']} closed by {counts['asked']} note(s), {counts['nothing']} with nothing to settle, "
                   f"{counts['failed']} failed ({settler.usage['calls']} model call(s), {settler.usage['input']:,} tokens in, "
                   f"{settler.usage['output']:,} out, {settler.model})")
        if counts["failed"]:
            raise typer.Exit(1)
        return
    if closed:
        rows = store.recent(40)
        if not rows:
            typer.echo("nothing was closed yet")
        for r in rows:
            by = "by hand" if r.get("closed_by") == "hand" else r.get("closed_by", "?")
            typer.secho(f"{r['closed_at'].astimezone():%Y-%m-%d}  {r.get('how', 'done'):<8} {r['text'][:110]}", bold=True)
            typer.echo(f"            {r.get('why', '')}")
            if r.get("quote"):
                typer.secho(f"            \"{r['quote'][:150]}\"", fg="bright_black")
            typer.secho(f"            {r['_id']}  from {r.get('title', r.get('source', '?'))[:60]}  closed {by}", fg="bright_black")
        return
    out = serve.open_items(settings, {"days": days, "limit": 100}, now)
    total = sum(len(n["items"]) for n in out["notes"])
    typer.secho(f"{total} open item(s) from {len(out['notes'])} session(s) in the last {out['days']} days", fg="bright_black")
    for n in out["notes"]:
        typer.secho(f"\n{(n['date'] or '')[:10]}  {n['title']}  ({n['folder'] or n['kind']})", bold=True)
        for item_id, text in zip(n["ids"], n["items"]):
            typer.echo(textwrap.fill(text, 100, initial_indent="  - ", subsequent_indent="    "))
            typer.secho(f"    {item_id}", fg="bright_black")


def openitems_model() -> str:
    from . import learn

    return learn.model_name()


@app.command(name="learn")
def learn_cmd(
    since: Annotated[str | None, typer.Option("--since", help="Also take Hermes exchanges stored since then: 36h, 30d, or 2026-10-01.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Show which conversations would be read and the estimated cost.")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="At most this many conversations in one run.")] = None,
    idle: Annotated[int, typer.Option("--idle", help="Minutes a conversation must be quiet before it is read.")] = 20,
    profile: Annotated[bool, typer.Option("--profile", help="Rewrite the profile now, then exit.")] = False,
    loop: Annotated[bool, typer.Option("--loop", help="Keep running and re-check on an interval.")] = False,
    interval: Annotated[int, typer.Option("--interval", help="Seconds between checks with --loop.")] = 300,
) -> None:
    """Read quiet Hermes conversations: keep lasting facts, retire replaced ones, write a record."""
    from datetime import datetime, timezone

    from . import learn

    settings = config.load()
    if loop:
        learn.run_loop(settings, interval, idle, typer.echo)
        return
    now = datetime.now(timezone.utc)
    start = sessions_mod.parse_since(since, now) if since else None
    store = learn.Store(settings)
    store.ensure()
    if dry_run:
        new = learn.import_events(store, now, start)
        pending = store.pending()
        states = {r["session_id"]: store.get_state(f"session:{r['session_id']}") for r in pending}
        ready, active, waiting = learn.due(pending, states, now, idle)
        ready = ready[:limit]
        model = learn.model_name()
        tokens, cost = learn.estimate(store, ready, model)
        price = "" if cost is None else f", about ${cost:.2f} on {model}"
        typer.echo(f"dry run: {new} exchange(s) copied in; {len(ready)} quiet conversation(s) to read "
                   f"({sum(r['turns'] for r in ready)} exchanges, about {tokens:,} tokens in{price}); "
                   f"{active} still active, {waiting} waiting after a failed call")
        return
    learner, usage, model = learn.make_learner()
    if profile:
        typer.echo(f"profile: {learn.refresh_profile(store, learner, now, force=True)}")
        return
    counts = learn.run_pass(store, learner, typer.echo, start, idle, limit, now)
    did = learn.refresh_profile(store, learner, now)
    done = ", ".join(f"{n} {what}" for what, n in sorted(counts.items())) or "nothing to read"
    typer.echo(f"learn: {done}; profile {did} ({usage['calls']} model call(s), {usage['input']:,} tokens in, "
               f"{usage['output']:,} out, {model})")
    if counts.get("failed"):
        raise typer.Exit(1)


@app.command(name="learned")
def learned_cmd(
    retired: Annotated[bool, typer.Option("--retired", help="List memories that a newer fact replaced.")] = False,
    restore: Annotated[str | None, typer.Option("--restore", help="Bring a retired memory back: memory:xxxxxxxxxxxx.")] = None,
    show_profile: Annotated[bool, typer.Option("--profile", help="Print the profile Hermes is given.")] = False,
    limit: Annotated[int, typer.Option("--limit", help="How many to list.")] = 20,
) -> None:
    """What recall learned from Hermes conversations, and what it retired."""
    from . import learn
    from .agent import memory

    settings = config.load()
    if restore:
        n = memory.restore(settings, restore)
        typer.echo(f"restored {restore}" if n else f"nothing retired under {restore}")
        return
    if show_profile:
        doc = learn.profile(settings)
        typer.echo(f"{doc['text']}\n\n(written {doc['written'].astimezone():%Y-%m-%d %H:%M} from {doc.get('sources', '?')} "
                   f"notes and memories)" if doc else "no profile yet; `recall learn --profile` writes one")
        return
    if retired:
        rows = memory.retired(settings, limit)
        for r in rows:
            mark = r["retired"]
            by = f" by {mark['by']}" if mark.get("by") else ""
            typer.echo(f"{r['source']}  retired {mark['at'].astimezone():%Y-%m-%d}{by} ({mark['why']})\n    {r['text']}")
        typer.echo(f"{len(rows)} retired; bring one back with `recall learned --restore <id>`" if rows else "nothing retired")
        return
    rows = [r for r in memory.recent(settings, 500) if "learned" in r["tags"]][:limit]
    for r in rows:
        typer.echo(f"{r['source']}  {r['created_at'].astimezone():%Y-%m-%d}  {r['text']}")
    typer.echo(f"{len(rows)} learned fact(s); remove a wrong one with the `forget` tool or `recall rm <id>`" if rows
               else "nothing learned yet")


@app.command(name="catch-up")
def catch_up_cmd(
    days: Annotated[int, typer.Option("--days", "-d", help="How far back to look.")] = 7,
    no_profile: Annotated[bool, typer.Option("--no-profile", help="Leave out the profile.")] = False,
) -> None:
    """The profile, then every Claude Code and Hermes session of the last few days with what it left open."""
    from . import serve

    typer.echo(serve.catch_up(config.load(), days, with_profile=not no_profile))


@app.command(name="doctor")
def doctor_cmd(
    alert: Annotated[bool, typer.Option("--alert", help="Send a failure to the phone (Bark) the second time it is seen.")] = False,
    loop: Annotated[bool, typer.Option("--loop", help="Keep running and re-check on an interval.")] = False,
    interval: Annotated[int, typer.Option("--interval", help="Seconds between checks with --loop.")] = 600,
    test_alert: Annotated[bool, typer.Option("--test-alert", help="Send one test message the way a real alert would go, then exit.")] = False,
) -> None:
    """Check every part of recall: services, database, search, embeddings, vault key."""
    if test_alert:
        how = doctor_mod.push("recall: test alert", "This is what a recall problem will look like. Nothing is wrong.")
        typer.echo(f"test alert sent by: {how}")
        raise typer.Exit(0 if how == "bark" else 1)
    settings = config.load()
    if loop:
        doctor_mod.run_loop(settings, interval, alert, typer.echo)
        return
    checks = doctor_mod.once(settings, alert, typer.echo)
    for line in doctor_mod.report(checks):
        typer.echo(line)
    failed = sum(c.failed for c in checks)
    typer.echo("all good" if not failed else f"{failed} problem{'s' if failed > 1 else ''}")
    if failed:
        raise typer.Exit(1)


@app.command(name="serve")
def serve_cmd(
    socket: Annotated[Path | None, typer.Option("--socket", help="Socket path (default $XDG_RUNTIME_DIR/recall.sock).")] = None,
) -> None:
    """Answer context requests on a local socket (for Hermes's memory provider)."""
    from . import serve

    serve.run(config.load(), socket, typer.echo)


@app.command(name="mcp")
def mcp_cmd() -> None:
    """Run recall as an MCP server on stdio (clients start this themselves)."""
    from .mcp_server import main as serve

    serve()


@app.command(name="mcp-online")
def mcp_online_cmd() -> None:
    """Run recall as an online MCP server (HTTP on a local port, behind a sign-in)."""
    from .online import main as serve

    serve()


@app.command()
def rm(source: Annotated[str, typer.Argument(help="Exact source id from `recall list`.")]) -> None:
    """Remove a source and all its chunks."""
    settings = config.load()
    n = ingest.remove(settings, source)
    typer.echo(f"removed {n} chunk(s)")


def main() -> None:
    try:
        app()
    except RateLimitError as exc:
        raise SystemExit(f"voyage rate limit: {str(exc).splitlines()[0]}")


if __name__ == "__main__":
    main()
