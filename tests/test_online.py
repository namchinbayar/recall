import hashlib
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from recall import mcp_server, online

URL = "https://recall.example/mcp"
ISSUER = "https://auth.example/v1/apps/P123"
ME = "U-me"
ENV = {"RECALL_ONLINE_URL": URL, "RECALL_ONLINE_ISSUER": ISSUER + "/", "RECALL_ONLINE_SUBJECTS": f"{ME}, U-second"}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings():
    return online.load_settings(ENV)


def token(key=KEY, **changes):
    claims = {
        "iss": ISSUER, "aud": URL, "sub": ME, "exp": int(time.time()) + 600,
        "scope": " ".join(online.SCOPES), "azp": "client-1",
    } | changes
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


def verifier(settings):
    return online.JwtVerifier(settings, key_for=lambda _token: KEY.public_key())


def test_settings_are_read_and_the_issuer_loses_its_trailing_slash(settings):
    assert settings.issuer == ISSUER
    assert settings.subjects == {ME, "U-second"}
    assert (settings.path, settings.public_host, settings.public_origin) == ("/mcp", "recall.example", "https://recall.example")
    assert (settings.host, settings.port) == ("127.0.0.1", 8791)
    assert settings.extra_hosts == ()
    behind_proxy = online.load_settings(ENV | {"RECALL_ONLINE_HOSTS": "container, other.internal"})
    assert behind_proxy.extra_hosts == ("container", "other.internal")


@pytest.mark.parametrize("env, word", [
    ({}, "RECALL_ONLINE_URL"),
    (ENV | {"RECALL_ONLINE_SUBJECTS": " , "}, "RECALL_ONLINE_SUBJECTS"),
    (ENV | {"RECALL_ONLINE_URL": "http://recall.example/mcp"}, "https"),
    (ENV | {"RECALL_ONLINE_URL": "https://recall.example/mcp?x=1"}, "plain address"),
])
def test_settings_refuse_what_would_be_unsafe_or_incomplete(env, word):
    with pytest.raises(SystemExit) as exc:
        online.load_settings(env)
    assert word in str(exc.value)


def test_scopes_are_read_from_any_of_the_usual_claims():
    assert online.scopes_of({"scope": "a b"}) == ["a", "b"]
    assert online.scopes_of({"scp": ["a"], "scopes": ["a", "c"]}) == ["a", "c"]
    assert online.scopes_of({"scope": 7}) == []


def test_a_good_token_is_accepted_with_its_user_and_permissions(settings):
    access = verifier(settings).check(token())
    assert (access.subject, access.client_id, access.resource) == (ME, "client-1", URL)
    assert access.scopes == online.SCOPES


@pytest.mark.parametrize("changes", [
    {"sub": "U-stranger"},                       # signed in, but not on the allow-list
    {"sub": None},
    {"aud": "https://other.example/mcp"},        # issued for another server
    {"iss": "https://auth.example/v1/apps/P999"},
    {"exp": int(time.time()) - online.CLOCK_SKEW_S - 5},
    {"exp": None},
    {"iat": int(time.time()) + online.CLOCK_SKEW_S + 60},
])
def test_a_token_for_someone_or_something_else_is_refused(settings, changes):
    assert verifier(settings).check(token(**changes)) is None


def test_a_token_issued_by_a_clock_a_little_ahead_is_accepted(settings):
    # Descope's clock runs slightly ahead; the app's sign-in checks the token the moment it is issued.
    assert verifier(settings).check(token(iat=int(time.time()) + 3)).subject == ME


def test_a_token_signed_by_another_key_or_not_at_all_is_refused(settings):
    assert verifier(settings).check(token(key=OTHER_KEY)) is None
    unsigned = jwt.encode({"iss": ISSUER, "aud": URL, "sub": ME, "exp": int(time.time()) + 600}, None, algorithm="none")
    assert verifier(settings).check(unsigned) is None
    assert verifier(settings).check("not-a-token") is None


