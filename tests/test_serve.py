import importlib.util
import json
import re
import sys
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from recall import serve
from recall.search import Hit

PLUGIN = Path(__file__).parents[1] / "contrib" / "hermes-memory" / "recall" / "__init__.py"


def hit(title, text, score, tags=(), day=9, session=None):
    return Hit(source=f"x:{title}", title=title, chunk_index=0, text=text, score=score, tags=list(tags),
               created_at=datetime(2026, 11, day, 12, tzinfo=timezone.utc), session=session)


HITS = [
    hit("Session: syncd stopped", "syncd was failing in a loop and is now disabled. " * 30, 0.91, ["session-note"]),
    hit("Sam runs niri on CachyOS", "Sam runs niri on CachyOS.", 0.74, ["memory"], day=10),
    hit("Hermes MEMORY.md", "notes Hermes already has in its prompt", 0.70, ["hermes-memory"]),
    hit("Unrelated README", "something else entirely", 0.21, ["recall"]),
]


def find(settings, query, **kw):
    find.calls.append((query, kw))
    return list(HITS)


find.calls = []
PROFILE = "## How to work with him\n- Uses he/him. Prefers short answers."
KEPT: list[dict] = []


def test_context_keeps_relevant_hits_as_short_excerpts():
    out = serve.context(None, {"query": "  why did   syncd stop? ", "exclude_tags": ["hermes-memory"]}, find)
    assert out["ok"] and out["count"] == 2 and out["scores"] == [0.91, 0.74]
    first, second = out["text"].splitlines()
    assert first.startswith("- Session: syncd stopped, 2026-11-09: syncd was failing") and len(first) < 480
    assert second == "- memory, 2026-11-10: Sam runs niri on CachyOS."
    query, kw = find.calls[-1]
    assert query == "why did syncd stop?" and kw["pool"] == serve.POOL and kw["include_events"] is False
    assert kw["exclude_tags"] == ["hermes-memory"]          # filtered in the search, not only afterwards


def test_context_recalls_other_hermes_chats_but_not_the_asking_one():
    chats = lambda *a, **kw: [
        hit("local: Me: which terminal", "[2026-11-09 12:00 UTC] hermes / local\nMe: which terminal do I use?\nHermes: WezTerm.",
            0.9, ["event", "hermes"], session="this-chat"),
        hit("photon: Me: which terminal", "[2026-11-08 12:00 UTC] hermes / photon\nMe: which terminal again?\nHermes: WezTerm, since October.",
            0.8, ["event", "hermes"], day=8, session="older-chat"),
    ]
    out = serve.context(None, {"query": "which terminal", "include_events": True, "session_id": "this-chat"}, chats)
    assert out["count"] == 1
    assert out["text"] == "- earlier Hermes chat, 2026-11-08: Me: which terminal again? Hermes: WezTerm, since October."
    assert serve.context(None, {"query": "which terminal", "include_events": True}, chats)["count"] == 2


def test_context_honours_limits_and_bad_values():
    assert serve.context(None, {"query": "x", "min_score": 0.1}, find)["count"] == 4
    assert serve.context(None, {"query": "x", "min_score": 0.1, "limit": 1}, find)["count"] == 1
    assert serve.context(None, {"query": "x", "min_score": "high", "limit": "many", "max_chars": 5}, find)["count"] == 3
    nothing = {"ok": True, "count": 0, "text": "", "scores": [], "ids": [], "items": []}
    assert serve.context(None, {"query": "   "}, find) == nothing
    assert serve.context(None, {"query": "x", "min_score": 0.99}, find) == nothing
    many = lambda *a, **kw: [hit("Same session (2026-11-09)", f"part {i}", 0.9 - i / 100, ["session-note"]) for i in range(5)]
    out = serve.context(None, {"query": "x"}, many)
    assert out["count"] == 2 and out["scores"] == [0.9, 0.89]                 # two excerpts per source at most
    assert out["text"].splitlines()[0] == "- Same session (2026-11-09): part 0"   # no second date


