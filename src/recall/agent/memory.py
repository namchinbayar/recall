"""Agent memory: durable facts stored as chunks so search finds them."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .. import db, embed
from ..config import Settings

MEMORY_TAG = "memory"


def remember(
    settings: Settings, fact: str, tags: list[str] | None = None, *,
    vector: list[float] | None = None, created_at: datetime | None = None, extra: dict | None = None,
) -> str:
    """Store one fact as a single chunk. Returns the source id. A fact that looks like it
    holds a secret is refused (ValueError), as sync refuses such a file.
    `vector` saves a second embedding when the caller already has one; `created_at` is
    when the fact was said, for facts learned later; `extra` adds fields to the chunk."""
    from .. import sync

    fact = fact.strip()
    if not fact:
        raise ValueError("empty fact")
    sync.refuse_secret(fact)
    source = f"memory:{uuid.uuid4().hex[:12]}"
    if vector is None:
        vector = embed.embed_documents(settings, [fact])[0]
    db.chunks(settings).insert_one({
        "source": source,
        "title": fact[:72] + ("..." if len(fact) > 72 else ""),
        "chunk_index": 0,
        "text": fact,
        "embedding": vector,
        "tags": sorted({MEMORY_TAG, *(tags or [])}),
        "model": settings.voyage_model,
        "created_at": created_at or datetime.now(timezone.utc),
        **(extra or {}),
    })
    return source


def forget(settings: Settings, source: str) -> int:
    if not source.startswith("memory:"):
        raise ValueError("forget only removes memory:* sources; use `recall rm` for others")
    return db.chunks(settings).delete_many({"source": source}).deleted_count


def recent(settings: Settings, limit: int = 20) -> list[dict]:
    cur = (
        db.chunks(settings)
        .find({"tags": MEMORY_TAG}, {"_id": 0, "source": 1, "text": 1, "tags": 1, "created_at": 1})
        .sort("created_at", -1)
        .limit(limit)
    )
    return list(cur)


def retire(settings: Settings, source: str, why: str, by: str | None = None, session: str | None = None) -> int:
    """Take a memory out of every search without losing it: its chunk moves to the
    `retired` collection with a note of why. Returns how many chunks moved."""
    if not source.startswith("memory:"):
        raise ValueError("only memory:* sources can be retired")
    docs = list(db.chunks(settings).find({"source": source}))
    if not docs:
        return 0
    stamp = {"at": datetime.now(timezone.utc), "why": why[:300], "by": by, "session": session}
    db.retired(settings).delete_many({"source": source})
    db.retired(settings).insert_many([{**d, "retired": stamp} for d in docs])
    db.chunks(settings).delete_many({"source": source})
    return len(docs)


def restore(settings: Settings, source: str) -> int:
    """Put a retired memory back where search finds it."""
    docs = list(db.retired(settings).find({"source": source}))
    if not docs:
        return 0
    for d in docs:
        d.pop("retired", None)
    db.chunks(settings).delete_many({"source": source})
    db.chunks(settings).insert_many(docs)
    db.retired(settings).delete_many({"source": source})
    return len(docs)


def retired(settings: Settings, limit: int = 20) -> list[dict]:
    cur = (
        db.retired(settings)
        .find({}, {"_id": 0, "source": 1, "text": 1, "tags": 1, "created_at": 1, "retired": 1})
        .sort("retired.at", -1)
        .limit(limit)
    )
    return list(cur)
