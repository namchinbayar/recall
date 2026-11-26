from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recall.stream import noise, purge, sink

EXAMPLE = Path(__file__).resolve().parents[1] / "contrib" / "noise.example.toml"
T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def policy():
    return noise.load(EXAMPLE)


@pytest.mark.parametrize("app, summary, body", [
    ("DMS", "Screenshot captured", "saved to ~/Pictures"),
    ("niri", "Screenshot captured", ""),
    ("Google Chrome", "Login saved", "github.com"),
    ("Tether", "“TikTok” is Now Up to Date", ""),
    ("notify-send", "hello", ""),
    ("1password-secret-service", "1Password Secret Service: bind item?", ""),
    ("Tether", "50% off your monster bash", ""),
    ("Tether", "Card declined, not enough funds ⛔️", "Your card was declined"),
    ("Tether", "Apple\xa0Cash", "You received $20"),
    ("Tether", "Y Combinator", "AI for City Hall"),
    ("Tether", "thehalfwaypost · New", ""),
    ("Hermes", "Hermes finished", "reply ready"),
])
def test_dropped(policy, app, summary, body):
    assert policy.drop_reason(app, summary, body)


@pytest.mark.parametrize("app, summary, body", [
    ("Claude", "Add a portfolio tab to the site", "merged as 04a230b"),
    ("Tether", "Anthropic", "Your API usage report"),
    ("Tether", "Jordan Lee", "see you at 6"),
    ("Superhuman", "New mail", "Re: lease"),
])
def test_kept(policy, app, summary, body):
    assert policy.drop_reason(app, summary, body) is None


def test_defaults_without_config(tmp_path):
    p = noise.load(tmp_path / "missing.toml")
    assert p.drop_reason("DMS", "Screenshot captured", "") == "screenshot"
    assert p.drop_reason("Tether", "Y Combinator", "x") is None


def test_rule_needs_a_field(tmp_path):
    f = tmp_path / "n.toml"
    f.write_text('[[drop]]\nreason = "x"\n')
    with pytest.raises(SystemExit):
        noise.load(f)


def test_content_key_ignores_case_and_spacing():
    assert noise.content_key("Tether", "Hi\n there") == noise.content_key("tether", "hi there")
    assert noise.content_key("Tether", "hi") != noise.content_key("Claude", "hi")


def test_recent_keys_window():
    r = noise.RecentKeys(timedelta(seconds=10))
    assert not r.seen_recently("k", 0)
    assert r.seen_recently("k", 5)
    assert r.seen_recently("k", 14)       # window slides from the last sighting
    assert not r.seen_recently("k", 30)


def _event(eid, app, summary, body, at):
    return {
        "event_id": eid, "source": "desktop-notification", "kind": app, "actor": None,
        "occurred_at": int(at.timestamp() * 1000), "body": f"{summary}\n{body}".strip(),
        "meta": f'{{"app": "{app}", "summary": "{summary}"}}',
    }


class FakeColl:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection=None):
        keys = set(query["event.content_key"]["$in"])
        return [r for r in self.rows if r["event"]["content_key"] in keys and r["created_at"] >= query["created_at"]["$gte"]]


def test_sink_keep(monkeypatch, policy):
    stored_key = noise.content_key("Tether", "Anthropic\nusage report")
    rows = [{"source": "event:desktop-notification:old", "created_at": T0, "event": {"content_key": stored_key}}]
    monkeypatch.setattr(sink.db, "chunks", lambda s: FakeColl(rows))
    docs = [sink.to_doc(e) for e in [
        _event("a", "Tether", "Anthropic", "usage report", T0 + timedelta(hours=1)),    # repeat of stored
        _event("b", "Claude", "PC health check", "all good", T0),                       # new
        _event("c", "Claude", "PC health check", "all good", T0 + timedelta(minutes=5)),  # repeat in batch
        _event("d", "DMS", "Screenshot captured", "", T0),                              # rule
        _event("e", "Tether", "Anthropic", "usage report", T0 + timedelta(days=9)),     # outside window
        {"event_id": "h", "source": "hermes", "kind": "discord", "occurred_at": int(T0.timestamp() * 1000), "body": "hi"},
    ]]
    kept = {d["source"].rsplit(":", 1)[1] for d in sink.keep(None, docs, policy)}
    assert kept == {"b", "e", "h"}


def test_sink_replay_of_same_event_is_kept(monkeypatch, policy):
    doc = sink.to_doc(_event("a", "Claude", "x", "y", T0))
    rows = [{"source": doc["source"], "created_at": T0, "event": {"content_key": doc["event"]["content_key"]}}]
    monkeypatch.setattr(sink.db, "chunks", lambda s: FakeColl(rows))
    assert sink.keep(None, [doc], policy) == [doc]


def test_purge_parts_matches_sink_key():
    doc = sink.to_doc(_event("a", "Tether", "Anthropic", "usage report", T0))
    app, summary, body, text = purge._parts(doc)
    assert (app, summary, body) == ("Tether", "Anthropic", "usage report")
    assert noise.content_key(app, text) == doc["event"]["content_key"]
