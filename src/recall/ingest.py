"""Chunk, embed, and store a source. Re-ingesting a source replaces it."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from . import chunk, db, embed, sources
from .config import Settings


def ingest(
    settings: Settings,
    target: str,
    title: str | None = None,
    tags: list[str] | None = None,
) -> tuple[str, int]:
    """Returns (source, number_of_chunks)."""
    source, resolved_title, text = sources.load(target, title)
    return store(settings, source, resolved_title, text, tags)


def ingest_text(settings: Settings, text: str, title: str, tags: list[str] | None = None) -> tuple[str, int]:
    """Store a piece of text (a note, a snippet). The same text saved twice replaces itself.
    A note that looks like it holds a secret is refused (ValueError), as sync refuses such a file."""
    from . import sync   # sync imports this module

    sync.refuse_secret(title)
    sync.refuse_secret(text)
    digest = hashlib.sha256(text.strip().encode()).hexdigest()[:12]
    return store(settings, f"note:{digest}", title.strip() or f"note {digest}", text, tags)


def store(
    settings: Settings, source: str, resolved_title: str, text: str,
    tags: list[str] | None = None, extra: dict | None = None,
) -> tuple[str, int]:
    """extra: fields added to every chunk (folder sync records the file's hash there)."""
    pieces = chunk.split(text)
    if not pieces:
        raise SystemExit(f"no text extracted from {source}")

    vectors = embed.embed_documents(settings, pieces)
    now = datetime.now(timezone.utc)
    docs = [
        {
            "source": source,
            "title": resolved_title,
            "chunk_index": i,
            "text": piece,
            "embedding": vec,
            "tags": tags or [],
            "model": settings.voyage_model,
            "created_at": now,
            **(extra or {}),
        }
        for i, (piece, vec) in enumerate(zip(pieces, vectors))
    ]

    coll = db.chunks(settings)
    coll.delete_many({"source": source})
    coll.insert_many(docs)
    return source, len(docs)


def remove(settings: Settings, source: str) -> int:
    return db.chunks(settings).delete_many({"source": source}).deleted_count


def list_sources(settings: Settings) -> list[dict]:
    pipeline = [
        {"$match": {"tags": {"$nin": ["vault", "event"]}}},
        {"$group": {
            "_id": "$source",
            "title": {"$first": "$title"},
            "chunks": {"$sum": 1},
            "tags": {"$first": "$tags"},
            "created_at": {"$max": "$created_at"},
        }},
        {"$sort": {"created_at": -1}},
    ]
    return list(db.chunks(settings).aggregate(pipeline))


def recent_events(settings: Settings, since: datetime, source: str | None = None, limit: int = 30) -> list[dict]:
    """Stream events (notifications, Hermes exchanges) newest first, without a search."""
    query: dict = {"tags": "event", "created_at": {"$gte": since}}
    if source:
        query["event.source"] = source
    cur = (
        db.chunks(settings)
        .find(query, {"_id": 0, "title": 1, "text": 1, "tags": 1, "created_at": 1, "event.source": 1})
        .sort("created_at", -1)
        .limit(limit)
    )
    return list(cur)
