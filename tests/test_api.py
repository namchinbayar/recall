import time
from datetime import datetime, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from recall import api, online, search

URL = "https://recall.example/mcp"
ISSUER = "https://auth.example/v1/apps/P123"
ME = "U-me"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
WHEN = datetime(2026, 11, 18, 9, 30, tzinfo=timezone.utc)
MEMORY = "memory:0123456789ab"
OPEN = "open:0123456789ab"


def token(**changes):
    claims = {
        "iss": ISSUER, "aud": URL, "sub": ME, "exp": int(time.time()) + 600,
        "scope": " ".join(online.SCOPES), "azp": "client-1",
    } | changes
    return jwt.encode(claims, KEY, algorithm="RS256")


def bearer(**changes):
    return {"Authorization": f"Bearer {token(**changes)}"}


@pytest.fixture
def http():
    settings = online.load_settings({"RECALL_ONLINE_URL": URL, "RECALL_ONLINE_ISSUER": ISSUER, "RECALL_ONLINE_SUBJECTS": ME})
    verifier = online.JwtVerifier(settings, key_for=lambda _token: KEY.public_key())
    app = online.build(settings, verifier).streamable_http_app(streamable_http_path=settings.path)
    with TestClient(app, base_url=settings.public_origin) as client:
        yield client


@pytest.fixture
def calls(monkeypatch):
    """Replace the work behind each route, so no test touches a database, and record the calls."""
    seen = []

    def fake(name, answer):
        def call(*args):
            seen.append((name, *args))
            return answer(*args) if callable(answer) else answer
        monkeypatch.setattr(api, name, call)

    fake("do_search", lambda q, *rest: {"query": q, "hits": []})
    fake("do_memories", {"memories": []})
    fake("do_remember", {"id": MEMORY})
    fake("do_forget", {"removed": 1})
    fake("do_retired", {"memories": []})
    fake("do_restore", {"restored": 1})
    fake("do_note", {"id": "note:abc", "chunks": 1})
    fake("do_sources", {"total": 0, "sources": []})
    fake("do_source", {"id": "note:abc", "chunks": ["text"]})
    fake("do_events", {"events": []})
    fake("do_event_counts", {"since": "24h", "total": 0, "kinds": {}})
    fake("do_profile", {"text": "", "written": None})
    fake("do_status", {"memories": 3})
    fake("do_open", lambda days: {"days": days, "total": 0, "notes": []})
    fake("do_open_done", lambda item, how, why: {"closed": {"id": item, "text": "x"}})
    fake("do_open_reopen", {"reopened": True})
    fake("do_open_closed", {"closed": []})
    return seen


EVERY_ROUTE = [
    ("GET", "/api/v1/me"), ("GET", "/api/v1/status"), ("GET", "/api/v1/search?q=x"),
    ("GET", "/api/v1/memories"), ("POST", "/api/v1/memories"), ("DELETE", f"/api/v1/memories/{MEMORY}"),
    ("GET", "/api/v1/retired"), ("POST", f"/api/v1/retired/{MEMORY}/restore"), ("POST", "/api/v1/notes"),
    ("GET", "/api/v1/sources"), ("GET", "/api/v1/source?id=note:abc"), ("GET", "/api/v1/events"),
    ("GET", "/api/v1/events/counts"), ("GET", "/api/v1/profile"),
    ("GET", "/api/v1/open"), ("GET", "/api/v1/open/closed"), ("POST", f"/api/v1/open/{OPEN}/done"),
    ("POST", f"/api/v1/open/{OPEN}/reopen"),
]


@pytest.mark.parametrize("method, path", EVERY_ROUTE)
def test_every_route_refuses_a_request_without_a_sign_in(http, calls, method, path):
    resp = http.request(method, path, json={"fact": "x", "text": "x"} if method == "POST" else None)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.headers["cache-control"] == "no-store"
    assert calls == []


@pytest.mark.parametrize("changes", [
    {"sub": "U-stranger"},                       # signed in, but not on the allow-list
    {"aud": "https://other.example/mcp"},        # a token issued for another server
    {"exp": int(time.time()) - online.CLOCK_SKEW_S - 5},
])
def test_a_token_for_someone_or_something_else_is_refused(http, calls, changes):
    assert http.get("/api/v1/search?q=x", headers=bearer(**changes)).status_code == 401
    assert calls == []


