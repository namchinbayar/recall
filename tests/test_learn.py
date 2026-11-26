import json
from datetime import datetime, timedelta, timezone

import pytest

from recall import learn

NOW = datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)
OLD = "memory:aaaaaaaaaaaa"
OTHER = "memory:bbbbbbbbbbbb"


class FakeStore(learn.Store):
    """Everything in lists, no database and no model."""

    def __init__(self):
        self.turns: dict[str, dict] = {}
        self.events: list[dict] = []
        self.state: dict[str, dict] = {}
        self.memories: dict[str, learn.Known] = {
            OLD: learn.Known(OLD, "Sam's terminal is Alacritty.", when=NOW - timedelta(days=30)),
            OTHER: learn.Known(OTHER, "Sam runs niri on CachyOS.", when=NOW - timedelta(days=20)),
        }
        self.notes_on_disk = [learn.Known("file:/m/user-desktop.md", "He uses WezTerm and zsh.", "Claude memory: user-desktop.md", NOW)]
        self.retired: dict[str, dict] = {}
        self.saved_notes: dict[str, tuple[str, str]] = {}
        self.near: tuple[str, float] | None = None
        self.n = 0

    def record_turn(self, doc):
        old = self.turns.get(doc["_id"])
        if old is None:
            self.turns[doc["_id"]] = dict(doc)
            return "new"
        if len(doc["message"]) + len(doc["response"]) > len(old["message"]) + len(old["response"]):
            old.update(message=doc["message"], response=doc["response"], origin=doc["origin"])
            return "longer"
        return "same"

    def hermes_events(self, since):
        return [e for e in self.events if e["created_at"] >= since]

    def pending(self):
        rows: dict[str, dict] = {}
        for t in self.turns.values():
            if not t["learned"]:
                r = rows.setdefault(t["session_id"], {"session_id": t["session_id"], "last": t["at"], "turns": 0, "chars": 0})
                r["last"], r["turns"] = max(r["last"], t["at"]), r["turns"] + 1
                r["chars"] += len(t["message"]) + len(t["response"])
        return sorted(rows.values(), key=lambda r: r["last"])

    def unread(self, session_id):
        return sorted((t for t in self.turns.values() if t["session_id"] == session_id and not t["learned"]), key=lambda t: t["at"])

    def mark_read(self, ids, when):
        for i in ids:
            self.turns[i]["learned"] = True

    def get_state(self, key):
        return self.state.get(key)

    def set_state(self, key, fields):
        self.state.setdefault(key, {}).update(fields)

    def recent_memories(self, limit):
        return list(self.memories.values())[:limit]

    def related(self, query):
        return list(self.notes_on_disk)

    def embed(self, text):
        return [0.0]

    def nearest_memory(self, vector):
        return self.near

    def add_fact(self, fact, vector, when, session_id, replaces):
        self.n += 1
        source = f"memory:{self.n:012x}"
        self.memories[source] = learn.Known(source, fact, when=when)
        return source

    def retire(self, source, why, by, session_id):
        if source not in self.memories:
            return 0
        self.retired[source] = {"text": self.memories.pop(source).text, "why": why, "by": by}
        return 1

    def save_note(self, session_id, title, text, when, platform):
        self.saved_notes[session_id] = (title, text)

    def profile_material(self):
        return list(self.memories.values()) + self.notes_on_disk


def say(store, message, response="Done.", session="s1", minutes_ago=60, **more):
    doc = learn.turn_doc(session, message, response, NOW - timedelta(minutes=minutes_ago), "local", **more)
    store.record_turn(doc)
    return doc


def model(reply: dict, calls: list | None = None, review: list | None = None):
    """A stand-in model: `reply` for the read, and for the second look `review` (default: keep everything)."""
    def learner(system, prompt, schema=None):
        if system == learn.REVIEW_SYSTEM:
            n = prompt.count("\n", prompt.index("<proposed>"), prompt.index("</proposed>")) - 1
            return json.dumps({"decisions": review if review is not None else [{"n": i, "decision": "keep", "id": ""} for i in range(1, n + 1)]})
        if calls is not None:
            calls.append(prompt)
        return json.dumps(reply)
    return learner


EMPTY = {"note_title": "", "note_body": "", "facts": [], "retire": []}
MOVED = "I switched my terminal from Alacritty to WezTerm last week, please remember that from now on."


