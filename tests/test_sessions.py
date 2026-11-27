import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recall import sessions

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
LONG = ("Please move the metrics sampler from a timer to a looping service, and say why it matters. "
        "Right now a systemd timer starts it every minute, and each start goes through op run, which takes "
        "about three seconds and counts against the 1Password service account's hourly limit. A loop would "
        "sign in once. Keep the unit name, keep the one-minute cadence, and make sure a database error "
        "does not end the loop.")


def row(kind, content, minute=0, **more):
    return {"type": kind, "sessionId": "s1", "cwd": str(Path.home() / "Projects/recall"),
            "timestamp": f"2026-11-10T19:{minute:02d}:00.000Z", "message": {"role": kind, "content": content}, **more}


def transcript(tmp_path, rows, name="s1", age_minutes=60) -> sessions.Candidate:
    path = tmp_path / "proj" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    when = (NOW - timedelta(minutes=age_minutes)).timestamp()
    os.utime(path, (when, when))
    return sessions.candidates([tmp_path])[0] if name == "s1" else next(
        c for c in sessions.candidates([tmp_path]) if c.session_id == name)


BASIC = [
    row("user", [{"type": "text", "text": f"<system-reminder>ignore me</system-reminder>{LONG}"}], 0, origin={"kind": "human"}),
    row("assistant", [{"type": "thinking", "thinking": "private reasoning"}, {"type": "text", "text": "I'll switch it to a loop."}], 1),
    row("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "systemctl --user restart x", "description": "Restart the metrics service"}}], 2),
    row("user", [{"type": "tool_result", "content": "SECRET TOOL OUTPUT"}], 3),
    row("assistant", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/unit.service"}},
                      {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/other"}}], 4),
    row("user", "<task-notification>agent finished</task-notification>", 5),
    row("user", "background agent says hi, at some length, to pass the minimum", 6, origin={"kind": "agent"}),
    row("assistant", [{"type": "text", "text": "Done: the service loops now."}], 7, isSidechain=True),
    {"type": "custom-title", "customTitle": "Metrics as a service", "sessionId": "s1"},
    {"type": "api-request-blob", "message": {"content": [{"type": "text", "text": "duplicate of the prompt"}]}},
]


def test_read_keeps_what_was_said_and_one_line_per_action(tmp_path):
    t = sessions.read(transcript(tmp_path, BASIC).path)
    assert t.turns == [
        ("USER", LONG),
        ("ASSISTANT", "I'll switch it to a loop.\n[ran: Restart the metrics service]\n[edited: /x/unit.service]"),
    ]
    assert t.title == "Metrics as a service" and t.cwd.endswith("Projects/recall")
    assert t.user_chars == len(LONG) and t.started.minute == 0 and t.ended.minute == 4
    text = sessions.windows(t.turns)[0]
    assert "SECRET TOOL OUTPUT" not in text and "private reasoning" not in text and "ignore me" not in text


def test_read_stops_at_the_last_whole_line_and_resumes_from_an_offset(tmp_path):
    c = transcript(tmp_path, BASIC[:2])
    with open(c.path, "a") as f:
        f.write('{"type": "user", "message": {"content": "half a li')
    first = sessions.read(c.path)
    assert len(first.turns) == 2 and first.end_offset < c.path.stat().st_size
    with open(c.path, "a") as f:
        f.write('ne"}}\n' + json.dumps(row("user", "And now the second question, in full.", 9)) + "\n")
    rest = sessions.read(c.path, first.end_offset)
    assert [text for _, text in rest.turns] == ["half a line", "And now the second question, in full."]


def test_read_includes_messages_sent_mid_turn_once(tmp_path):
    queued = {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "also add a test for it"}}
    other = {"type": "attachment", "attachment": {"type": "file", "prompt": "not a message"}}
    c = transcript(tmp_path, [BASIC[0], BASIC[1], queued, queued, other, row("assistant", [{"type": "text", "text": "Added."}], 8)])
    t = sessions.read(c.path)
    assert [text for role, text in t.turns if role == "USER"] == [LONG, "also add a test for it"]
    assert t.turns[-1] == ("ASSISTANT", "Added.")


def test_long_messages_keep_both_ends_and_windows_split_between_turns():
    clipped = sessions._clip("a" * 5000 + "MIDDLE" + "z" * 5000)
    assert clipped.startswith("aaa") and clipped.endswith("zzz") and "MIDDLE" not in clipped and "left out" in clipped
    turns = [("USER", "u" * 50), ("ASSISTANT", "a" * 50), ("USER", "v" * 50)]
    assert [len(w.split("\n\n")) for w in sessions.windows(turns, limit=130)] == [2, 1]
    assert sessions.windows([]) == []


def test_plan_sorts_sessions_by_state_age_and_activity(tmp_path):
    done = transcript(tmp_path, BASIC, "done")
    grown = transcript(tmp_path, BASIC, "grown")
    transcript(tmp_path, BASIC, "active", age_minutes=5)
    transcript(tmp_path, BASIC, "old", age_minutes=60 * 24 * 30)
    transcript(tmp_path, BASIC, "failed")
    state = {"sessions": {
        "done": {"bytes": done.size, "status": "saved"},
        "grown": {"bytes": grown.size - 10, "status": "saved"},
        "failed": {"bytes": 0, "retry_after": (NOW + timedelta(minutes=30)).isoformat()},
    }}
    todo = sessions.plan(sessions.candidates([tmp_path]), state, NOW - timedelta(days=7), NOW)
    assert [c.session_id for c in todo.due] == ["grown"] and [c.session_id for c in todo.active] == ["active"]
    assert (todo.unchanged, todo.older, todo.waiting) == (1, 1, 1)
    later = sessions.plan(sessions.candidates([tmp_path]), state, NOW - timedelta(days=7), NOW + timedelta(hours=2))
    assert sorted(c.session_id for c in later.due) == ["active", "failed", "grown"]


def test_parse_reply_and_since():
    note = sessions.parse_reply("TITLE: Metrics as a service\n## Done\n- looped it")
    assert note == sessions.Note("Metrics as a service", "## Done\n- looped it")
    assert sessions.parse_reply("SKIP") is None and sessions.parse_reply(" skip.\n") is None
    assert sessions.parse_reply("## Done\n- no title line").title == ""
    assert sessions.parse_since("30d", NOW) == NOW - timedelta(days=30)
    assert sessions.parse_since("2026-10-01", NOW).year == 2026
    with pytest.raises(SystemExit):
        sessions.parse_since("last week", NOW)


@pytest.fixture
def stored(monkeypatch):
    calls = []
    monkeypatch.setattr(sessions.ingest, "store", lambda settings, *a: calls.append(a) or (a[0], 1))
    return calls


def test_process_saves_then_updates_from_the_new_part_only(tmp_path, stored):
    seen_by_model = []

    def summarize(head, note, dialogue):
        seen_by_model.append((head, note, dialogue))
        return sessions.Note("Metrics as a service", f"## Done\n- pass {len(seen_by_model)}")

    state = {"sessions": {}}
    c = transcript(tmp_path, BASIC)
    assert sessions.process(None, c, state, summarize) == "saved"
    source, title, text, tags, extra = stored[0]
    assert source == "session:s1" and title == "Session: Metrics as a service (2026-11-10)"
    assert text.startswith("Claude Code session in ~/Projects/recall, 2026-11-10.\n\n## Done")
    assert tags == sessions.TAGS and extra["created_at"].minute == 4 and extra["session"]["id"] == "s1"
    assert state["sessions"]["s1"]["bytes"] == c.size and seen_by_model[0][1] is None

    c = transcript(tmp_path, [row("user", "Actually make it sample every 30 seconds instead, the minute is too coarse.", 30),
                              row("assistant", [{"type": "text", "text": "Changed the interval to 30 s."}], 31)])
    assert sessions.process(None, c, state, summarize) == "updated"
    head, earlier, dialogue = seen_by_model[1]
    assert earlier.body == "## Done\n- pass 1" and "30 seconds" in dialogue and LONG not in dialogue
    assert stored[1][2].endswith("- pass 2") and stored[1][4]["created_at"].minute == 31
    assert "2026-11-10" in head and state["sessions"]["s1"]["note"] == "## Done\n- pass 2"

    c = transcript(tmp_path, [row("assistant", [{"type": "text", "text": "A background job finished."}], 40)])
    assert sessions.process(None, c, state, summarize) == "quiet"
    assert len(stored) == 2 and state["sessions"]["s1"]["bytes"] == c.size and state["sessions"]["s1"]["status"] == "saved"


def test_process_skips_small_talk_model_skips_and_secrets(tmp_path, stored):
    state = {"sessions": {}}
    tiny = transcript(tmp_path, [row("user", "hi"), row("assistant", [{"type": "text", "text": "Hello."}], 1)], "tiny")
    assert sessions.process(None, tiny, state, lambda *a: pytest.fail("no model call for small talk")) == "skipped"

    c = transcript(tmp_path, BASIC)
    assert sessions.process(None, c, state, lambda *a: None) == "skipped"
    leak = sessions.Note("Keys", "## Where\n- token ghp_" + "a1B2" * 9)
    state["sessions"].pop("s1")
    assert sessions.process(None, c, state, lambda *a: leak) == "secret"
    assert stored == [] and "GitHub token" in state["sessions"]["s1"]["why"]


def test_run_pass_starts_from_now_and_backs_off_after_a_failure(tmp_path, stored, monkeypatch):
    monkeypatch.setenv("RECALL_SESSION_ROOTS", str(tmp_path))
    state_path = tmp_path / "state.json"
    transcript(tmp_path, BASIC)
    lines = []
    good = lambda *a: sessions.Note("Metrics as a service", "## Done\n- looped it")

    assert sessions.run_pass(None, good, lines.append, state_path=state_path) == {}     # older than the first run
    assert "start" in json.loads(state_path.read_text())

    def broken(*a):
        raise sessions.Declined("Claude API error (APIConnectionError)")

    since = datetime.now(timezone.utc) - timedelta(days=365 * 5)
    assert sessions.run_pass(None, broken, lines.append, since, state_path=state_path) == {"failed": 1}
    assert sessions.run_pass(None, good, lines.append, since, state_path=state_path) == {}      # waiting to retry
    state = json.loads(state_path.read_text())
    state["sessions"]["s1"]["retry_after"] = "2000-01-01T00:00:00+00:00"
    state_path.write_text(json.dumps(state))
    assert sessions.run_pass(None, good, lines.append, since, state_path=state_path) == {"saved": 1}
    assert any("saved" in l and "Metrics as a service" in l for l in lines) and len(stored) == 1


def test_estimate_counts_only_sessions_with_something_said(tmp_path):
    transcript(tmp_path, BASIC)
    transcript(tmp_path, [row("user", "hi")], "tiny")
    transcript(tmp_path, [row("assistant", [{"type": "text", "text": "No user here. " * 60}])], "nobody")
    state = {"sessions": {}}
    todo = sessions.plan(sessions.candidates([tmp_path]), state, NOW - timedelta(days=1), NOW)
    n, tokens, cost = sessions.estimate(todo, state, "claude-opus-5-5")
    assert n == 1 and tokens > 500 and 0 < cost < 0.1
    assert sessions.estimate(todo, state, "some-other-model")[2] is None


def test_two_runs_share_the_state_file_without_losing_each_others_work(tmp_path, stored, monkeypatch):
    monkeypatch.setenv("RECALL_SESSION_ROOTS", str(tmp_path))
    state_path = tmp_path / "state.json"
    a, b = transcript(tmp_path, BASIC, "a"), transcript(tmp_path, BASIC, "b")
    since = datetime.now(timezone.utc) - timedelta(days=365 * 5)

    def summarize(head, note, dialogue):
        if len(stored) == 0:    # while this run works on "a", another run finishes "b" and "other"
            other = sessions.load_state(state_path)
            other["sessions"]["b"] = {"bytes": b.size, "status": "saved", "title": "theirs", "note": "x"}
            other["sessions"]["other"] = {"bytes": 1, "status": "skipped"}
            sessions.save_state(other, state_path)
        return sessions.Note("Mine", "## Done\n- it")

    assert sessions.run_pass(None, summarize, lambda line: None, since, state_path=state_path) == {"saved": 1}
    on_disk = sessions.load_state(state_path)["sessions"]
    assert on_disk["a"]["title"] == "Mine" and on_disk["b"]["title"] == "theirs" and "other" in on_disk
    assert len(stored) == 1 and a.size == on_disk["a"]["bytes"]


def test_a_session_found_under_two_roots_counts_once_using_the_longer_copy(tmp_path):
    short = transcript(tmp_path / "one", BASIC[:1])
    transcript(tmp_path / "two", BASIC)
    found = sessions.candidates([tmp_path / "one", tmp_path / "two"])
    assert len(found) == 1 and found[0].size > short.size and "two" in str(found[0].path)


def test_default_roots_are_every_claude_folder_in_home_that_holds_transcripts(tmp_path, monkeypatch):
    for name in (".claude", ".claude2", ".claude-second", ".config", ".claude-empty"):
        (tmp_path / name).mkdir()
    for name in (".claude", ".claude2", ".claude-second", ".config"):
        (tmp_path / name / "projects").mkdir()
    (tmp_path / ".claude.json").write_text("{}")
    (tmp_path / ".claude-linked").symlink_to(tmp_path / ".claude2")       # two names, one folder: read once
    assert [p.parent.name for p in sessions.default_roots(tmp_path)] == [".claude", ".claude-second", ".claude2"]

    monkeypatch.setenv("RECALL_SESSION_ROOTS", f"{tmp_path}/a:{tmp_path}/b")      # the setting replaces them
    assert sessions.roots() == [tmp_path / "a", tmp_path / "b"]


def test_sessions_in_the_skip_file_are_never_candidates(tmp_path):
    transcript(tmp_path / "t", BASIC, name="keep")
    transcript(tmp_path / "t", BASIC, name="hide")
    skip_file = tmp_path / "sessions-skip"
    skip_file.write_text("# kept off the record\nhide  # trailing comment\n\n")
    assert sessions.skipped(skip_file) == {"hide"}
    found = sessions.candidates([tmp_path / "t"], skip=sessions.skipped(skip_file))
    assert [c.session_id for c in found] == ["keep"]
    assert sessions.skipped(tmp_path / "missing") == set()
