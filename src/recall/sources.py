"""Turn a path, URL, or raw string into (source_id, title, text)."""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import trafilatura
from pypdf import PdfReader


MAX_FETCH_BYTES = 30_000_000   # of one page or PDF; more than that is not something to read whole


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _fetch(url: str) -> tuple[bytes, str, str]:
    """(body, content type, text encoding) of a URL. Stops reading at MAX_FETCH_BYTES."""
    data = bytearray()
    with httpx.stream("GET", url, follow_redirects=True, timeout=30, headers={
        "User-Agent": "recall/0.1 (personal knowledge base)"
    }) as resp:
        resp.raise_for_status()
        for part in resp.iter_bytes():
            data += part
            if len(data) > MAX_FETCH_BYTES:
                raise SystemExit(f"{url} is larger than {MAX_FETCH_BYTES // 1_000_000} MB; download it and add the part you need")
        return bytes(data), resp.headers.get("content-type", ""), resp.encoding or "utf-8"


def _pdf_text(reader: PdfReader) -> str:
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def from_response(url: str, data: bytes, content_type: str, encoding: str, title: str | None = None) -> tuple[str, str, str]:
    """(source, title, text) for what a URL returned: a PDF is read as a PDF, anything else as a page."""
    if "application/pdf" in content_type.lower() or data.startswith(b"%PDF-"):
        try:
            reader = PdfReader(io.BytesIO(data))
            text = _pdf_text(reader)
            named = (reader.metadata.title or "").strip() if reader.metadata else ""
        except Exception as exc:    # pypdf raises many kinds of error for a damaged or locked file
            raise SystemExit(f"could not read the PDF at {url}: {type(exc).__name__}") from None
        return url, title or named or unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or url, text
    html = data.decode(encoding, errors="replace")
    extracted = trafilatura.extract(html, include_comments=False, include_tables=True, url=url)
    meta = trafilatura.extract_metadata(html, default_url=url)
    page_title = (meta.title if meta and meta.title else None) or url
    return url, title or page_title, extracted or ""


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
        data, content_type, encoding = _fetch(target)
        return from_response(target, data, content_type, encoding, title)

    path = Path(target).expanduser()
    if not path.is_file():
        raise SystemExit(f"not a file or URL: {target}")

    if path.suffix.lower() == ".pdf":
        text = _pdf_text(PdfReader(str(path)))
    else:
        text = path.read_text(errors="replace")

    return f"file:{path.resolve()}", title or path.name, text
