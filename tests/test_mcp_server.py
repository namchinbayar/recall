from datetime import datetime, timezone

import pytest
from mcp import Client

from recall import mcp_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_run(settings, name, args):
        seen.append((name, args))
        if args.get("query") == "boom":
            return "database error: nope", True
        return f"ok {name}", False

    monkeypatch.setattr(mcp_server, "_settings", lambda: "SETTINGS")
    monkeypatch.setattr(mcp_server.agent_tools, "run", fake_run)
    return seen


@pytest.mark.anyio
async def test_tool_list_and_hints():
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "profile", "catch_up", "search", "remember", "forget", "list_memories", "list_sources", "save_url_or_file",
        "save_note", "recent_events", "metrics_summary", "metrics_manual", "record_metric",
        "open_items", "close_open_item", "reopen_open_item",
    }
    assert tools["search"].annotations.read_only_hint is True
    assert tools["open_items"].annotations.read_only_hint is True
    assert tools["close_open_item"].annotations.destructive_hint is False
    assert tools["forget"].annotations.destructive_hint is True
    assert not any("vault" in name for name in tools)


@pytest.mark.anyio
async def test_search_passes_through_and_drops_nones(calls):
    async with Client(mcp_server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool("search", {"query": "niri", "include_events": True})
    assert result.content[0].text == "ok search_knowledge"
    assert calls == [("search_knowledge", {"query": "niri", "limit": 5, "include_events": True, "max_chars": 700})]


@pytest.mark.anyio
async def test_search_full_drops_the_excerpt_cap(calls):
    async with Client(mcp_server.mcp, raise_exceptions=True) as client:
        await client.call_tool("search", {"query": "niri", "full": True, "limit": 2})
    assert calls == [("search_knowledge", {"query": "niri", "limit": 2, "include_events": False})]


@pytest.mark.anyio
async def test_agent_error_becomes_tool_error(calls):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("search", {"query": "boom"})
    assert result.is_error and "database error" in result.content[0].text


@pytest.mark.anyio
async def test_since_is_validated(calls):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("recent_events", {"since": "yesterday"})
    assert result.is_error


def _note(title, detail, hour, minute=0):
    summary = title.split(": ", 1)[1]
    return {"title": title, "text": f"[ts] desktop-notification / x\n{summary}\n{detail}",
            "created_at": datetime(2026, 11, 10, hour, minute, tzinfo=timezone.utc),
            "event": {"source": "desktop-notification"}}


def test_format_events_groups_repeated_titles():
    rows = [  # newest first, as the query returns them
        _note("Claude: PC health check", "all good", 12, 30),
        {"title": "discord: hi", "text": "[ts] hermes / discord\nUser: hi\nHermes: hello",
         "created_at": datetime(2026, 11, 10, 12, tzinfo=timezone.utc), "event": {"source": "hermes"}},
        _note("Claude: PC health check", "checking disks", 11, 45),
        _note("Claude: PC health check", "started", 11, 0),
        _note("Tether: Anthropic", "usage report", 10),
    ]
    text = mcp_server.format_events(rows, 10)
    lines = text.splitlines()
    assert sum("PC health check" in l for l in lines) == 1
    assert "x3" in lines[0] and "latest: all good" in lines[0] and "checking disks" not in text
    assert "discord: hi" in lines[1] and "User: hi / Hermes: hello" in lines[2]
    assert "Tether: Anthropic" in lines[3] and "x" not in lines[3].split("Anthropic")[1].split("(")[0]


def test_format_events_limit_reports_what_is_hidden():
    rows = [_note(f"App: n{i}", "", 12, 59 - i) for i in range(5)]
    text = mcp_server.format_events(rows, 2)
    assert "n0" in text and "n1" in text and "n2" not in text
    assert "3 older line(s) not shown" in text


@pytest.mark.anyio
async def test_recent_events_tool(monkeypatch, calls):
    monkeypatch.setattr(mcp_server.ingest, "recent_events", lambda s, since, source, limit: [_note("Claude: Done", "ok", 9)])
    async with Client(mcp_server.mcp, raise_exceptions=True) as client:
        text = (await client.call_tool("recent_events", {})).content[0].text
    assert "Claude: Done" in text and "(ok)" in text


@pytest.mark.anyio
async def test_unconfigured_reports_cleanly(monkeypatch):
    mcp_server._settings.cache_clear()
    monkeypatch.setattr(mcp_server.config, "load", lambda: (_ for _ in ()).throw(SystemExit("MONGODB_URI is not set.")))
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("list_sources", {})
    assert result.is_error and "not configured" in result.content[0].text
    mcp_server._settings.cache_clear()


@pytest.mark.anyio
async def test_open_items_are_listed_with_ids_and_closed_only_by_a_real_id(monkeypatch):
    from recall import openitems, serve

    notes = [{"source": "session:a", "title": "Older work", "date": "2026-11-08T12:00:00+00:00", "folder": "~/x", "kind": "claude",
              "items": ["Nothing is pushed.", "Sign in on the phone."], "ids": ["open:0123456789ab", "open:ba9876543210"]}]
    monkeypatch.setattr(mcp_server, "_settings", lambda: None)
    monkeypatch.setattr(serve, "open_items", lambda settings, req: {"ok": True, "days": req["days"], "notes": notes if req["days"] > 1 else []})
    closed = []

    def close(settings, item, how, why, by):
        if item != "open:0123456789ab":
            raise LookupError(f"no session note of the last 90 days lists {item}")
        closed.append((item, how, why, by))
        return openitems.Item(item, "Nothing is pushed.", "session:a", "Older work", None)

    monkeypatch.setattr(openitems, "close_by_hand", close)
    async with Client(mcp_server.mcp) as client:
        listing = (await client.call_tool("open_items", {})).content[0].text
        assert listing.startswith("2 open item(s) from 1 session(s) in the last 14 days")
        assert "2026-11-08  Older work  (~/x; session:a)" in listing and "- Nothing is pushed.  [open:0123456789ab]" in listing
        assert (await client.call_tool("open_items", {"days": 1})).content[0].text == "nothing is open from the last 1 days"

        done = await client.call_tool("close_open_item", {"item": "open:0123456789ab", "why": "They pushed it this morning."})
        assert done.content[0].text == "closed (done): Nothing is pushed."
        assert closed == [("open:0123456789ab", "done", "They pushed it this morning.", "assistant")]
        missing = await client.call_tool("close_open_item", {"item": "open:ffffffffffff", "how": "dropped"})
        assert missing.is_error and "no session note" in missing.content[0].text
        bad = await client.call_tool("reopen_open_item", {"item": "memory:0123456789ab"})
        assert bad.is_error and "open:0123456789ab" in bad.content[0].text
