"""Atlas connection and the vector search index."""

from __future__ import annotations

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.operations import SearchIndexModel

from .config import Settings

_client: MongoClient | None = None


def client(settings: Settings) -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(settings.mongodb_uri, appname="recall", tz_aware=True, **credentials(settings))
    return _client


def credentials(settings: Settings) -> dict:
    """Username and password given apart from the URI; pymongo lets them replace the URI's own."""
    if not (settings.mongodb_username and settings.mongodb_password):
        return {}
    return {"username": settings.mongodb_username, "password": settings.mongodb_password}


def chunks(settings: Settings) -> Collection:
    return client(settings)[settings.db_name][settings.collection]


def search_collections(settings: Settings) -> list[Collection]:
    """Where search looks: this database first, then the read-only ones (RECALL_READ_DBS).

    Only search calls this. Every write goes through chunks(), so a database
    listed here is read and never changed.
    """
    own = chunks(settings)
    return [own, *(client(settings)[name][settings.collection] for name in settings.read_dbs)]


def turns(settings: Settings) -> Collection:
    """Finished Hermes exchanges in full, kept for `recall learn`. Never searched."""
    return client(settings)[settings.db_name]["turns"]


def retired(settings: Settings) -> Collection:
    """Memories a newer fact replaced. Moved here whole, so no search can find them
    and `recall learned --restore` can move them back."""
    return client(settings)[settings.db_name]["retired"]


def open_items(settings: Settings) -> Collection:
    """Which "Still open" items of session notes were settled, and which notes were looked at
    for that (openitems.py). The notes themselves stay as they were written."""
    return client(settings)[settings.db_name]["open_items"]


def state(settings: Settings) -> Collection:
    """Small bookkeeping documents for `recall learn`: one per conversation, the profile, the import mark."""
    return client(settings)[settings.db_name]["learn_state"]


TEXT_INDEX = {"mappings": {"dynamic": False, "fields": {
    "title": {"type": "string"},
    "text": {"type": "string"},
    "tags": {"type": "token"},
    "created_at": {"type": "date"},     # time words in a query ("last week") filter on it
}}}


def vector_index(settings: Settings) -> dict:
    return {"fields": [
        {"type": "vector", "path": "embedding", "numDimensions": settings.dimensions, "similarity": "cosine"},
        {"type": "filter", "path": "source"},
        {"type": "filter", "path": "tags"},
        {"type": "filter", "path": "created_at"},
    ]}


def _covers(current: dict | None, wanted: dict) -> bool:
    """Whether an index definition already has every field of `wanted` (extra settings are fine)."""
    if not current:
        return False
    if "fields" in wanted:
        have = {(f.get("type"), f.get("path")) for f in current.get("fields", [])}
        return all((f["type"], f["path"]) in have for f in wanted["fields"])
    have = current.get("mappings", {}).get("fields", {})
    return all(have.get(k, {}).get("type") == v["type"] for k, v in wanted["mappings"]["fields"].items())


# The plain indexes on chunks, as (keys, options). `recall init` creates them and `recall doctor`
# checks that they are there.
PLAIN_INDEXES: tuple[tuple[list[tuple[str, int]], dict], ...] = (
    ([("source", 1)], {}),
    ([("source", 1), ("chunk_index", 1)], {"unique": True}),
    ([("event.content_key", 1)], {"sparse": True}),     # the stream sink's repeat check
    ([("created_at", 1)], {}),                          # a time window listed without a subject
    # Lists and counts by kind (memories, events, session notes), newest first. Without it each
    # of those reads every chunk, embedding included.
    ([("tags", 1), ("created_at", -1)], {}),
)


def plain_index_name(keys: list[tuple[str, int]]) -> str:
    """The name MongoDB gives an index made without one: source_1_chunk_index_1."""
    return "_".join(f"{field}_{direction}" for field, direction in keys)


def missing_plain_indexes(coll: Collection) -> list[str]:
    have = set(coll.index_information())
    return [name for keys, _ in PLAIN_INDEXES if (name := plain_index_name(keys)) not in have]


def ensure_indexes(settings: Settings) -> str:
    """Create the search indexes and plain indexes, and bring older search indexes up to date.

    Returns a short status string. Index builds on Atlas are asynchronous: a new
    index shows as 'building' for a minute or so before it is queryable, and an
    updated one keeps answering with its old definition until the rebuild is done.
    """
    coll = chunks(settings)
    new = missing_plain_indexes(coll)
    for keys, options in PLAIN_INDEXES:
        coll.create_index(keys, **options)
    status = [f"created plain index '{name}'" for name in new]
    kept = len(PLAIN_INDEXES) - len(new)
    if kept:
        status.append(f"{kept} {'other ' if new else ''}plain index{'es are' if kept != 1 else ' is'} current")

    existing = {idx["name"]: idx.get("latestDefinition") for idx in coll.list_search_indexes()}
    for name, kind, definition in (
        (settings.text_index_name, "search", TEXT_INDEX),
        (settings.index_name, "vectorSearch", vector_index(settings)),
    ):
        if name not in existing:
            coll.create_search_index(SearchIndexModel(name=name, type=kind, definition=definition))
            status.append(f"created {kind} index '{name}' (building, give it a minute)")
        elif not _covers(existing[name], definition):
            coll.update_search_index(name, definition)
            status.append(f"updated {kind} index '{name}' (rebuilding; the old one answers meanwhile)")
        else:
            status.append(f"index '{name}' is current")
    return "; ".join(status)
