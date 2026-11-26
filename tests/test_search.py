from datetime import datetime, timedelta, timezone

import pytest

from recall import search
from recall.config import Settings
from recall.search import Hit

NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
SETTINGS = Settings(mongodb_uri="mongodb://x", voyage_api_key="k")


def hit(title, days_old=0, score=0.0):
    return Hit(source=f"s:{title}", title=title, chunk_index=0, text=title, score=score, tags=[],
               created_at=NOW - timedelta(days=days_old))


@pytest.mark.parametrize("tags, events, excluded", [
    (None, False, ["vault", "event"]),
    (None, True, ["vault"]),
    (["hermes"], False, ["vault"]),
])
def test_filters_always_exclude_vault(tags, events, excluded):
    vf = search.vector_filter(tags, events)
    ts = search.text_stage(SETTINGS, "q", tags, events)["$search"]["compound"]
    assert ts["mustNot"] == [{"in": {"path": "tags", "value": excluded}}]
    nin = vf["$and"][0] if tags else vf
    assert nin == {"tags": {"$nin": excluded}}
    if tags:
        assert vf["$and"][1] == {"tags": {"$in": tags}}
        assert ts["filter"] == [{"in": {"path": "tags", "value": tags}}]


def test_exclude_tags_reach_both_retrievers_and_cannot_readmit_the_vault():
    out = ["vault", "desktop-notification", "hermes-memory"]
    assert search.vector_filter(None, True, ["hermes-memory", "desktop-notification", "vault"]) == {"tags": {"$nin": out}}
    ts = search.text_stage(SETTINGS, "q", None, True, ["hermes-memory", "desktop-notification"])["$search"]["compound"]
    assert ts["mustNot"] == [{"in": {"path": "tags", "value": out}}]
    assert search._excluded(None, False, ["event"]) == ["vault", "event"]


def test_recency_bonus_halves():
    full = search.recency_bonus(NOW, NOW)
    assert full == pytest.approx(search.RECENCY_WEIGHT)
    assert search.recency_bonus(NOW - timedelta(days=search.RECENCY_HALF_LIFE_DAYS), NOW) == pytest.approx(full / 2)
    assert search.recency_bonus(None, NOW) == 0


def test_rank_uses_rerank_scores_and_limit():
    hits = [hit("a"), hit("b"), hit("c")]
    ranked = search.rank("q", hits, 2, lambda q, docs: [0.1, 0.9, 0.5], now=NOW)
    assert [h.title for h in ranked] == ["b", "c"]


def test_recency_breaks_near_ties_only():
    old, new = hit("old", days_old=365), hit("new", days_old=0)
    assert search.rank("q", [old, new], 2, lambda q, d: [0.80, 0.79], now=NOW)[0].title == "new"
    old, new = hit("old", days_old=365), hit("new", days_old=0)
    assert search.rank("q", [old, new], 2, lambda q, d: [0.80, 0.60], now=NOW)[0].title == "old"


def test_rank_without_reranker_keeps_retrieval_order():
    hits = [hit("a"), hit("b"), hit("c")]
    assert [h.title for h in search.rank("q", hits, 3, None, now=NOW)] == ["a", "b", "c"]


def test_search_falls_back_to_vector_only(monkeypatch):
    from pymongo.errors import OperationFailure

    calls = []

    class Coll:
        def aggregate(self, pipeline):
            calls.append(next(iter(pipeline[0])))
            if "$rankFusion" in pipeline[0]:
                raise OperationFailure("no such index")
            return [{"source": "s", "title": "t", "chunk_index": 0, "text": "x", "tags": [], "score": 0.9}]

    monkeypatch.setattr(search.db, "chunks", lambda s: Coll())
    monkeypatch.setattr(search.embed, "embed_query", lambda s, q: [0.0])
    hits = search.search(Settings(mongodb_uri="m", voyage_api_key="k", rerank_model=None), "q", limit=1)
    assert calls == ["$rankFusion", "$vectorSearch"]
    assert [h.title for h in hits] == ["t"]