def test_the_same_exchange_has_one_key_whole_or_cut():
    long = "Why does the sink restart? " * 60
    whole = learn.turn_doc("s1", f"[Triggering message id: 9]  {long}", "Because of DNS at boot. " * 50, NOW, "discord")
    cut = learn.turn_doc("s1", long[:500], ("Because of DNS at boot. " * 50)[:500], NOW, "discord", origin="event")
    assert whole["_id"] == cut["_id"] != learn.turn_doc("s2", long, "Because of DNS at boot.", NOW)["_id"]
    assert learn.turn_doc("s1", "hi", "", NOW) is None                      # half an exchange is not one
    assert learn.turn_doc("", "hello there", "Hi.", NOW, "sms")["session_id"] == "-sms-20261114"

    store = FakeStore()
    assert store.record_turn(cut) == "new" and store.record_turn(whole) == "longer" and store.record_turn(cut) == "same"
    assert len(store.turns) == 1 and store.turns[whole["_id"]]["origin"] == "provider"


def test_events_are_copied_in_once_and_a_first_run_starts_from_now():
    store = FakeStore()
    event = {"text": "[2026-11-14 19:00 UTC] hermes / photon\nMe: remind me\nabout the dentist\nHermes: Set for Monday.",
             "created_at": NOW - timedelta(hours=1), "event": {"kind": "photon", "meta": {"session_id": "a1", "agent": "cloud", "platform": "photon"}}}
    store.events = [event, {"text": "[x] hermes / local\nnot an exchange", "created_at": NOW, "event": {}}]
    assert learn.import_events(store, NOW) == 0                               # nothing before the first run is touched
    assert learn.import_events(store, NOW + timedelta(minutes=5), since=NOW - timedelta(days=1)) == 1
    assert learn.import_events(store, NOW + timedelta(minutes=10), since=NOW - timedelta(days=1)) == 0
    turn = next(iter(store.turns.values()))
    assert (turn["message"], turn["response"], turn["agent"], turn["origin"]) == ("remind me\nabout the dentist", "Set for Monday.", "cloud", "event")
    assert learn.head([turn]) == "Conversation with Hermes in the cloud on photon, 2026-11-14."


def test_due_waits_for_quiet_and_backs_off_after_a_failure():
    rows = [{"session_id": "quiet", "last": NOW - timedelta(minutes=45)}, {"session_id": "active", "last": NOW - timedelta(minutes=5)},
            {"session_id": "failed", "last": NOW - timedelta(hours=3)}]
    ready, active, waiting = learn.due(rows, {"failed": {"retry_after": NOW + timedelta(minutes=30)}}, NOW)
    assert [r["session_id"] for r in ready] == ["quiet"] and (active, waiting) == (1, 1)
    assert len(learn.due(rows, {"failed": {"retry_after": NOW - timedelta(minutes=1)}}, NOW)[0]) == 2


def test_a_new_fact_replaces_the_memory_it_corrects_and_nothing_is_deleted():
    store, calls = FakeStore(), []
    say(store, MOVED, "Noted, WezTerm it is.")
    reply = {**EMPTY, "facts": [{"fact": "Sam's terminal is WezTerm, as of 2026-11-14; he moved off Alacritty.", "replaces": f"[{OLD}]"}]}
    out = learn.read_session(store, store.pending()[0], model(reply, calls), NOW)

    assert out.result == "learned" and out.replaced == [OLD] and len(out.added) == 1
    assert OLD not in store.memories and store.retired[OLD]["by"] == out.added[0]
    assert store.retired[OLD]["text"] == "Sam's terminal is Alacritty."         # kept, and can come back
    assert f"[{OLD}, 2026-10-15] Sam's terminal is Alacritty." in calls[0]      # the model saw what it replaced
    assert "[note: Claude memory: user-desktop.md, 2026-11-14] He uses WezTerm and zsh." in calls[0]
    assert "Me: I switched my terminal" in calls[0] and "Hermes: Noted, WezTerm it is." in calls[0]
    assert store.pending() == [] and store.state["session:s1"]["turns"] == 1


