from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from mcp.server.mcpserver.exceptions import ToolError

from recall import mcp_server, online, vault

KEY = Fernet.generate_key().decode()


class Coll:
    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(doc)

    def find_one(self, query, projection=None):
        return next((d for d in self.docs if d["source"] == query["source"]), None)

    def find(self, query, projection=None):
        rows = [d for d in self.docs if "vault" in d["tags"]]
        return type("Cur", (), {"sort": lambda self, *a: sorted(rows, key=lambda d: d["created_at"], reverse=True)})()


@pytest.fixture
def box(monkeypatch, tmp_path):
    coll = Coll()
    monkeypatch.setattr(vault.db, "chunks", lambda s: coll)
    monkeypatch.setattr(vault.embed, "embed_documents", lambda s, texts: [[0.0] for _ in texts])
    monkeypatch.setattr(vault, "find", lambda s, q, limit=3: [d for d in coll.docs if q.lower() in d["title"].lower()][:limit])
    monkeypatch.setattr(mcp_server, "_settings", lambda: type("S", (), {"voyage_model": "voyage-4"})())
    monkeypatch.setattr(vault, "KEY_PATH", tmp_path / "no-such.key")
    monkeypatch.setenv("RECALL_VAULT_SECRET", KEY)
    return coll


def test_the_online_owner_can_save_list_and_open_an_entry(box):
    out = mcp_server.vault_save("Passport number", "X1234567, expires 2031-04-02", note="travel")
    source = out.split()[1]
    doc = box.docs[0]
    assert b"X1234567" not in bytes(doc["vault"]["blob"]) and "X1234567" not in doc["text"]      # stored encrypted
    assert doc["text"] == "Passport number — travel" and "vault" in doc["tags"]
    assert "Passport number" in mcp_server.vault_list() and source in mcp_server.vault_list()
    shown = mcp_server.vault_get("passport")
    assert shown.endswith("X1234567, expires 2031-04-02") and source in shown
    assert mcp_server.vault_get(source) == shown
    with pytest.raises(ToolError):
        mcp_server.vault_get("vault:000000000000")


def test_no_key_means_no_vault_and_no_new_key(box, monkeypatch, tmp_path):
    monkeypatch.delenv("RECALL_VAULT_SECRET")
    with pytest.raises(ToolError, match="not set up"):
        mcp_server.vault_save("Bank PIN", "0000")
    assert not (tmp_path / "no-such.key").exists() and box.docs == []
    assert mcp_server.vault_list() == "the vault is empty"


def test_a_different_key_cannot_open_an_entry(box, monkeypatch):
    mcp_server.vault_save("Wifi password", "hunter2hunter2")
    monkeypatch.setenv("RECALL_VAULT_SECRET", Fernet.generate_key().decode())
    with pytest.raises(ToolError, match="different vault.key"):
        mcp_server.vault_get("wifi")


def test_vault_tools_are_online_only():
    online_only = {fn.__name__ for fn, scope, _ in mcp_server.TOOLS if fn.__name__.startswith("vault_")}
    assert online_only == {"vault_list", "vault_get", "vault_save"}          # the local tool list is checked in test_mcp_server
    assert "vault_get" in mcp_server.ONLINE_INSTRUCTIONS and "vault_get" not in mcp_server.INSTRUCTIONS
