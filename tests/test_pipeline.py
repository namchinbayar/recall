import json
from datetime import datetime, timezone

from recall import chunk
from recall.stream import notify, sink, spool


def test_chunk_split_sizes_and_overlap():
    paras = [("word " * 60).strip() for _ in range(12)]  # ~300 chars each
    pieces = chunk.split("\n\n".join(paras), target=1000, overlap=100)
    assert len(pieces) > 1
    assert all(len(p) <= 1000 + 100 + 2 for p in pieces)
    assert pieces[1].startswith(pieces[0][-100:])


def test_chunk_hard_splits_long_paragraph():
    pieces = chunk.split("x" * 3500, target=1500, overlap=200)
    assert [len(p) for p in pieces][:2] == [1500, 1500]
    assert chunk.split("   ") == []


def test_one_time_codes():
    assert notify.is_one_time_code("Your verification code is 482913")
    assert notify.is_one_time_code("Use 123-456 as your security code")
    assert not notify.is_one_time_code("Meeting moved to 1430")
    assert not notify.is_one_time_code("Your code review is ready")


def test_notify_event_carries_content_key():
    e = notify.to_event("Claude", "Done", "merged", 1, "claude", 1_700_000_000_000)
    meta = json.loads(e["meta"])
    assert e["body"] == "Done\nmerged" and meta["content_key"]


def test_spool_validation():
    ok = spool.to_event({"event_id": "1", "source": "hermes", "occurred_at": "5", "body": "hi", "meta": {"a": 1}})
    assert ok["occurred_at"] == 5 and ok["meta"] == '{"a": 1}'
    assert spool.to_event({"source": "hermes", "occurred_at": 5}) is None
    assert spool.to_event({"event_id": "1", "source": "x", "occurred_at": 5, "body": "your code is 123456"}) is None


def test_sink_doc_shape():
    d = sink.to_doc({"event_id": "abc", "source": "hermes", "kind": "discord", "actor": "u",
                     "occurred_at": 1_700_000_000_000, "body": "hello\nworld", "meta": '{"x": 1}'})
    assert d["source"] == "event:hermes:abc" and d["tags"] == ["event", "hermes"]
    assert d["title"] == "discord: hello" and d["created_at"].tzinfo == timezone.utc
    assert "content_key" not in d["event"]


def test_digest_row_becomes_one_event_per_source_and_hour():
    row = {
        "window_start": datetime(2026, 11, 14, 9, tzinfo=timezone.utc),
        "window_end": 1794650400000,          # the same hour's end, as Avro may also hand it over
        "source": "desktop-notification", "events": 3, "kinds": "Tether, Claude",
        "sample": "Tether: a | Claude: b | Tether: c",
    }
    event = sink.digest_event(row)
    assert event["event_id"] == "desktop-notification:20261114T0900"
    assert (event["source"], event["kind"]) == ("digest", "desktop-notification")
    assert event["body"].splitlines()[0] == "3 events from 09:00 to 10:00 UTC (Tether, Claude)"
    assert json.loads(event["meta"])["events"] == 3

    doc = sink.to_doc(event)
    assert doc["source"] == "event:digest:desktop-notification:20261114T0900"
    assert doc["tags"] == ["event", "digest"]
    assert doc["title"].startswith("desktop-notification: 3 events from 09:00")
    assert "content_key" not in doc["event"]      # digests skip the notification repeat check


def test_digest_keeps_a_long_hour_within_bounds():
    row = {"window_start": 0, "window_end": 3_600_000, "source": "hermes", "events": 1, "kinds": None,
           "sample": "x" * 50_000}
    event = sink.digest_event(row)
    assert event["body"].startswith("1 event from 00:00 to 01:00 UTC\n")
    assert len(event["body"]) < sink.DIGEST_CHARS + 100