def test_code_turns_down_what_the_model_should_not_have_proposed():
    store = FakeStore()
    say(store, MOVED)
    reply = {**EMPTY, "facts": [
        {"fact": "ok", "replaces": ""},
        {"fact": "Sam's database password is hunter2 and the key is sk-ant-api03-" + "a" * 40 + ".", "replaces": ""},
        {"fact": "Sam keeps his notes in Obsidian,\nand everything else too.", "replaces": ""},
        {"fact": "Sam's phone is called pixel, as of 2026-11-14.", "replaces": "memory:ffffffffffff"},
        {"fact": "x" * 500, "replaces": ""},
    ], "retire": [{"id": "memory:ffffffffffff", "why": "made up"}, {"id": "file:/m/user-desktop.md", "why": "a file"}]}
    out = learn.read_session(store, store.pending()[0], model(reply), NOW)

    assert len(out.added) == 2 and out.replaced == [] and out.retired == []       # an id it was never shown replaces nothing
    assert [r.split(":")[0] for r in out.rejected] == ["too short", "looks like a secret (API key)", "too long"]
    assert store.retired == {} and set(store.memories) >= {OLD, OTHER}
    assert "Sam keeps his notes in Obsidian, and everything else too." in [m.text for m in store.memories.values()]

    store.near = (OTHER, 0.97)                                                     # says what a memory already says
    say(store, MOVED + " Again.", session="s2")
    again = learn.read_session(store, store.pending()[0], model({**EMPTY, "facts": [{"fact": "Sam runs the niri compositor on CachyOS.", "replaces": ""}]}), NOW)
    assert again.result == "nothing" and again.rejected == [f"already known as {OTHER}: Sam runs the niri compositor on CachyOS."]

    store.near = (OLD, 0.95)                                                       # close to the memory it corrects: still a correction
    say(store, MOVED + " Really.", session="s4")
    fix = {**EMPTY, "facts": [{"fact": "Sam's terminal is WezTerm, not Alacritty.", "replaces": OLD}]}
    assert learn.read_session(store, store.pending()[0], model(fix), NOW).replaced == [OLD]
    store.memories[OTHER] = learn.Known(OTHER, "Sam runs niri on CachyOS.")
    store.near = (OTHER, 0.99)                                                     # the very same sentence replaces nothing
    say(store, MOVED + " Truly.", session="s5")
    same = {**EMPTY, "facts": [{"fact": "Sam runs niri on CachyOS.", "replaces": OTHER}]}
    assert learn.read_session(store, store.pending()[0], model(same), NOW).added == [] and OTHER in store.memories


def test_a_second_look_drops_what_is_known_and_finds_what_a_fact_replaces():
    store, asked = FakeStore(), []
    store.related = lambda query: asked.append(query) or [learn.Known(OTHER, "Sam runs niri on CachyOS."), *store.notes_on_disk]
    say(store, MOVED)
    reply = {**EMPTY, "facts": [
        {"fact": "Sam uses WezTerm as his terminal on the desktop.", "replaces": ""},
        {"fact": "Sam moved desktop from niri to Hyprland on 2026-11-14.", "replaces": ""},
        {"fact": "Sam's dentist is on Monday mornings, as of 2026-11-14.", "replaces": ""},
    ]}
    verdict = [{"n": 1, "decision": "known", "id": ""}, {"n": 2, "decision": "replaces", "id": OTHER}, {"n": 3, "decision": "keep", "id": ""}]
    out = learn.read_session(store, store.pending()[0], model(reply, review=verdict), NOW)
    assert asked[-3:] == [f["fact"] for f in reply["facts"]]                       # each fact is looked up by what it says
    assert out.rejected == ["already known: Sam uses WezTerm as his terminal on the desktop."]
    assert len(out.added) == 2 and out.replaced == [OTHER] and OTHER in store.retired

    say(store, MOVED + " Once more.", session="s2")
    broken = lambda system, prompt, schema=None: "{}" if system == learn.REVIEW_SYSTEM else json.dumps(reply)
    with pytest.raises(learn.Declined):
        learn.read_session(store, store.pending()[0], broken, NOW)
    assert len(store.unread("s2")) == 1                                            # nothing stored on half a judgement


def test_retire_takes_only_memories_it_was_shown_and_at_most_a_few():
    store = FakeStore()
    for i in range(5):
        store.memories[f"memory:{i:012d}"] = learn.Known(f"memory:{i:012d}", f"Old fact number {i} about a thing.")
    say(store, MOVED)
    reply = {**EMPTY, "retire": [{"id": f"memory:{i:012d}", "why": "he said so"} for i in range(5)]}
    out = learn.read_session(store, store.pending()[0], model(reply), NOW)
    assert out.result == "learned" and len(out.retired) == learn.MAX_RETIRE and len(store.retired) == learn.MAX_RETIRE


