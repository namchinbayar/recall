from types import SimpleNamespace

from recall import db


class Collection:
    def __init__(self):
        self.indexes, self.search_indexes = [], []

    def create_index(self, keys, **options):
        self.indexes.append(keys)

    def list_search_indexes(self):
        settings = SimpleNamespace(dimensions=1024)
        return [{"name": "vector_index", "latestDefinition": db.vector_index(settings)},
                {"name": "text_index", "latestDefinition": db.TEXT_INDEX}]

    def update_search_index(self, name, definition):
        self.search_indexes.append((name, definition))

    def create_search_index(self, model):
        self.search_indexes.append(model)


def test_init_indexes_what_the_lists_and_counts_filter_on(monkeypatch):
    coll = Collection()
    monkeypatch.setattr(db, "chunks", lambda settings: coll)
    status = db.ensure_indexes(SimpleNamespace(index_name="vector_index", text_index_name="text_index", dimensions=1024))
    # memories, events and session notes are all read as "this tag, newest first"
    assert [("tags", 1), ("created_at", -1)] in coll.indexes
    assert [("source", 1), ("chunk_index", 1)] in coll.indexes
    assert coll.search_indexes == [] and "is current" in status
