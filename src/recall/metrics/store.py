"""The `metrics` time series collection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pymongo.collection import Collection
from pymongo.errors import CollectionInvalid

from .. import db
from ..config import Settings

COLLECTION = "metrics"


def metrics(settings: Settings) -> Collection:
    return db.client(settings)[settings.db_name][COLLECTION]


def ensure_collection(settings: Settings) -> str:
    database = db.client(settings)[settings.db_name]
    try:
        database.create_collection(
            COLLECTION,
            timeseries={"timeField": "ts", "metaField": "meta", "granularity": "minutes"},
        )
        return f"created time series collection '{COLLECTION}'"
    except CollectionInvalid:
        return f"collection '{COLLECTION}' already exists"


def insert(settings: Settings, doc: dict) -> None:
    metrics(settings).insert_one(doc)


def parse_since(text: str) -> datetime:
    """'90m', '24h', '7d' -> UTC datetime that far in the past."""
    units = {"m": "minutes", "h": "hours", "d": "days"}
    n, u = int(text[:-1]), text[-1]
    return datetime.now(timezone.utc) - timedelta(**{units[u]: n})


def summary(settings: Settings, since: str, host: str | None, bucket: str) -> list[dict]:
    """Average system metrics per time bucket.

    bucket is a $dateTrunc unit: 'minute', 'hour', 'day'.
    """
    match: dict = {"ts": {"$gte": parse_since(since)}, "meta.kind": "system"}
    if host:
        match["meta.host"] = host
    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": {"$dateTrunc": {"date": "$ts", "unit": bucket}},
            "cpu_pct": {"$avg": "$cpu_pct"},
            "load1": {"$avg": "$load1"},
            "mem_pct": {"$avg": "$mem_pct"},
            "cpu_temp": {"$avg": "$cpu_temp"},
            "samples": {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]
    return list(metrics(settings).aggregate(pipeline))


def manual(settings: Settings, since: str, name: str | None) -> list[dict]:
    match: dict = {"ts": {"$gte": parse_since(since)}, "meta.kind": "manual"}
    if name:
        match["meta.name"] = name
    return list(metrics(settings).find(match, {"_id": 0}).sort("ts", 1))