@pytest.mark.parametrize("method, path", EVERY_ROUTE)
def test_an_agent_key_never_reaches_the_api(calls, method, path):
    import hashlib
    key = "rk_" + "a" * 40
    settings = online.load_settings({"RECALL_ONLINE_URL": URL, "RECALL_ONLINE_ISSUER": ISSUER, "RECALL_ONLINE_SUBJECTS": ME,
                                     "RECALL_AGENT_KEYS": f"cloud:{hashlib.sha256(key.encode()).hexdigest()}"})
    app = online.build(settings, online.JwtVerifier(settings)).streamable_http_app(streamable_http_path=settings.path)
    with TestClient(app, base_url=settings.public_origin) as client:
        assert client.request(method, path, headers={"Authorization": f"Bearer {key}"}, json={}).status_code == 403
    assert calls == []


def test_a_wrong_scheme_or_an_empty_token_is_refused(http, calls):
    assert http.get("/api/v1/me", headers={"Authorization": f"Basic {token()}"}).status_code == 401
    assert http.get("/api/v1/me", headers={"Authorization": "Bearer "}).status_code == 401


@pytest.mark.parametrize("method, path, scope", [
    ("GET", "/api/v1/search?q=x", "recall:write recall:forget"),          # reading needs recall:read
    ("POST", "/api/v1/memories", "recall:read recall:forget"),            # saving needs recall:write
    ("POST", "/api/v1/notes", "recall:read"),
    ("POST", f"/api/v1/retired/{MEMORY}/restore", "recall:read"),
    ("DELETE", f"/api/v1/memories/{MEMORY}", "recall:read recall:write"),  # deleting needs recall:forget
    ("GET", "/api/v1/open", "recall:write"),
    ("POST", f"/api/v1/open/{OPEN}/done", "recall:read"),                 # closing an item is a write
    ("POST", f"/api/v1/open/{OPEN}/reopen", "recall:read"),
])
def test_each_route_needs_its_own_permission(http, calls, method, path, scope):
    resp = http.request(method, path, headers=bearer(scope=scope), json={"fact": "x", "text": "x"} if method == "POST" else None)
    assert resp.status_code == 403
    assert "permission" in resp.json()["error"]
    assert calls == []


def test_me_says_who_is_signed_in_and_needs_no_particular_permission(http):
    resp = http.get("/api/v1/me", headers=bearer(scope="recall:read offline_access"))
    assert resp.json() == {"user": ME, "scopes": ["recall:read"]}


def test_search_passes_the_question_and_its_options(http, calls):
    resp = http.get("/api/v1/search", params={"q": "  which   terminal ", "limit": "99", "tags": "memory, setup", "events": "1"},
                    headers=bearer())
    assert resp.status_code == 200 and resp.json() == {"query": "which terminal", "hits": []}
    assert calls == [("do_search", "which terminal", 20, ["memory", "setup"], True, False)]


@pytest.mark.parametrize("params, status", [
    ({}, 400), ({"q": "   "}, 400), ({"q": "x" * 1001}, 413), ({"q": "x", "limit": "many"}, 400),
    ({"q": "x", "tags": "ok,<script>"}, 400), ({"q": "x", "tags": ",".join(f"t{i}" for i in range(13))}, 400),
])
def test_search_refuses_what_it_cannot_use(http, calls, params, status):
    assert http.get("/api/v1/search", params=params, headers=bearer()).status_code == status
    assert calls == []


def test_saving_a_memory_and_a_note(http, calls):
    resp = http.post("/api/v1/memories", json={"fact": " Sam uses WezTerm. ", "tags": ["setup"]}, headers=bearer())
    assert (resp.status_code, resp.json()) == (201, {"id": MEMORY})
    resp = http.post("/api/v1/notes", json={"text": "First line\nmore"}, headers=bearer())
    assert (resp.status_code, resp.json()) == (201, {"id": "note:abc", "chunks": 1})
    assert calls == [("do_remember", "Sam uses WezTerm.", ["setup"]), ("do_note", "First line", "First line\nmore", [])]


@pytest.mark.parametrize("kwargs, status", [
    ({"json": {}}, 400), ({"json": {"fact": 7}}, 400), ({"json": {"fact": "x" * 2001}}, 413),
    ({"json": ["a fact"]}, 400), ({"content": "fact=x", "headers": {"Content-Type": "application/x-www-form-urlencoded"}}, 415),
    ({"content": "{not json", "headers": {"Content-Type": "application/json"}}, 400),
])
def test_a_memory_that_is_missing_too_long_or_not_json_is_refused(http, calls, kwargs, status):
    headers = bearer() | kwargs.pop("headers", {})
    assert http.post("/api/v1/memories", headers=headers, **kwargs).status_code == status
    assert calls == []


