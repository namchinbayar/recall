"""Apply the noise rules to desktop notifications already stored in Atlas.

Walks every stored notification oldest first, drops the ones a rule matches and
repeats of one kept earlier within the repeat window (the same check the sink
runs on new events), and writes `event.content_key` onto the ones it keeps so
the sink's repeat check can find them. Optionally sets a TTL on notifications.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from pymongo import UpdateOne

from .. import db
from ..config import Settings
from . import noise
from .notify import SOURCE

TTL_INDEX = "notification_ttl"


@dataclass
class Plan:
    delete: list = field(default_factory=list)
    backfill: list[tuple] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)
    kept: int = 0


def _parts(doc: dict) -> tuple[str, str, str, str]:
    """(app, summary, body, full text) from a stored notification chunk."""
    ev = doc.get("event") or {}
    meta = ev.get("meta") or {}
    text = doc["text"].split("\n", 1)[1] if "\n" in doc["text"] else ""
    summary = meta.get("summary") or ""
    body = text[len(summary):].lstrip("\n") if text.startswith(summary) else text
    return meta.get("app") or ev.get("kind") or "", summary, body, text


def plan(settings: Settings, policy: noise.Policy) -> Plan:
    out = Plan()
    last_kept: dict[str, datetime] = {}
    cursor = db.chunks(settings).find(
        {"event.source": SOURCE},
        {"_id": 1, "text": 1, "created_at": 1, "event.kind": 1, "event.meta": 1, "event.content_key": 1},
    ).sort("created_at", 1)
    for doc in cursor:
        app, summary, body, text = _parts(doc)
        reason = policy.drop_reason(app, summary, body)
        key = doc["event"].get("content_key") or noise.content_key(app, text)
        if reason is None:
            at = last_kept.get(key)
            if at is not None and doc["created_at"] - at <= policy.repeat_window:
                reason = "repeat"
        if reason:
            out.delete.append(doc["_id"])
            out.reasons[reason] += 1
            continue
        last_kept[key] = doc["created_at"]
        out.kept += 1
        if doc["event"].get("content_key") != key:
            out.backfill.append((doc["_id"], key))
    return out


def apply(settings: Settings, p: Plan) -> int:
    coll = db.chunks(settings)
    coll.create_index("event.content_key", sparse=True)
    if p.backfill:
        coll.bulk_write([UpdateOne({"_id": i}, {"$set": {"event.content_key": k}}) for i, k in p.backfill], ordered=False)
    deleted = 0
    for i in range(0, len(p.delete), 1000):
        deleted += coll.delete_many({"_id": {"$in": p.delete[i : i + 1000]}}).deleted_count
    return deleted


def set_retention(settings: Settings, days: int) -> str:
    """Expire notifications `days` after they arrived; 0 removes the expiry."""
    coll = db.chunks(settings)
    existing = {ix["name"]: ix for ix in coll.list_indexes()}
    if days <= 0:
        if TTL_INDEX in existing:
            coll.drop_index(TTL_INDEX)
            return "notification expiry removed"
        return "no notification expiry was set"
    seconds = days * 86400
    if TTL_INDEX in existing:
        db.client(settings)[settings.db_name].command(
            "collMod", settings.collection, index={"name": TTL_INDEX, "expireAfterSeconds": seconds}
        )
    else:
        coll.create_index(
            "created_at", name=TTL_INDEX, expireAfterSeconds=seconds,
            partialFilterExpression={"event.source": SOURCE},
        )
    return f"notifications now expire {days} day(s) after they arrive"