def test_a_failing_key_lookup_refuses_instead_of_crashing(settings):
    def broken(_token):
        raise RuntimeError("keys unreachable")
    assert online.JwtVerifier(settings, key_for=broken).check(token()) is None


@pytest.mark.anyio
async def test_online_offers_every_tool_except_the_local_only_one(settings):
    tools = {t.name for t in await online.build(settings, verifier(settings)).list_tools()}
    assert tools == {
        "profile", "catch_up", "search", "remember", "forget", "list_memories", "list_sources",
        "save_note", "recent_events", "metrics_summary", "metrics_manual", "record_metric",
        "open_items", "close_open_item", "reopen_open_item",
        "vault_list", "vault_get", "vault_save",
    }
    assert "save_url_or_file" in {fn.__name__ for fn, scope, _ in mcp_server.TOOLS if scope is None}


def test_a_tool_is_refused_without_its_permission(monkeypatch):
    calls = []
    forget = online.guarded(lambda source: calls.append(source) or "gone", mcp_server.FORGET_SCOPE)

    def signed_in_with(*scopes):
        access = AccessToken(token="t", client_id="c", scopes=list(scopes))
        monkeypatch.setattr(online, "get_access_token", lambda: access)

    monkeypatch.setattr(online, "get_access_token", lambda: None)
    with pytest.raises(ToolError):
        forget("memory:1")
    signed_in_with(mcp_server.READ_SCOPE, mcp_server.WRITE_SCOPE)
    with pytest.raises(ToolError, match="recall:forget"):
        forget("memory:1")
    assert calls == []
    signed_in_with(*online.SCOPES)
    assert forget("memory:1") == "gone" and calls == ["memory:1"]


@pytest.fixture
def http(settings):
    app = online.build(settings, verifier(settings)).streamable_http_app(
        streamable_http_path=settings.path,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[settings.public_host], allowed_origins=[settings.public_origin]),
    )
    with TestClient(app, base_url=settings.public_origin) as client:
        yield client


def test_without_a_token_the_server_points_to_the_sign_in(http):
    resp = http.post("/mcp", json=INITIALIZE, headers=HEADERS)
    assert resp.status_code == 401
    assert "/.well-known/oauth-protected-resource/mcp" in resp.headers["www-authenticate"]
    meta = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == URL
    assert meta["authorization_servers"] == [ISSUER]
    assert meta["scopes_supported"] == online.SCOPES


def test_with_a_good_token_the_server_answers(http):
    resp = http.post("/mcp", json=INITIALIZE, headers=HEADERS | {"Authorization": f"Bearer {token()}"})
    assert resp.status_code == 200
    assert "recall" in resp.text


@pytest.mark.parametrize("changes, status", [
    ({"sub": "U-stranger"}, 401),
    ({"aud": "https://other.example/mcp"}, 401),
    ({"scope": "recall:read"}, 403),             # signed in, but without every permission
])
def test_a_wrong_token_gets_no_further_than_the_door(http, changes, status):
    resp = http.post("/mcp", json=INITIALIZE, headers=HEADERS | {"Authorization": f"Bearer {token(**changes)}"})
    assert resp.status_code == status


def test_health_says_only_up_or_down_and_needs_no_sign_in(http, monkeypatch):
    monkeypatch.setattr(online, "database_reachable", lambda: True)
    resp = http.get("/healthz")
    assert (resp.status_code, resp.json()) == (200, {"ok": True})
    monkeypatch.setattr(online, "database_reachable", lambda: False)
    resp = http.get("/healthz")
    assert (resp.status_code, resp.json()) == (503, {"ok": False})


def test_a_missing_setting_counts_as_database_down(monkeypatch):
    def unconfigured():
        raise SystemExit("MONGODB_URI is not set")
    monkeypatch.setattr(online.mcp_server, "_settings", unconfigured)
    assert online.database_reachable() is False


