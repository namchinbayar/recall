"""Search over stored chunks: hybrid retrieval, then a rerank, then a recency nudge.

1. Retrieve: `$rankFusion` merges Atlas Vector Search (meaning) with Atlas
   Search full text (exact words: names, ids, error strings) by reciprocal
   rank. Without the text index (or on a server that can't fuse) it falls
   back to vector search alone.
2. Rerank: Voyage's reranker reads the query next to each candidate and
   scores relevance directly, which beats either retriever's own score.
3. Recency: a small bonus that halves every RECENCY_HALF_LIFE_DAYS, so of two
   equally relevant chunks the newer one wins. It never outweighs relevance.
4. One source, one place: a long note matching in five chunks would fill the
   page; by default only the best chunk of each source is returned.

Time words ("yesterday", "last week", "in October", "since 2026-11-20") are
read by when.parse and become a created_at window, in both retrievers. When
nothing is left of the query but the time ("what did I do yesterday"), there
is nothing to rank by meaning, so the window is listed newest first.

Vault entries are encrypted and must never surface here or in the agent.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from pymongo.errors import OperationFailure

from . import db, embed, when as when_mod
from .config import Settings

RECENCY_WEIGHT = float(os.environ.get("RECALL_RECENCY_WEIGHT", "0.05"))
RECENCY_HALF_LIFE_DAYS = float(os.environ.get("RECALL_RECENCY_HALF_LIFE_DAYS", "30"))
PER_SOURCE = 1
# A window listed without a subject: what they worked on first, then what they told an assistant, then the rest.
BROWSE_ORDER = ("session-note", "memory")
BROWSE_CHUNKS = 400     # read at most this many chunks of a window listed without a subject


@dataclass
class Hit:
    source: str
    title: str
    chunk_index: int
    text: str
    score: float
    tags: list[str]
    created_at: datetime | None = None
    session: str | None = None   # events only: the conversation the exchange belongs to


class Results(list):
    """Hits, and the time window the query named (None when it named none)."""
    window: when_mod.Window | None = None


def _date_range(window: when_mod.Window | None) -> dict:
    rng = {}
    if window and window.start:
        rng["$gte"] = window.start
    if window and window.end:
        rng["$lt"] = window.end
    return rng


def _excluded(tags: list[str] | None, include_events: bool, exclude_tags: list[str] | None = None) -> list[str]:
    base = ["vault"] if tags or include_events else ["vault", "event"]
    return base + sorted(set(exclude_tags or ()) - set(base))


def vector_filter(tags: list[str] | None, include_events: bool, exclude_tags: list[str] | None = None,
                  window: when_mod.Window | None = None) -> dict:
    not_excluded = {"tags": {"$nin": _excluded(tags, include_events, exclude_tags)}}
    parts = [not_excluded]
    if tags:
        parts.append({"tags": {"$in": tags}})
    if rng := _date_range(window):
        parts.append({"created_at": rng})
    return {"$and": parts} if len(parts) > 1 else not_excluded


def text_stage(
    settings: Settings, query: str, tags: list[str] | None, include_events: bool, exclude_tags: list[str] | None = None,
    window: when_mod.Window | None = None,
) -> dict:
    compound: dict = {
        "should": [
            {"text": {"query": query, "path": "title", "score": {"boost": {"value": 2}}}},
            {"text": {"query": query, "path": "text"}},
        ],
        "minimumShouldMatch": 1,
        "mustNot": [{"in": {"path": "tags", "value": _excluded(tags, include_events, exclude_tags)}}],
    }
    filters = [{"in": {"path": "tags", "value": tags}}] if tags else []
    if rng := _date_range(window):
        filters.append({"range": {"path": "created_at", **{k.strip("$"): v for k, v in rng.items()}}})
    if filters:
        compound["filter"] = filters
    return {"$search": {"index": settings.text_index_name, "compound": compound}}


_PROJECT = {"$project": {
    "_id": 0, "source": 1, "title": 1, "chunk_index": 1, "text": 1, "tags": 1, "created_at": 1,
    "session": "$event.meta.session_id", "score": {"$meta": "score"},
}}


def _pipelines(settings: Settings, query: str, vector: list[float], n: int, tags: list[str] | None,
               include_events: bool, exclude_tags: list[str] | None, window: when_mod.Window | None,
               ) -> tuple[list[dict], list[dict]]:
    """(fused, vector only) for one window; the window is applied inside both retrievers."""
    vector_stage = {"$vectorSearch": {
        "index": settings.index_name,
        "path": "embedding",
        "queryVector": vector,
        "numCandidates": max(n * 20, 100),
        "limit": n,
        "filter": vector_filter(tags, include_events, exclude_tags, window),
    }}
    fused = [
        {"$rankFusion": {"input": {"pipelines": {
            "vector": [vector_stage],
            "text": [text_stage(settings, query, tags, include_events, exclude_tags, window), {"$limit": n}],
        }}}},
        {"$limit": n},
        _PROJECT,
    ]
    plain = [vector_stage, {**_PROJECT, "$project": {**_PROJECT["$project"], "score": {"$meta": "vectorSearchScore"}}}]
    return fused, plain


def _unindexed_date(exc: OperationFailure) -> bool:
    """An index built before created_at was a filter field (`recall init` adds it)."""
    return "created_at" in str(exc.details or exc)


def _in_window(h: Hit, window: when_mod.Window | None) -> bool:
    if window is None or h.created_at is None:
        return window is None
    at = h.created_at if h.created_at.tzinfo else h.created_at.replace(tzinfo=timezone.utc)
    return (window.start is None or at >= window.start) and (window.end is None or at < window.end)


def candidates(
    settings: Settings, query: str, vector: list[float], n: int, tags: list[str] | None, include_events: bool,
    exclude_tags: list[str] | None = None, window: when_mod.Window | None = None,
) -> list[Hit]:
    colls = db.search_collections(settings)
    found: list[Hit] = []
    for coll in colls:
        found += _from_one(coll, settings, query, vector, n, tags, include_events, exclude_tags, window)
    if len(colls) > 1:      # each database ranked its own; interleave so no reranker still sees best first
        found.sort(key=lambda h: h.score, reverse=True)
    return found


def _from_one(coll, settings, query, vector, n, tags, include_events, exclude_tags, window) -> list[Hit]:
    fused, plain = _pipelines(settings, query, vector, n, tags, include_events, exclude_tags, window)
    try:
        return [Hit(**d) for d in coll.aggregate(fused)]
    except OperationFailure as exc:
        if "HostUnreachable" in str(exc):
            raise
        if window is not None and _unindexed_date(exc):
            # The index cannot filter by date yet: fetch wider without the window and filter here.
            wide = _from_one(coll, settings, query, vector, n * 5, tags, include_events, exclude_tags, None)
            return [h for h in wide if _in_window(h, window)]
        # No text index yet, or a server without $rankFusion: vector search alone.
        print(f"hybrid search unavailable, using vector only: {exc.details or exc}", file=sys.stderr)
    return [Hit(**d) for d in coll.aggregate(plain)]


def browse(settings: Settings, window: when_mod.Window, limit: int, tags: list[str] | None,
           include_events: bool, exclude_tags: list[str] | None = None) -> list[Hit]:
    """What a window holds, one chunk per source (the first), with no subject to rank by: session
    notes first, then memories, then everything else, each newest first."""
    match = vector_filter(tags, include_events, exclude_tags, window)
    found: list[Hit] = []
    for coll in db.search_collections(settings):
        rows = coll.find(match, {"_id": 0, "source": 1, "title": 1, "chunk_index": 1, "text": 1, "tags": 1,
                                 "created_at": 1, "event": 1}).sort("created_at", -1).limit(BROWSE_CHUNKS)
        for d in rows:
            session = ((d.pop("event", None) or {}).get("meta") or {}).get("session_id")
            found.append(Hit(**d, score=0.0, session=session))
    found.sort(key=lambda h: (h.created_at or datetime.min.replace(tzinfo=timezone.utc), -h.chunk_index), reverse=True)
    first: dict[str, Hit] = {}
    for h in found:
        if h.source not in first or h.chunk_index < first[h.source].chunk_index:
            first[h.source] = h
    def kind(h: Hit) -> int:
        return next((i for i, tag in enumerate(BROWSE_ORDER) if tag in h.tags), len(BROWSE_ORDER))

    out = sorted(first.values(), key=lambda h: h.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    out.sort(key=kind)          # stable: newest first within each kind
    for i, h in enumerate(out):
        h.score = round(1.0 - i / max(len(out), 1), 3)       # list order, shown as a score
    return out[:limit]


def per_source(hits: list[Hit], cap: int | None) -> list[Hit]:
    """Best first, at most `cap` chunks of any one source."""
    if not cap:
        return hits
    seen: dict[str, int] = {}
    out = []
    for h in hits:
        if seen.get(h.source, 0) < cap:
            seen[h.source] = seen.get(h.source, 0) + 1
            out.append(h)
    return out


def recency_bonus(created_at: datetime | None, now: datetime) -> float:
    if created_at is None or RECENCY_WEIGHT <= 0:
        return 0.0
    age_days = max((now - created_at).total_seconds() / 86400, 0)
    return RECENCY_WEIGHT * math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)


def rank(query: str, hits: list[Hit], limit: int, rerank_fn=None, now: datetime | None = None) -> list[Hit]:
    """Order candidates by rerank score (or retrieval order) plus the recency bonus."""
    now = now or datetime.now(timezone.utc)
    if rerank_fn is not None and hits:
        scores = rerank_fn(query, [f"{h.title}\n{h.text}" for h in hits])
    else:
        scores = [1.0 / (60 + i) * 60 for i in range(len(hits))]  # keep retrieval order, scaled to ~1
    for h, s in zip(hits, scores):
        h.score = s + recency_bonus(h.created_at, now)
    return sorted(hits, key=lambda h: h.score, reverse=True)[:limit]


def search(
    settings: Settings,
    query: str,
    limit: int = 5,
    tags: list[str] | None = None,
    include_events: bool = False,
    rerank: bool = True,
    pool: int | None = None,
    exclude_tags: list[str] | None = None,
    when: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
    cap: int | None = PER_SOURCE,
) -> Results:
    """Events (notifications, Hermes exchanges) are left out unless include_events
    is set or tags are given, so they don't crowd out saved material. `exclude_tags`
    drops chunks with any of those tags before ranking, so they take no place among
    the candidates. `pool` is how many candidates the reranker reads (default: four
    per result, at least 30).

    `when` reads a time phrase out of the query (see when.py); `since` and `until`
    set the window directly and win over it. `cap` is how many chunks of one source
    may be returned (None: no limit)."""
    window, rest = when_mod.parse(query) if when else (None, query)
    if since or until:
        window = when_mod.Window(since or (window.start if window else None),
                                 until or (window.end if window else None), "")
    out = Results()
    out.window = window
    if window is not None and not when_mod.topical(rest):
        out += browse(settings, window, limit, tags, include_events, exclude_tags)
        return out
    query = rest if window is not None and rest else query
    vector = embed.embed_query(settings, query)
    n = max(pool, limit) if pool else max(limit * 4, 30)
    found = candidates(settings, query, vector, n, tags, include_events, exclude_tags, window)
    use_rerank = rerank and bool(settings.rerank_model)
    ranked = rank(query, found, len(found), (lambda q, docs: embed.rerank(settings, q, docs)) if use_rerank else None)
    out += per_source(ranked, cap)[:limit]
    return out