def test_excerpt_centres_on_query_words():
    from recall.agent.tools import excerpt

    text = "a" * 2000 + " the compositor reloads config.kdl on save " + "b" * 2000
    out = excerpt(text, "how does niri reload config.kdl", 300)
    assert "config.kdl" in out and len(out) <= 300 + 8
    assert out.startswith("... ") and out.endswith(" ...")
    assert excerpt("short text", "anything", 300) == "short text"
    assert excerpt("x" * 500, "nomatch", 100) == "x" * 100 + " ..."


class FakeColl:
    """Stands in for one database's chunks collection and records what touched it."""

    def __init__(self, name, rows=()):
        self.name, self.rows, self.calls = name, list(rows), []

    def aggregate(self, pipeline):
        self.calls.append("aggregate")
        return [dict(r) for r in self.rows]

    def __getattr__(self, op):          # any write (insert_one, delete_many, ...) is recorded
        def call(*a, **k):
            self.calls.append(op)
            return type("R", (), {"deleted_count": 0})()
        return call


class FakeClient:
    def __init__(self, colls):
        self.colls = colls

    def __getitem__(self, db_name):
        return {"chunks": self.colls[db_name]}


def row(title, score):
    return {"source": f"s:{title}", "title": title, "chunk_index": 0, "text": title, "tags": [], "score": score}


def test_read_dbs_setting_drops_blanks_repeats_and_itself(monkeypatch):
    from recall import config
    monkeypatch.setenv("MONGODB_URI", "mongodb://x")
    monkeypatch.setenv("VOYAGE_API_KEY", "k")
    monkeypatch.setenv("RECALL_DB", "den")
    monkeypatch.setenv("RECALL_READ_DBS", " recall, den ,,recall,other ")
    assert config.load().read_dbs == ("recall", "other")
    monkeypatch.delenv("RECALL_READ_DBS")
    assert config.load().read_dbs == ()


def test_search_reads_every_database_and_interleaves_by_score(monkeypatch):
    own, shared = FakeColl("den", [row("mine", 0.4)]), FakeColl("recall", [row("theirs", 0.9), row("low", 0.1)])
    monkeypatch.setattr(search.db, "client", lambda s: FakeClient({"den": own, "recall": shared}))
    settings = Settings(mongodb_uri="mongodb://x", voyage_api_key="k", db_name="den", read_dbs=("recall",))
    hits = search.candidates(settings, "q", [0.0], 5, None, False)
    assert [h.title for h in hits] == ["theirs", "mine", "low"]
    assert own.calls == ["aggregate"] and shared.calls == ["aggregate"]


def test_writes_never_reach_a_read_only_database(monkeypatch):
    from recall import ingest
    from recall.agent import memory
    own, shared = FakeColl("den"), FakeColl("recall")
    monkeypatch.setattr(search.db, "client", lambda s: FakeClient({"den": own, "recall": shared}))
    monkeypatch.setattr(ingest.embed, "embed_documents", lambda s, texts: [[0.0] for _ in texts])
    settings = Settings(mongodb_uri="mongodb://x", voyage_api_key="k", db_name="den", read_dbs=("recall",))
    memory.remember(settings, "a private fact")
    memory.forget(settings, "memory:abc")
    ingest.ingest_text(settings, "a private note", "note")
    ingest.remove(settings, "note:abc")
    assert own.calls == ["insert_one", "delete_many", "delete_many", "insert_many", "delete_many"]
    assert shared.calls == []


def test_one_database_keeps_retrieval_order(monkeypatch):
    own = FakeColl("recall", [row("first", 0.2), row("second", 0.9)])
    monkeypatch.setattr(search.db, "client", lambda s: FakeClient({"recall": own}))
    assert [h.title for h in search.candidates(SETTINGS, "q", [0.0], 5, None, False)] == ["first", "second"]


def test_separate_database_user_replaces_the_one_in_the_uri(monkeypatch):
    from recall import config, db
    monkeypatch.setenv("MONGODB_URI", "mongodb://main:mainpw@localhost/")
    monkeypatch.setenv("VOYAGE_API_KEY", "k")
    assert db.credentials(config.load()) == {}
    monkeypatch.setenv("MONGODB_USERNAME", "private")
    monkeypatch.setenv("MONGODB_PASSWORD", "p@ss/word")
    creds = db.credentials(config.load())
    assert creds == {"username": "private", "password": "p@ss/word"}
    monkeypatch.delenv("MONGODB_PASSWORD")
    assert db.credentials(config.load()) == {}


