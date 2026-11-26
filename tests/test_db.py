from types import SimpleNamespace

from recall import db, doctor

SETTINGS = SimpleNamespace(index_name="vector_index", text_index_name="text_index", dimensions=1024)
ALL = [db.plain_index_name(keys) for keys, _ in db.PLAIN_INDEXES]


class Collection:
    def __init__(self, have=()):
        self.have = ["_id_", *have]
        self.created, self.search_indexes = [], []

    def index_information(self):
        return {name: {} for name in self.have}

    def create_index(self, keys, **options):
        self.created.append((keys, options))
        name = db.plain_index_name(keys)
        if name not in self.have:
            self.have.append(name)

    def list_search_indexes(self):
        return [{"name": "vector_index", "latestDefinition": db.vector_index(SETTINGS)},
                {"name": "text_index", "latestDefinition": db.TEXT_INDEX}]

    def update_search_index(self, name, definition):
        self.search_indexes.append((name, definition))

    def create_search_index(self, model):
        self.search_indexes.append(model)


def test_plain_indexes_are_named_as_mongodb_names_them():
    assert ALL == ["source_1", "source_1_chunk_index_1", "event.content_key_1", "created_at_1", "tags_1_created_at_-1"]


def test_init_indexes_what_the_lists_and_counts_filter_on(monkeypatch):
    coll = Collection()
    monkeypatch.setattr(db, "chunks", lambda settings: coll)
    status = db.ensure_indexes(SETTINGS)
    # memories, events and session notes are all read as "this tag, newest first"
    assert ([("tags", 1), ("created_at", -1)], {}) in coll.created
    assert ([("source", 1), ("chunk_index", 1)], {"unique": True}) in coll.created
    assert coll.search_indexes == [] and "index 'vector_index' is current" in status


def test_init_says_which_plain_indexes_it_created(monkeypatch):
    coll = Collection(have=ALL[:-1])
    monkeypatch.setattr(db, "chunks", lambda settings: coll)
    first = db.ensure_indexes(SETTINGS)
    assert first.startswith("created plain index 'tags_1_created_at_-1'; 4 other plain indexes are current; ")
    again = db.ensure_indexes(SETTINGS)
    assert again.startswith("5 plain indexes are current; ") and "created" not in again


def test_doctor_warns_about_a_missing_plain_index_without_alerting(monkeypatch):
    coll = Collection(have=ALL[:-1])
    monkeypatch.setattr(db, "chunks", lambda settings: coll)
    check = doctor.check_plain_indexes(SETTINGS)
    assert (check.ok, check.warn, check.failed) == (False, True, False)
    assert "tags_1_created_at_-1" in check.detail and "recall init" in check.detail
    coll.have.append("tags_1_created_at_-1")
    assert doctor.check_plain_indexes(SETTINGS) == doctor.Check("plain indexes", True, "all 5 present")