def test_context_gives_a_conversation_each_item_once():
    first = serve.context(None, {"query": "why did syncd stop?", "min_score": 0.1, "limit": 2}, find)
    assert first["ids"] == ["x:Session: syncd stopped#0", "x:Sam runs niri on CachyOS#0"]
    again = serve.context(None, {"query": "why did syncd stop?", "min_score": 0.1, "limit": 2, "seen": first["ids"]}, find)
    assert again["ids"] == ["x:Hermes MEMORY.md#0", "x:Unrelated README#0"]        # the next ones, not the same two
    _, kw = find.calls[-1]
    assert kw["limit"] == 6                                                         # asks for more, to have some left
    everything = first["ids"] + again["ids"]
    assert serve.context(None, {"query": "why did syncd stop?", "min_score": 0.1, "seen": everything}, find)["count"] == 0


def test_context_reads_a_short_follow_up_with_the_message_before_it():
    serve.context(None, {"query": "and the other one?", "previous": "Which monitor is the main one on the desktop?"}, find)
    assert find.calls[-1][0] == "Which monitor is the main one on the desktop? and the other one?"
    long = "now tell me how the folder sync decides which files to skip and why"
    serve.context(None, {"query": long, "previous": "Which monitor is the main one on the desktop?"}, find)
    assert find.calls[-1][0] == long                                                # says enough by itself
    serve.context(None, {"query": "same again", "previous": "same again"}, find)
    assert find.calls[-1][0] == "same again"
    assert serve.context(None, {"query": "why did syncd stop?", "min_score": 0.72}, find)["scores"] == [0.91, 0.74]
    assert serve.context(None, {"query": "and why?", "min_score": 0.72}, find)["scores"] == [0.91]    # alone, a short one needs 0.82


def test_context_says_which_memories_were_learned():
    facts = lambda *a, **kw: [hit("Sam's terminal is WezTerm", "Sam's terminal is WezTerm.", 0.9, ["hermes", "learned", "memory"], day=12),
                              hit("Sam runs niri", "Sam runs niri.", 0.8, ["memory"], day=10)]
    text = serve.context(None, {"query": "which terminal"}, facts)["text"]
    assert text == "- learned from a chat, 2026-11-12: Sam's terminal is WezTerm.\n- memory, 2026-11-10: Sam runs niri."


def test_turn_keeps_a_whole_exchange_but_not_one_with_a_secret():
    kept = []
    record = lambda doc: kept.append(doc) or "new"
    req = {"message": " why is the sink restarting? ", "response": "x" * 9_000, "session_id": "s1", "platform": "cli"}
    assert serve.turn(None, req, record) == {"ok": True, "kept": "new"}
    assert kept[0]["message"] == "why is the sink restarting?" and len(kept[0]["response"]) == 9_000    # not cut to 500
    assert kept[0]["session_id"] == "s1" and kept[0]["platform"] == "cli" and kept[0]["learned"] is False
    assert serve.turn(None, {"message": "hi", "response": ""}, record) == {"ok": True, "kept": "empty"}
    leak = {"message": "use this", "response": "export ANTHROPIC_API_KEY=sk-ant-api03-" + "a" * 40, "session_id": "s1"}
    assert serve.turn(None, leak, record) == {"ok": True, "kept": "secret"} and len(kept) == 1


def test_profile_is_empty_until_one_is_written():
    assert serve.profile(None, lambda s: None) == {"ok": True, "text": ""}
    doc = {"text": "## Setup\n- WezTerm", "written": datetime(2026, 11, 14, 12, tzinfo=timezone.utc)}
    assert serve.profile(None, lambda s: doc) == {"ok": True, "text": "## Setup\n- WezTerm", "written": "2026-11-14T12:00:00+00:00"}