# ----- time windows and the per-source cap -----------------------------------

from recall import when as when_mod  # noqa: E402

WINDOW = when_mod.Window(datetime(2026, 11, 23, 8, tzinfo=timezone.utc), datetime(2026, 11, 24, 8, tzinfo=timezone.utc), "yesterday")


def test_a_window_reaches_both_retrievers():
    vf = search.vector_filter(None, False, None, WINDOW)
    assert vf["$and"][1] == {"created_at": {"$gte": WINDOW.start, "$lt": WINDOW.end}}
    ts = search.text_stage(SETTINGS, "q", ["hermes"], False, None, WINDOW)["$search"]["compound"]
    assert ts["filter"][1] == {"range": {"path": "created_at", "gte": WINDOW.start, "lt": WINDOW.end}}
    assert search.vector_filter(None, False) == {"tags": {"$nin": ["vault", "event"]}}    # no window, no clause


def test_per_source_cap_keeps_best_first():
    hits = [Hit("a", "a", i, "", 1 - i / 10, []) for i in range(4)] + [Hit("b", "b", 0, "", 0.1, [])]
    assert [(h.source, h.chunk_index) for h in search.per_source(hits, 2)] == [("a", 0), ("a", 1), ("b", 0)]
    assert len(search.per_source(hits, None)) == 5


def test_search_reads_the_time_out_of_the_query(monkeypatch):
    seen = {}

    def fake_candidates(settings, query, vector, n, tags, events, exclude, window):
        seen.update(query=query, window=window)
        return [hit("x")]

    monkeypatch.setattr(search.when_mod, "zone", lambda: timezone.utc)
    monkeypatch.setattr(search.embed, "embed_query", lambda s, q: seen.setdefault("embedded", q) and [0.0])
    monkeypatch.setattr(search, "candidates", fake_candidates)
    out = search.search(Settings(mongodb_uri="m", voyage_api_key="k", rerank_model=None), "niri crash last week", when=True)
    assert seen["query"] == seen["embedded"] == "niri crash"
    assert seen["window"].phrase == "last week" and out.window is seen["window"]
    out = search.search(Settings(mongodb_uri="m", voyage_api_key="k", rerank_model=None), "niri crash last week")
    assert seen["query"] == "niri crash last week" and out.window is None             # off unless asked


class BrowseColl:
    def __init__(self, rows):
        self.rows, self.match = rows, None

    def find(self, match, projection):
        self.match = match
        coll = self

        class Cursor:
            def sort(self, *a):
                return self

            def limit(self, n):
                return [dict(r) for r in coll.rows]
        return Cursor()


def test_a_query_that_is_only_a_time_lists_the_window_newest_first(monkeypatch):
    at = lambda h: datetime(2026, 11, 23, h, tzinfo=timezone.utc)     # noqa: E731
    coll = BrowseColl([
        {"source": "session:a", "title": "A", "chunk_index": 1, "text": "a1", "tags": ["session-note"], "created_at": at(9)},
        {"source": "session:a", "title": "A", "chunk_index": 0, "text": "a0", "tags": ["session-note"], "created_at": at(9)},
        {"source": "session:b", "title": "B", "chunk_index": 0, "text": "b0", "tags": ["session-note"], "created_at": at(20)},
        {"source": "file:x", "title": "X", "chunk_index": 0, "text": "x0", "tags": ["claude"], "created_at": at(23)},
    ])
    monkeypatch.setattr(search.db, "chunks", lambda s: coll)
    monkeypatch.setattr(search.embed, "embed_query", lambda s, q: pytest.fail("nothing to embed"))
    out = search.search(SETTINGS, "what did I work on", when=True, since=WINDOW.start, until=WINDOW.end)
    assert [(h.title, h.text) for h in out] == [("B", "b0"), ("A", "a0"), ("X", "x0")]   # notes before files
    assert coll.match["$and"][1] == {"created_at": {"$gte": WINDOW.start, "$lt": WINDOW.end}}