def test_a_request_for_another_host_name_is_refused(settings, http):
    resp = http.post("/mcp", json=INITIALIZE, headers=HEADERS | {
        "Authorization": f"Bearer {token()}", "Host": "evil.example"})
    assert resp.status_code in (400, 421)


# ----- agent keys and /context ---------------------------------------------

AGENT_KEY = "rk_" + "a" * 40
AGENT_ENV = ENV | {"RECALL_AGENT_KEYS": f"cloud:{hashlib.sha256(AGENT_KEY.encode()).hexdigest()}"}


def test_agent_keys_are_parsed_and_a_bad_entry_stops_the_server():
    assert online.load_settings(AGENT_ENV).agent_keys == (("cloud", hashlib.sha256(AGENT_KEY.encode()).hexdigest()),)
    assert online.load_settings(ENV).agent_keys == ()
    with pytest.raises(SystemExit, match="RECALL_AGENT_KEYS"):
        online.load_settings(ENV | {"RECALL_AGENT_KEYS": "cloud:not-a-hash"})


def test_only_the_known_agent_key_gets_in_and_never_as_the_owner():
    s = online.load_settings(AGENT_ENV)
    access = verifier(s).check(AGENT_KEY)
    assert access.subject == "agent:cloud" and online.is_agent(access)
    assert verifier(s).check("rk_" + "b" * 40) is None
    assert verifier(online.load_settings(ENV)).check(AGENT_KEY) is None     # no keys configured: no agents
    assert not online.is_agent(verifier(s).check(token()))                  # a real sign-in still works


def test_an_agent_key_cannot_forget_or_open_the_vault(monkeypatch):
    access = online.agent_for(AGENT_KEY, online.load_settings(AGENT_ENV))
    monkeypatch.setattr(online, "get_access_token", lambda: access)
    by_name = {fn.__name__: (fn, scope) for fn, scope, _ in mcp_server.TOOLS if scope}
    for name in ("forget", "vault_list", "vault_get", "vault_save"):
        fn, scope = by_name[name]
        assert online.owner_only(fn, scope)
        with pytest.raises(ToolError, match="owner"):
            online.guarded(fn, scope)()
    for name in ("search", "remember", "save_note", "recent_events"):
        assert not online.owner_only(*by_name[name])


@pytest.fixture
def agent_http():
    s = online.load_settings(AGENT_ENV)
    app = online.build(s, verifier(s)).streamable_http_app(
        streamable_http_path=s.path,
        transport_security=TransportSecuritySettings(allowed_hosts=[s.public_host], allowed_origins=[s.public_origin]),
    )
    with TestClient(app, base_url=s.public_origin) as client:
        yield client


def test_serve_needs_a_key_and_passes_only_the_allowed_ops(agent_http, monkeypatch):
    seen = []
    monkeypatch.setattr(online.mcp_server, "_settings", lambda: "settings")
    monkeypatch.setattr(online.serve, "answer", lambda settings, line: seen.append((settings, json.loads(line))) or {"ok": True})
    key = {"Authorization": f"Bearer {AGENT_KEY}"}
    body = {"op": "context", "query": "which terminal", "session_id": "s1"}
    assert agent_http.post("/serve", json=body).status_code == 401
    assert agent_http.post("/serve", json=body, headers={"Authorization": "Bearer rk_" + "b" * 40}).status_code == 401
    resp = agent_http.post("/serve", json=body, headers=key)
    assert resp.status_code == 200 and resp.json() == {"ok": True} and seen == [("settings", body)]
    for op in ("store", "open_items", "drop", None):
        assert agent_http.post("/serve", json={"op": op}, headers=key).status_code == 400
    assert agent_http.post("/serve", content=b"[1]", headers=key).status_code == 400
    assert agent_http.post("/serve", json={"op": "turn", "message": "m"}, headers=key).status_code == 200
    assert agent_http.post("/serve", json=body, headers={"Authorization": f"Bearer {token()}"}).status_code == 200