def test_answer_never_raises():
    assert serve.answer(None, b'{"op": "ping"}') == {"ok": True}
    assert serve.answer(None, b"not json")["ok"] is False
    assert serve.answer(None, b"[1, 2]")["ok"] is False
    assert "unknown op" in serve.answer(None, b'{"op": "drop"}')["error"]

    def broken(*a, **kw):
        raise RuntimeError("atlas is down")

    assert serve.answer(None, b'{"op": "context", "query": "x"}', broken) == {"ok": False, "error": "RuntimeError: atlas is down"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A real `recall serve` on a temporary socket, with search replaced by `find`."""
    monkeypatch.setattr(serve.search, "search", find)
    monkeypatch.setattr(serve, "profile", lambda settings: {"ok": True, "text": PROFILE})
    monkeypatch.setattr(serve, "turn", lambda settings, req: KEPT.append(req) or {"ok": True, "kept": "new"})
    monkeypatch.setattr(serve, "status", lambda settings: {
        "ok": True, "memories": 12, "learned": 5, "retired": 2, "unread_turns": 3,
        "last_read": "2026-11-14T19:00:00+00:00", "profile_written": None})
    KEPT.clear()
    path = tmp_path / "recall.sock"
    threading.Thread(target=serve.run, args=(None, path, lambda line: None), daemon=True).start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.02)
    assert path.stat().st_mode & 0o777 == 0o600
    return path


@pytest.fixture
def plugin(monkeypatch):
    """The Hermes plugin, loaded against a stand-in for Hermes's own base module."""
    base = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    class RecallStatus:
        def __init__(self, provider_label, count):
            self.provider_label, self.count = provider_label, count

    base.MemoryProvider, base.RecallStatus = MemoryProvider, RecallStatus
    base.is_trivial_prompt = lambda text: not (text or "").strip() or bool(re.fullmatch(r"(ok|yes|thanks)[.!]*", text.strip(), re.I))
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.memory_provider", base)
    spec = importlib.util.spec_from_file_location("hermes_recall_plugin", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def provider(plugin, tmp_path, sock, **settings):
    (tmp_path / "recall.json").write_text(json.dumps({"socket": str(sock), "timeout": 2, **settings}))
    p = plugin.RecallMemoryProvider()
    p.initialize("s1", hermes_home=str(tmp_path), platform="cli")
    return p


def test_plugin_recalls_through_the_socket(server, plugin, tmp_path):
    p = provider(plugin, tmp_path, server)
    text = p.prefetch("why did syncd stop?")
    assert text.startswith("From recall") and "syncd was failing" in text and "Hermes MEMORY.md" not in text
    assert p.recall_status().count == 2 and p.recall_status().provider_label == "recall"
    assert "# recall" in p.system_prompt_block() and p.get_tool_schemas() == [] and p.name == "recall"
    p.prefetch("why did syncd stop?", session_id="20261111_120000_abc")
    _, kw = find.calls[-1]                                  # Hermes chats on, notifications and its own memory files off
    assert kw["include_events"] is True and kw["exclude_tags"] == ["desktop-notification", "digest", "hermes-memory"]

    assert p.prefetch("thanks!") == "" and p.recall_status() is None       # small talk: no search, no stale indicator
    strict = provider(plugin, tmp_path, server, min_score=0.99)
    assert strict.prefetch("why did syncd stop?") == "" and strict.recall_status() is None


def test_plugin_fails_open_and_stays_quiet_for_helpers(server, plugin, tmp_path):
    down = provider(plugin, tmp_path, tmp_path / "missing.sock")
    assert down.prefetch("why did syncd stop?") == ""
    cron = plugin.RecallMemoryProvider()
    cron.initialize("s2", hermes_home=str(tmp_path), agent_context="cron")
    assert cron.prefetch("why did syncd stop?") == "" and cron.system_prompt_block() == ""
    seen = []
    plugin.register(types.SimpleNamespace(register_memory_provider=seen.append))
    assert isinstance(seen[0], plugin.RecallMemoryProvider)
    assert plugin.load_config(str(tmp_path / "nowhere"))["limit"] == 4


def test_store_is_refused_unless_the_instance_turns_it_on(monkeypatch):
    monkeypatch.delenv("RECALL_SERVE_STORE", raising=False)
    saved = []
    reply = serve.answer(None, json.dumps({"op": "store", "message": "hi", "response": "hello"}), save=lambda s, ev: saved.append(ev) or 1)
    assert reply["ok"] is False and "RECALL_SERVE_STORE" in reply["error"] and saved == []


def test_store_saves_one_exchange_as_a_hermes_event(monkeypatch):
    monkeypatch.setenv("RECALL_SERVE_STORE", "1")
    saved = []
    req = {"op": "store", "message": " what is my plan? ", "response": "x" * 9000, "session_id": "s1", "platform": "cli"}
    reply = serve.answer(None, json.dumps(req), save=lambda s, ev: saved.append(ev) or len(ev))
    assert reply == {"ok": True, "stored": 1}
    (event,) = saved[0]
    assert event["source"] == "hermes" and event["kind"] == "cli"
    assert event["body"].startswith("Me: what is my plan?\nHermes: xxx") and len(event["body"]) < 6100
    assert json.loads(event["meta"]) == {"session_id": "s1", "platform": "cli"}
    assert serve.answer(None, json.dumps({"op": "store", "message": " ", "response": ""}), save=lambda s, ev: 1) == {"ok": True, "stored": 0}


def test_plugin_does_not_repeat_itself_within_a_conversation(server, plugin, tmp_path):
    p = provider(plugin, tmp_path, server)
    first = p.prefetch("why did syncd stop?", session_id="chat-1")
    assert "syncd was failing" in first and "niri" in first and p.recall_status().count == 2
    assert p.prefetch("why did syncd stop?", session_id="chat-1") == "" and p.recall_status() is None
    assert "syncd was failing" in p.prefetch("why did syncd stop?", session_id="chat-2")     # another conversation has not seen it
    p.on_session_switch("chat-1", rewound=True)                                                   # cut back: it may get it again
    assert "syncd was failing" in p.prefetch("why did syncd stop?", session_id="chat-1")
    for i in range(6):
        p.prefetch("why did syncd stop?", session_id=f"other-{i}")
    assert len(p._seen) == plugin.KEPT_CONVERSATIONS


def test_plugin_sends_the_message_before_a_follow_up(server, plugin, tmp_path):
    p = provider(plugin, tmp_path, server)
    p.on_turn_start(2, "and the other one?", previous_message="Which monitor is the main one on the desktop?")
    p.prefetch("and the other one?", session_id="chat-1")
    assert find.calls[-1][0] == "Which monitor is the main one on the desktop? and the other one?"
    p.on_session_switch("chat-9", reset=True)
    p.prefetch("and the other one?", session_id="chat-9")
    assert find.calls[-1][0] == "and the other one?"


def test_plugin_hands_each_exchange_over_whole_and_carries_the_profile(server, plugin, tmp_path):
    p = provider(plugin, tmp_path, server)
    block = p.system_prompt_block()
    assert "## What recall knows about the user" in block and block.endswith(PROFILE)
    p.sync_turn("why is the sink restarting?", "y" * 3_000, session_id="chat-1")
    assert KEPT == [{"op": "turn", "message": "why is the sink restarting?", "response": "y" * 3_000,
                     "session_id": "chat-1", "platform": "cli"}]

    quiet = provider(plugin, tmp_path, server, learn=False, profile=False)
    quiet.sync_turn("another question entirely", "an answer", session_id="chat-2")
    assert len(KEPT) == 1 and "What recall knows" not in quiet.system_prompt_block()

    cron = plugin.RecallMemoryProvider()
    (tmp_path / "recall.json").write_text(json.dumps({"socket": str(server)}))
    cron.initialize("s2", hermes_home=str(tmp_path), agent_context="cron")
    cron.sync_turn("scheduled work", "report", session_id="cron-1")
    assert len(KEPT) == 1


def test_plugin_for_an_instance_that_stores_turns_asks_for_no_profile_and_no_learning(server, plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_SERVE_STORE", "1")
    stored = []
    monkeypatch.setattr(serve, "store", lambda settings, req, save=None: stored.append(req) or {"ok": True, "stored": 1})
    p = provider(plugin, tmp_path, server, store_turns=True)
    assert p.system_prompt_block() == plugin.PRIVATE_BLOCK
    p.sync_turn("a private question", "a private answer", session_id="chat-1")
    for _ in range(100):
        if stored:
            break
        time.sleep(0.02)
    assert stored[0]["op"] == "store" and KEPT == []


def test_plugin_status_line_and_an_older_server(server, plugin, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", types.SimpleNamespace(get_hermes_home=lambda: tmp_path))
    p = provider(plugin, tmp_path, server)
    shown = p.get_status_config({})
    assert shown["summary"] == "✓ connected · 12 memories, 5 learned from chats, 2 retired"
    assert shown["last conversation read"] == "2026-11-14" and shown["profile written"] == "never"
    assert shown["exchanges waiting to be read"] == 3

    # A server from before these requests existed answers "unknown op"; the plugin carries on without them.
    monkeypatch.setattr(serve, "profile", lambda settings: {"ok": False, "error": "unknown op 'profile'"})
    monkeypatch.setattr(serve, "turn", lambda settings, req: {"ok": False, "error": "unknown op 'turn'"})
    old = provider(plugin, tmp_path, server)
    assert "What recall knows" not in old.system_prompt_block() and "# recall" in old.system_prompt_block()
    old.sync_turn("a question", "an answer", session_id="chat-1")
    assert "syncd was failing" in old.prefetch("why did syncd stop?", session_id="chat-1")
    (tmp_path / "recall.json").write_text(json.dumps({"socket": str(tmp_path / "missing.sock")}))
    assert p.get_status_config({})["summary"].startswith("✗ recall is not answering")


def test_context_returns_structured_items():
    find.calls.clear()
    out = serve.context(None, {"query": "what happened with syncd and niri"}, find)
    assert len(out["items"]) == out["count"]
    first = out["items"][0]
    assert first["label"] == "Session: syncd stopped" and first["date"] == "2026-11-09"
    assert first["source"] == "x:Session: syncd stopped" and first["score"] == 0.91
    assert first["text"] and "\n" not in first["text"]


NOTE = """Claude Code session in ~/Projects/x, 2026-11-09.

## Done
- Built the thing.

## Still open
- Nothing is pushed to GitHub.
- The Gmail connector needs
  reconnecting.

## Where
- ~/Projects/x"""


def test_still_open_reads_only_its_section():
    assert serve.still_open(NOTE) == ["Nothing is pushed to GitHub.", "The Gmail connector needs reconnecting."]
    assert serve.still_open("## Done\n- all of it") == []


def test_rejoin_drops_the_chunk_overlap():
    from recall import chunk

    long = "\n\n".join(f"Paragraph {i}. " + "words " * 60 for i in range(8)) + "\n\n" + NOTE
    pieces = chunk.split(long)
    assert len(pieces) > 2
    assert serve.rejoin(pieces).count("Paragraph 3.") == 1
    assert serve.still_open(serve.rejoin(pieces))[0] == "Nothing is pushed to GitHub."


def test_open_items_groups_notes_newest_first():
    from recall import chunk

    def rows(source, title, day, text, tags=("session-note", "claude"), cwd="/home/x/Projects/x"):
        return [{"source": source, "title": title, "chunk_index": i, "text": t, "tags": list(tags),
                 "created_at": datetime(2026, 11, day, 12, tzinfo=timezone.utc), "session": {"cwd": cwd}}
                for i, t in enumerate(chunk.split(text))]

    data = (rows("session:a", "Session: Older work (2026-11-08)", 8, NOTE)
            + rows("session:b", "Session: Newer work (2026-11-10)", 10, NOTE.replace("pushed", "merged"))
            + rows("session:c", "Session: Nothing left (2026-11-11)", 11, "## Done\n- all"))
    seen = {}

    def load(settings, since):
        seen["since"] = since
        return data

    out = serve.open_items(None, {"days": 999}, now=datetime(2026, 11, 14, tzinfo=timezone.utc), load=load)
    assert out["days"] == 90 and seen["since"] == datetime(2026, 8, 16, tzinfo=timezone.utc)
    assert [n["source"] for n in out["notes"]] == ["session:b", "session:a"]
    assert out["notes"][0]["title"] == "Newer work" and out["notes"][0]["kind"] == "claude"
    assert out["notes"][0]["items"][0] == "Nothing is merged to GitHub."


def test_answer_routes_open_items(monkeypatch):
    monkeypatch.setattr(serve, "open_items", lambda settings, req: {"ok": True, "notes": [], "days": req["days"]})
    assert serve.answer(None, '{"op": "open_items", "days": 3}') == {"ok": True, "notes": [], "days": 3}


def test_plugin_asks_the_hosted_recall_with_the_agent_key(plugin, tmp_path, monkeypatch):
    sent = []

    class Reply:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self.body).encode()

    def urlopen(req, timeout):
        sent.append((req.full_url, req.get_header("Authorization"), json.loads(req.data), timeout))
        return Reply({"ok": True, "count": 1, "text": "- memory: WezTerm", "ids": ["memory:1#0"]})

    monkeypatch.setattr(plugin.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("RECALL_AGENT_KEY", "rk_test")
    p = provider(plugin, tmp_path, tmp_path / "unused.sock", url="https://recall.example/serve", profile=False)
    text = p.prefetch("which terminal do I use?", session_id="s1")
    assert text.startswith("From recall") and "WezTerm" in text and p.recall_status().count == 1
    url, auth, body, _ = sent[-1]
    assert url == "https://recall.example/serve" and auth == "Bearer rk_test" and body["op"] == "context"
    monkeypatch.delenv("RECALL_AGENT_KEY")
    assert p.prefetch("which terminal again?") == ""          # no key: the turn goes on without recall


def test_catch_up_lists_every_session_newest_first_with_what_it_left_open():
    from recall import chunk

    def rows(source, title, day, text, tags):
        return [{"source": source, "title": title, "chunk_index": i, "text": t, "tags": list(tags),
                 "created_at": datetime(2026, 11, day, 12, tzinfo=timezone.utc), "session": {"cwd": "/srv/proj"}}
                for i, t in enumerate(chunk.split(text))]

    data = (rows("session:a", "Session: Older work (2026-11-08)", 8, NOTE, ("session-note", "claude"))
            + rows("session:hermes:b", "Session: Chat with Hermes (2026-11-12)", 12, "## Done\n- all", ("session-note", "hermes")))
    profile = {"text": "**Who he is**\n- Sam.", "written": datetime(2026, 11, 13, tzinfo=timezone.utc)}
    text = serve.catch_up(None, 7, now=datetime(2026, 11, 14, tzinfo=timezone.utc), load=lambda s, d: data,
                          load_profile=lambda s: profile)
    assert text.startswith("# Who the user is (profile written 2026-11-13)\n**Who he is**")
    chat, older = text.index("Chat with Hermes"), text.index("Older work")
    assert chat < older and "(Hermes; session:hermes:b)" in text and "(/srv/proj; session:a)" in text
    assert "    open: Nothing is pushed to GitHub." in text
    bare = serve.catch_up(None, 7, now=datetime(2026, 11, 14, tzinfo=timezone.utc), load=lambda s, d: [], with_profile=False)
    assert "Who the user is" not in bare and "none" in bare