def test_an_index_without_the_date_field_filters_after_retrieval(monkeypatch):
    from pymongo.errors import OperationFailure

    class Coll:
        def aggregate(self, pipeline):
            stage = pipeline[0].get("$rankFusion", {}).get("input", {}).get("pipelines", {}).get("vector", [{}])[0]
            if "created_at" in str(stage.get("$vectorSearch", {}).get("filter")):
                raise OperationFailure("Path 'created_at' needs to be indexed as filter")
            return [{**row("inside", 0.9), "created_at": datetime(2026, 11, 23, 12, tzinfo=timezone.utc)},
                    {**row("outside", 0.8), "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc)}]

    monkeypatch.setattr(search.db, "chunks", lambda s: Coll())
    assert [h.title for h in search.candidates(SETTINGS, "q", [0.0], 5, None, False, None, WINDOW)] == ["inside"]


# ----- session notes step below newer material -------------------------------

def dated(source, score, days_old, *tags):
    return Hit(source=source, title=source, chunk_index=0, text="", score=score, tags=list(tags),
               created_at=NOW - timedelta(days=days_old))


def order(hits, margin=0.1):
    return [h.source for h in search.yield_to_newer(hits, margin)]


def test_a_session_note_steps_below_a_newer_note_that_answers_nearly_as_well():
    review = dated("session:review", 0.92, 16, "session-note", "claude")
    removed = dated("file:removed.md", 0.87, 5, "memory-note")
    other = dated("session:other", 0.72, 3, "session-note")
    assert order([review, removed, other]) == ["file:removed.md", "session:review", "session:other"]
    assert review.score == pytest.approx(0.869) and removed.score == 0.87      # only the session note's score changes


def test_a_session_note_keeps_its_place_when_nothing_newer_comes_close():
    def session(score=0.92, days_old=16):
        return dated("session:s", score, days_old, "session-note")

    assert order([session(), dated("file:old.md", 0.87, 30, "memory-note")]) == ["session:s", "file:old.md"]      # older
    assert order([session(), dated("file:far.md", 0.80, 1, "memory-note")]) == ["session:s", "file:far.md"]       # not close
    assert order([session(), dated("event:e", 0.90, 1, "event", "hermes")]) == ["session:s", "event:e"]           # an event
    assert order([session(), dated("session:t", 0.90, 1, "session-note")]) == ["session:s", "session:t"]          # another record
    assert order([session(), dated("file:new.md", 0.87, 5, "memory-note")], margin=0) == ["session:s", "file:new.md"]
    undated = Hit("file:u.md", "u", 0, "", 0.9, ["memory-note"])
    assert order([session(), undated]) == ["session:s", "file:u.md"]


def test_a_session_note_goes_under_the_best_of_several_newer_notes_and_no_further():
    hits = [dated("session:s", 0.90, 20, "session-note"), dated("memory:a", 0.88, 2, "memory"),
            dated("https://page", 0.85, 1), dated("file:far.md", 0.70, 1, "memory-note")]
    assert order(hits) == ["memory:a", "session:s", "https://page", "file:far.md"]


def test_search_applies_it_only_when_the_reranker_scored_the_hits(monkeypatch):
    found = [dated("session:s", 0.0, 16, "session-note"), dated("file:new.md", 0.0, 5, "memory-note")]
    monkeypatch.setattr(search.embed, "embed_query", lambda s, q: [0.0])
    monkeypatch.setattr(search, "candidates", lambda *a, **k: [Hit(**vars(h)) for h in found])
    monkeypatch.setattr(search.embed, "rerank", lambda s, q, docs: [0.92, 0.87])
    monkeypatch.setattr(search, "RECENCY_WEIGHT", 0)
    assert [h.source for h in search.search(SETTINGS, "q")] == ["file:new.md", "session:s"]
    assert [h.source for h in search.search(SETTINGS, "q", rerank=False)] == ["session:s", "file:new.md"]
