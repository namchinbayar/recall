"""What an assistant or the app saves directly is checked for secrets, as synced files are."""

import pytest
from starlette.testclient import TestClient

from recall import db, embed, ingest, mcp_server, online
from recall.agent import memory, tools

KEY = "sk-" + "a1B2" * 8
FACT = f"The deploy key for the staging box is {KEY}"


@pytest.fixture
def untouched(monkeypatch):
    """Fails the test if anything is embedded or written."""
    def never(*args, **kwargs):
        raise AssertionError("a refused save must not reach Voyage or the database")

    monkeypatch.setattr(embed, "embed_documents", never)
    monkeypatch.setattr(db, "chunks", never)


def test_a_fact_holding_a_secret_is_not_remembered(untouched):
    with pytest.raises(ValueError, match=r"looks like it holds a secret \(API key, line 1\)"):
        memory.remember(None, FACT)
    content, is_error = tools.run(None, "remember", {"fact": FACT})
    assert is_error and "the vault is the place for it" in content
    assert KEY not in content


def test_a_note_holding_a_secret_is_not_saved(untouched, monkeypatch):
    monkeypatch.setattr(mcp_server, "_settings", lambda: None)
    with pytest.raises(ValueError, match="line 2"):
        ingest.ingest_text(None, f"how to deploy\npassword = {'Zx9' * 10}", "Deploy notes")
    with pytest.raises(ValueError, match="AWS key"):
        ingest.ingest_text(None, "see the title", "AKIAABCDEFGHIJKLMNOP")
    with pytest.raises(mcp_server.ToolError, match="looks like it holds a secret"):
        mcp_server.save_note(FACT, "Staging")


def test_an_ordinary_fact_is_stored(monkeypatch):
    stored = []
    monkeypatch.setattr(embed, "embed_documents", lambda settings, texts: [[0.0] for _ in texts])
    monkeypatch.setattr(db, "chunks", lambda settings: type("C", (), {"insert_one": staticmethod(stored.append)}))
    settings = type("S", (), {"voyage_model": "voyage-4"})
    source = memory.remember(settings, "The owner runs niri on CachyOS and prefers Chrome Beta.")
    assert source.startswith("memory:") and stored[0]["text"].startswith("The owner runs niri")


def test_the_app_is_told_why(untouched, monkeypatch):
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    url, issuer = "https://recall.example/mcp", "https://auth.example/v1/apps/P123"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = online.load_settings({"RECALL_ONLINE_URL": url, "RECALL_ONLINE_ISSUER": issuer, "RECALL_ONLINE_SUBJECTS": "U-me"})
    verifier = online.JwtVerifier(settings, key_for=lambda _token: key.public_key())
    monkeypatch.setattr(mcp_server, "_settings", lambda: None)
    token = jwt.encode({"iss": issuer, "aud": url, "sub": "U-me", "exp": int(time.time()) + 600,
                        "scope": " ".join(online.SCOPES)}, key, algorithm="RS256")
    app = online.build(settings, verifier).streamable_http_app(streamable_http_path=settings.path)
    with TestClient(app, base_url=settings.public_origin) as client:
        for path, body in (("/api/v1/memories", {"fact": FACT}), ("/api/v1/notes", {"text": FACT})):
            answer = client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
            assert answer.status_code == 400, path
            assert "looks like it holds a secret" in answer.json()["error"]
            assert KEY not in answer.text