def test_forget_and_restore_take_only_memory_ids(http, calls):
    assert http.delete(f"/api/v1/memories/{MEMORY}", headers=bearer()).json() == {"removed": 1}
    assert http.post(f"/api/v1/retired/{MEMORY}/restore", headers=bearer()).json() == {"restored": 1}
    assert http.delete("/api/v1/memories/note:abc", headers=bearer()).status_code == 400
    assert http.delete("/api/v1/memories/memory:XYZ", headers=bearer()).status_code == 400
    assert calls == [("do_forget", MEMORY), ("do_restore", MEMORY)]


def test_events_take_a_window_and_a_stream(http, calls):
    assert http.get("/api/v1/events", params={"since": "7d", "source": "hermes", "limit": "5"}, headers=bearer()).status_code == 200
    assert http.get("/api/v1/events", params={"since": "last week"}, headers=bearer()).status_code == 400
    assert http.get("/api/v1/events", params={"source": "elsewhere"}, headers=bearer()).status_code == 400
    assert calls == [("do_events", "7d", "hermes", 5)]


def test_event_counts_take_a_window(http, calls):
    assert http.get("/api/v1/events/counts", params={"since": "7d"}, headers=bearer()).json()["total"] == 0
    assert http.get("/api/v1/events/counts", params={"since": "a while"}, headers=bearer()).status_code == 400
    assert calls == [("do_event_counts", "7d")]


def test_a_failure_inside_says_nothing_about_what_failed(http, monkeypatch):
    def broken():
        raise RuntimeError("mongodb+srv://user:secret@cluster refused")
    monkeypatch.setattr(api, "do_status", broken)
    resp = http.get("/api/v1/status", headers=bearer())
    assert resp.status_code == 500
    assert "secret" not in resp.text and "recall could not" in resp.json()["error"]


def test_the_data_layers_own_refusal_is_a_plain_400(http, monkeypatch):
    def refuse(_source):
        raise ValueError("forget only removes memory:* sources")
    monkeypatch.setattr(api, "do_forget", refuse)
    resp = http.delete(f"/api/v1/memories/{MEMORY}", headers=bearer())
    assert (resp.status_code, resp.json()) == (400, {"error": "forget only removes memory:* sources"})


@pytest.mark.parametrize("source, tags, kind", [
    ("memory:1", ["memory"], "memory"),
    ("memory:2", ["hermes", "learned", "memory"], "learned"),
    ("event:1", ["event", "hermes"], "chat"),
    ("event:2", ["event", "desktop-notification"], "notification"),
    ("event:3", ["event", "digest"], "digest"),
    ("session:abc", ["session-note", "claude"], "session"),
    ("note:abc", [], "note"),
    ("https://example.com/a", ["reading"], "page"),
    ("file:/home/x/notes.md", ["memory-note"], "file"),
])
def test_kind_names_what_a_chunk_is(source, tags, kind):
    assert api.kind_of(source, tags) == kind


def test_a_hit_is_an_excerpt_unless_the_whole_chunk_is_asked_for():
    long = "start " + "filler " * 300 + "the terminal is WezTerm " + "filler " * 300
    hit = search.Hit(source="note:abc", title="Setup", chunk_index=2, text=long, score=0.81234, tags=["setup"], created_at=WHEN)
    short = api.hit_json(hit, "terminal", full=False)
    assert short["id"] == "note:abc#2" and short["kind"] == "note" and short["score"] == 0.812
    assert short["at"] == "2026-11-18T09:30:00+00:00"
    assert "WezTerm" in short["text"] and len(short["text"]) < 720
    assert api.hit_json(hit, "terminal", full=True)["text"] == long


def test_a_memory_row_drops_the_memory_tag_and_keeps_why_it_was_retired():
    row = {"source": MEMORY, "text": "Sam uses Rio.", "tags": ["learned", "memory"], "created_at": WHEN,
           "retired": {"at": WHEN, "why": "he switched to WezTerm", "by": "memory:ffffffffffff", "session": "s1"}}
    out = api.memory_json(row)
    assert (out["id"], out["kind"], out["tags"]) == (MEMORY, "learned", ["learned"])
    assert out["retired"] == {"at": "2026-11-18T09:30:00+00:00", "why": "he switched to WezTerm", "by": "memory:ffffffffffff"}
    assert "retired" not in api.memory_json({"source": MEMORY, "text": "x", "tags": ["memory"]})


