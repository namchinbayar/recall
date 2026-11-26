"""Turn a path, URL, or raw string into (source_id, title, text)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import httpx
import trafilatura
from pypdf import PdfReader


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def load(target: str, title: str | None = None) -> tuple[str, str, str]:
    """Return (source, title, text).

    target may be a URL, a file path, or '-' for stdin.
    source is the stable identifier used for dedupe and re-ingest.
    """
    if target == "-":
        text = sys.stdin.read()
        digest = hashlib.sha256(text.encode()).hexdigest()[:12]
        return f"stdin:{digest}", title or f"stdin {digest}", text

    if _is_url(target):
        resp = httpx.get(target, follow_redirects=True, timeout=30, headers={
            "User-Agent": "recall/0.1 (personal knowledge base)"
        })
        resp.raise_for_status()
        extracted = trafilatura.extract(
            resp.text, include_comments=False, include_tables=True, url=target
        )
        meta = trafilatura.extract_metadata(resp.text, default_url=target)
        page_title = (meta.title if meta and meta.title else None) or target
        return target, title or page_title, extracted or ""

    path = Path(target).expanduser()
    if not path.is_file():
        raise SystemExit(f"not a file or URL: {target}")

    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    else:
        text = path.read_text(errors="replace")

    return f"file:{path.resolve()}", title or path.name, text
