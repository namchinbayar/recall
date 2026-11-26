"""Vault: documents and secrets encrypted on this machine before they reach Atlas.

What leaves the machine: the title and note (embedded for search) and the
ciphertext. The key lives in ~/.config/recall/vault.key. Vault entries carry the
tag "vault"; search and the agent exclude that tag.

The online server (recall.example.com) has no key file. It is given the same key
as RECALL_VAULT_SECRET, a Worker secret, so the signed-in owner can read and add
vault entries there (mcp_server.py, the vault_* tools). It never makes a key of
its own: an entry encrypted with a key that exists only inside a container would
be lost with it.
"""

from __future__ import annotations

import mimetypes
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bson.binary import Binary
from cryptography.fernet import Fernet, InvalidToken

from . import db, embed
from .config import Settings

TAG = "vault"
KEY_PATH = Path(os.environ.get("RECALL_VAULT_KEY", Path.home() / ".config" / "recall" / "vault.key"))
MAX_BYTES = 12 * 1024 * 1024  # leave headroom under Mongo's 16 MB document cap


class NoKey(Exception):
    """The vault key is not available here."""


def available() -> bool:
    return bool(os.environ.get("RECALL_VAULT_SECRET", "").strip()) or KEY_PATH.is_file()


def _fernet(create: bool = True) -> Fernet:
    if secret := os.environ.get("RECALL_VAULT_SECRET", "").strip():
        return Fernet(secret.encode())
    if not KEY_PATH.is_file():
        if not create:
            raise NoKey("the vault key is not set up here")
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        KEY_PATH.touch(mode=0o600)
        KEY_PATH.write_bytes(Fernet.generate_key())
        KEY_PATH.chmod(0o600)
    return Fernet(KEY_PATH.read_bytes().strip())


def _read_target(target: str) -> tuple[bytes, str | None, str]:
    """Returns (raw bytes, filename, content type)."""
    if target == "-":
        data = sys.stdin.buffer.read()
        return data, None, "text/plain"
    path = Path(target).expanduser()
    if not path.is_file():
        raise SystemExit(f"not a file: {target}")
    data = path.read_bytes()
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return data, path.name, ctype


def _extract_text(data: bytes, ctype: str, filename: str | None) -> str:
    if ctype == "application/pdf" or (filename or "").lower().endswith(".pdf"):
        import io
        from pypdf import PdfReader
        try:
            return "\n\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages).strip()
        except Exception:
            return ""
    if ctype.startswith("text/") or ctype in ("application/json", "application/toml", "application/x-yaml"):
        return data.decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def add(
    settings: Settings,
    target: str,
    title: str,
    note: str | None = None,
    tags: list[str] | None = None,
    embed_content: bool = False,
) -> str:
    data, filename, ctype = _read_target(target)
    return store(settings, data, filename, ctype, title, note, tags, embed_content)


def store(
    settings: Settings, data: bytes, filename: str | None, ctype: str, title: str,
    note: str | None = None, tags: list[str] | None = None, embed_content: bool = False, create_key: bool = True,
) -> str:
    """Encrypt and store bytes as one vault entry. Returns its source id."""
    if len(data) > MAX_BYTES:
        raise SystemExit(f"file is {len(data)/1e6:.1f} MB; vault entries must be under {MAX_BYTES/1e6:.0f} MB")
    text = _extract_text(data, ctype, filename)
    f = _fernet(create_key)

    searchable = title + (f"\n{note}" if note else "")
    if embed_content and text:
        searchable += "\n" + text[:6000]  # content leaves the machine only when asked
    vector = embed.embed_documents(settings, [searchable])[0]

    source = f"vault:{uuid.uuid4().hex[:12]}"
    db.chunks(settings).insert_one({
        "source": source,
        "title": title,
        "chunk_index": 0,
        "text": title + (f" — {note}" if note else ""),  # plaintext shown in listings; never the content
        "embedding": vector,
        "tags": sorted({TAG, *(tags or [])}),
        "model": settings.voyage_model,
        "created_at": datetime.now(timezone.utc),
        "vault": {
            "filename": filename,
            "content_type": ctype,
            "size": len(data),
            "blob": Binary(f.encrypt(data)),
            "text_enc": f.encrypt(text.encode()) if text else None,
            "content_embedded": bool(embed_content and text),
        },
    })
    return source


def find(settings: Settings, query: str, limit: int = 3) -> list[dict]:
    vector = embed.embed_query(settings, query)
    pipeline = [
        {"$vectorSearch": {
            "index": settings.index_name, "path": "embedding", "queryVector": vector,
            "numCandidates": 100, "limit": limit, "filter": {"tags": {"$eq": TAG}},
        }},
        {"$project": {"_id": 0, "embedding": 0}},
        {"$addFields": {"score": {"$meta": "vectorSearchScore"}}},
    ]
    return list(db.chunks(settings).aggregate(pipeline))


def get_by_source(settings: Settings, source: str) -> dict | None:
    return db.chunks(settings).find_one({"source": source, "tags": TAG}, {"_id": 0, "embedding": 0})


def decrypt(entry: dict) -> tuple[bytes, str]:
    """Returns (raw bytes, extracted text or '')."""
    f = _fernet(create=False)
    v = entry["vault"]
    try:
        data = f.decrypt(bytes(v["blob"]))
        text = f.decrypt(bytes(v["text_enc"])).decode() if v.get("text_enc") else ""
    except InvalidToken:
        raise SystemExit("cannot decrypt: this entry was encrypted with a different vault.key")
    return data, text


def list_entries(settings: Settings) -> list[dict]:
    cur = db.chunks(settings).find(
        {"tags": TAG}, {"_id": 0, "source": 1, "title": 1, "tags": 1, "created_at": 1, "vault.size": 1, "vault.filename": 1}
    ).sort("created_at", -1)
    return list(cur)


def remove(settings: Settings, source: str) -> int:
    return db.chunks(settings).delete_many({"source": source, "tags": TAG}).deleted_count