def test_a_conversation_gets_a_note_and_its_continuation_updates_it():
    store, calls = FakeStore(), []
    say(store, "Why does the recall sink keep restarting after boot? Please look into it properly.", "It starts before DNS is up.")
    first = {**EMPTY, "note_title": "recall sink restarts at boot (2026-11-14)", "note_body": "## Done\n- Found the sink starts before DNS is up."}
    out = learn.read_session(store, store.pending()[0], model(first, calls), NOW)
    assert out.result == "noted" and out.title == "recall sink restarts at boot"
    title, text = store.saved_notes["s1"]
    assert title == "Session: recall sink restarts at boot (2026-11-14)"
    assert text.startswith("Conversation with Hermes on local, 2026-11-14.\n\n## Done")
    assert "<earlier_note>" not in calls[0]

    say(store, "ok fix it", "Added a wait for name resolution.", minutes_ago=30)   # short, but it continues a noted conversation
    second = {**EMPTY, "note_title": "", "note_body": "## Done\n- Found and fixed: the sink now waits for DNS."}
    learn.read_session(store, store.pending()[0], model(second, calls), NOW)
    assert "<earlier_note>\nTITLE: recall sink restarts at boot\n## Done\n- Found the sink starts" in calls[1]
    assert "Why does the recall sink" not in calls[1]                              # only the new turns are read again
    assert store.saved_notes["s1"][0] == "Session: recall sink restarts at boot (2026-11-14)"
    assert "fixed" in store.saved_notes["s1"][1] and store.state["session:s1"]["turns"] == 2

    say(store, MOVED, session="s3")
    leak = {**EMPTY, "note_title": "keys", "note_body": "## Where\n- The key is sk-ant-api03-" + "b" * 40}
    assert learn.read_session(store, store.pending()[0], model(leak), NOW).result == "secret" and "s3" not in store.saved_notes


def test_small_talk_and_scheduled_reports_cost_no_model_call():
    store, calls = FakeStore(), []
    say(store, "thanks!", "Any time.")
    say(store, "[Cron job \"Morning briefing\" report: shown in the chat, no reply asked for] " + "Weather and mail. " * 30, "ok", session="cron")
    counts = learn.run_pass(store, model(EMPTY, calls), lambda line: None, now=NOW)
    assert counts == {"quiet": 2} and calls == [] and store.pending() == []


def test_a_long_backlog_is_read_in_pieces_and_a_failed_call_waits():
    store, lines = FakeStore(), []
    for i in range(4):
        say(store, f"Question {i}: " + "why " * 5_000, "Because. " * 2_000, minutes_ago=200 - i)
    assert len(learn.take(store.unread("s1"))) == 2

    def broken(system, prompt, schema=None):
        raise learn.Declined("Claude API error (APIConnectionError)")

    assert learn.run_pass(store, broken, lines.append, now=NOW) == {"failed": 1}
    assert "trying again in 1 h" in lines[0] and len(store.unread("s1")) == 4      # nothing was marked read
    assert learn.run_pass(store, model(EMPTY), lines.append, now=NOW + timedelta(minutes=5)) == {}
    later = NOW + timedelta(hours=2)
    assert learn.run_pass(store, model(EMPTY), lines.append, now=later) == {"nothing": 1} and len(store.unread("s1")) == 2
    assert learn.run_pass(store, model(EMPTY), lines.append, now=later) == {"nothing": 1} and store.pending() == []

    with pytest.raises(learn.Declined):
        learn.parse("not json")


def test_the_profile_is_rewritten_only_when_its_material_changed():
    store, calls = FakeStore(), []
    writer = lambda system, prompt, schema=None: calls.append((system, prompt)) or "## Setup\n- Uses WezTerm on CachyOS."
    assert learn.refresh_profile(store, writer, NOW) == "written"
    assert store.state["profile"]["text"].startswith("## Setup") and store.state["profile"]["sources"] == 3
    assert calls[0][0] == learn.PROFILE_SYSTEM and "[note: Claude memory: user-desktop.md, 2026-11-14]\nHe uses WezTerm" in calls[0][1]
    assert learn.refresh_profile(store, writer, NOW + timedelta(hours=1)) == "recent"
    assert learn.refresh_profile(store, writer, NOW + timedelta(hours=7)) == "current" and len(calls) == 1
    store.memories[OLD].text = "Sam's terminal is WezTerm."
    assert learn.refresh_profile(store, writer, NOW + timedelta(hours=14)) == "written" and len(calls) == 2
    leaky = lambda system, prompt, schema=None: "- His key is sk-ant-api03-" + "c" * 40
    assert learn.refresh_profile(store, leaky, NOW, force=True) == "secret"
    assert store.state["profile"]["text"].startswith("## Setup")                  # the old profile stays
    assert learn.estimate(store, [{"chars": 4_000}], "claude-sonnet-5-5")[1] < 0.05
