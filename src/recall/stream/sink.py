"""Consume events_raw and events_inbox, embed each event, and upsert it into the chunks collection.

Each event becomes one chunk with source "event:<source>:<event_id>" and the tags
["event", <source>], so `recall search` and the agent find it like any other
document. Offsets are committed only after a batch is stored, so a crash or a
Voyage rate limit replays the batch instead of losing it (upserts make that safe).

events_raw is Avro (schemas registered by Flink). events_inbox holds plain JSON
events from producers that use the Confluent Cloud REST API, which can't produce
Schema Registry formats (the cloud agent's Hermes hook, contrib/hermes-hook/cloud-recall-events/).

events_digest holds one row per hour and source, written by a Flink statement over
events_raw: how many events there were, of which kinds, and the start of each. A row is
stored as one "digest" event, so "what happened this morning" finds a few summaries
instead of hundreds of single notifications.

Desktop notifications pass the same noise rules and repeat check as
`recall stream notify` (noise.py), here against what Atlas already holds, so a
replayed or older producer can't refill the collection with copies.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

from confluent_kafka import KafkaError
from confluent_kafka.serialization import MessageField, SerializationContext
from pymongo import ReplaceOne
from voyageai.error import RateLimitError

from .. import db, embed
from ..config import Settings
from . import kafka, noise
from .notify import SOURCE as NOTIFY_SOURCE
from .spool import to_event as checked_event

GROUP = "recall-sink"
DIGEST_SOURCE = "digest"
DIGEST_CHARS = 6_000   # of the joined event starts kept in a digest


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def digest_event(row: dict) -> dict:
    """An events_digest row (window_start, window_end, source, events, kinds, sample) as an event.
    The id names the source and the hour, so a replayed or corrected row replaces the earlier one."""
    start, end = _as_datetime(row["window_start"]), _as_datetime(row["window_end"])
    count = int(row["events"])
    kinds = row.get("kinds") or ""
    sample = (row.get("sample") or "")[:DIGEST_CHARS]
    head = f"{count} event{'s' if count != 1 else ''} from {start:%H:%M} to {end:%H:%M} UTC"
    return {
        "event_id": f"{row['source']}:{start:%Y%m%dT%H%M}",
        "source": DIGEST_SOURCE,
        "kind": row["source"],
        "actor": None,
        "occurred_at": end,
        "body": f"{head}{f' ({kinds})' if kinds else ''}\n{sample}".rstrip(),
        "meta": json.dumps({"window_start": start.isoformat(), "window_end": end.isoformat(), "events": count}),
    }


def to_doc(event: dict) -> dict:
    occurred = _as_datetime(event["occurred_at"])
    body = event.get("body") or ""
    kind = event.get("kind") or event["source"]
    first_line = body.splitlines()[0] if body else ""
    try:
        meta = json.loads(event["meta"]) if event.get("meta") else {}
    except ValueError:
        meta = {"raw": event["meta"]}
    doc = {
        "source": f"event:{event['source']}:{event['event_id']}",
        "title": f"{kind}: {first_line}"[:200],
        "chunk_index": 0,
        "text": f"[{occurred:%Y-%m-%d %H:%M} UTC] {event['source']} / {kind}\n{body}",
        "tags": ["event", event["source"]],
        "created_at": occurred,
        "event": {
            "id": event["event_id"],
            "source": event["source"],
            "kind": event.get("kind"),
            "actor": event.get("actor"),
            "meta": meta,
        },
    }
    if event["source"] == NOTIFY_SOURCE:
        doc["event"]["content_key"] = meta.get("content_key") or noise.content_key(meta.get("app") or kind, body)
    return doc


def _split_body(doc: dict) -> tuple[str, str, str]:
    """(app, summary, body) of a notification doc, for the drop rules."""
    meta = doc["event"]["meta"]
    summary = meta.get("summary") or ""
    text = doc["text"].split("\n", 1)[1] if "\n" in doc["text"] else ""
    body = text[len(summary):].lstrip("\n") if text.startswith(summary) else text
    return meta.get("app") or doc["event"].get("kind") or "", summary, body


def keep(settings: Settings, docs: list[dict], policy: noise.Policy) -> list[dict]:
    """Drop noisy notifications and repeats of ones already stored or earlier in the batch."""
    notes = [d for d in docs if "content_key" in d["event"]]
    if not notes:
        return docs
    window = policy.repeat_window
    oldest = min(d["created_at"] for d in notes) - window
    stored: dict[str, list[tuple[datetime, str]]] = {}
    for row in db.chunks(settings).find(
        {"event.content_key": {"$in": [d["event"]["content_key"] for d in notes]}, "created_at": {"$gte": oldest}},
        {"_id": 0, "source": 1, "created_at": 1, "event.content_key": 1},
    ):
        stored.setdefault(row["event"]["content_key"], []).append((row["created_at"], row["source"]))

    out = []
    for d in sorted(docs, key=lambda d: d["created_at"]):
        key = d["event"].get("content_key")
        if key is None:
            out.append(d)
            continue
        if policy.drop_reason(*_split_body(d)):
            continue
        prior = stored.get(key, [])
        if any(src != d["source"] and abs(d["created_at"] - at) <= window for at, src in prior):
            continue
        stored.setdefault(key, []).append((d["created_at"], d["source"]))
        out.append(d)
    return out


def store(settings: Settings, events: list[dict], policy: noise.Policy | None = None) -> int:
    docs = keep(settings, [to_doc(e) for e in events], policy or noise.load())
    if not docs:
        return 0
    vectors = embed.embed_documents(settings, [d["text"] for d in docs])
    for d, v in zip(docs, vectors):
        d["embedding"] = v
        d["model"] = settings.voyage_model
    ops = [ReplaceOne({"source": d["source"], "chunk_index": 0}, d, upsert=True) for d in docs]
    db.chunks(settings).bulk_write(ops, ordered=False)
    return len(docs)


def run(settings: Settings, batch_size: int = 64, max_wait: float = 30, once: bool = False) -> None:
    """Consume forever (or until the topic is drained, with once=True)."""
    ks = kafka.load()
    consumer, deserialize = kafka.consumer(ks, GROUP)
    policy = noise.load()
    try:
        while True:
            events, deadline = [], time.monotonic() + max_wait
            while len(events) < batch_size and time.monotonic() < deadline:
                msg = consumer.poll(1.0)
                if msg is None:
                    if once and events:
                        break
                    continue
                if msg.error():
                    if msg.error().code() != KafkaError._PARTITION_EOF:
                        print(f"consumer error: {msg.error()}", file=sys.stderr, flush=True)
                    continue
                if msg.topic() == ks.inbox_topic:
                    try:
                        event = checked_event(json.loads(msg.value() or b"null") or {})
                    except (ValueError, TypeError, AttributeError) as e:
                        print(f"skipped a bad inbox record: {e}", file=sys.stderr, flush=True)
                        continue
                    if event is not None:
                        events.append(event)
                    continue
                value = deserialize(msg.value(), SerializationContext(msg.topic(), MessageField.VALUE))
                if value is not None and msg.topic() == ks.digest_topic:
                    events.append(digest_event(value))
                elif value is not None:
                    # Flink keeps key columns (event_id) out of the value by default.
                    key = deserialize(msg.key(), SerializationContext(msg.topic(), MessageField.KEY))
                    events.append({**(key or {}), **value})
            if events:
                while True:
                    try:
                        n = store(settings, events, policy)
                        break
                    except RateLimitError:
                        print("voyage rate limit, retrying in 60s", file=sys.stderr, flush=True)
                        time.sleep(60)
                consumer.commit(asynchronous=False)
                print(f"stored {n} of {len(events)} event(s)", flush=True)
            elif once:
                return
    finally:
        consumer.close()