class FakeChunks:
    def __init__(self, rows):
        self.rows, self.query = rows, None

    def find(self, query, _fields):
        self.query = query
        return self

    def sort(self, *_):
        return self

    def limit(self, _n):
        return self.rows


def test_reading_one_source_never_asks_for_vault_chunks(monkeypatch):
    chunks = FakeChunks([{"title": "Setup", "text": "one", "tags": [], "created_at": WHEN, "chunk_index": 0},
                         {"title": "Setup", "text": "two", "tags": [], "created_at": WHEN, "chunk_index": 1}])
    monkeypatch.setattr(api.mcp_server, "_settings", lambda: None)
    monkeypatch.setattr(api.db, "chunks", lambda _settings: chunks)
    out = api.do_source("note:abc")
    assert chunks.query == {"source": "note:abc", "tags": {"$ne": "vault"}}
    assert (out["title"], out["kind"], out["chunks"]) == ("Setup", "note", ["one", "two"])
    chunks.rows = []
    with pytest.raises(api.Refused) as exc:
        api.do_source("vault:bank")
    assert exc.value.status == 404


def test_open_items_are_listed_closed_and_reopened_by_id(http, calls):
    assert http.get("/api/v1/open", headers=bearer()).json() == {"days": 14, "total": 0, "notes": []}
    assert http.get("/api/v1/open", params={"days": "999"}, headers=bearer()).json()["days"] == 90
    assert http.get("/api/v1/open/closed", params={"limit": "5"}, headers=bearer()).json() == {"closed": []}

    assert http.post(f"/api/v1/open/{OPEN}/done", headers=bearer()).json() == {"closed": {"id": OPEN, "text": "x"}}      # no body: done
    body = {"how": "dropped", "why": " not needed any more "}
    assert http.post(f"/api/v1/open/{OPEN}/done", headers=bearer(), json=body).status_code == 200
    assert http.post(f"/api/v1/open/{OPEN}/reopen", headers=bearer()).json() == {"reopened": True}
    assert calls == [("do_open", 14), ("do_open", 90), ("do_open_closed", 5), ("do_open_done", OPEN, "done", ""),
                     ("do_open_done", OPEN, "dropped", "not needed any more"), ("do_open_reopen", OPEN)]

    for path, kwargs in ((f"/api/v1/open/{MEMORY}/done", {}), ("/api/v1/open/open:XYZ/reopen", {}),
                         (f"/api/v1/open/{OPEN}/done", {"json": {"how": "finished"}}),
                         (f"/api/v1/open/{OPEN}/done", {"json": {"why": "x" * 301}})):
        assert http.post(path, headers=bearer(), **kwargs).status_code in (400, 413), path
    assert len(calls) == 6


def test_closing_an_item_no_note_lists_is_a_404(http, monkeypatch):
    def gone(settings, item, how, why, by):
        raise LookupError(f"no session note of the last 90 days lists {item}")

    monkeypatch.setattr(api.mcp_server, "_settings", lambda: None)
    monkeypatch.setattr(api.openitems, "close_by_hand", gone)
    resp = http.post(f"/api/v1/open/{OPEN}/done", headers=bearer())
    assert resp.status_code == 404 and OPEN in resp.json()["error"]


def test_open_items_come_back_as_ids_with_their_text(monkeypatch):
    from recall import serve

    notes = [{"source": "session:a", "title": "Older work", "date": "2026-11-08T12:00:00+00:00", "folder": "~/x", "kind": "claude",
              "items": ["Sign in.", "Nothing is pushed."], "ids": ["open:ba9876543210", OPEN], "kinds": ["step", "check"]}]
    monkeypatch.setattr(api.mcp_server, "_settings", lambda: None)
    asked = []
    monkeypatch.setattr(serve, "open_items", lambda settings, req: asked.append(req) or {"ok": True, "days": req["days"], "notes": notes})
    out = api.do_open(30)
    assert (out["days"], out["total"], out["kinds"]) == (30, 2, {"step": 1, "check": 1})
    assert asked[0]["all"] is True                       # the page shows everything; it filters by kind itself
    assert asked[0]["limit"] == serve.OPEN_LIMIT[1] >= 1000   # and every session of the window, not the first hundred
    assert out["notes"][0] == {"source": "session:a", "title": "Older work", "at": "2026-11-08T12:00:00+00:00", "folder": "~/x",
                               "kind": "claude", "items": [{"id": "open:ba9876543210", "text": "Sign in.", "kind": "step"},
                                                           {"id": OPEN, "text": "Nothing is pushed.", "kind": "check"}]}
