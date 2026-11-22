import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from recall import sources


def pdf(text: str, title: str | None = None) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(300, 200)
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode())
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Contents")] = writer._add_object(content)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    if title:
        writer.add_metadata({"/Title": title})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_a_pdf_url_is_read_as_a_pdf():
    source, title, text = sources.from_response(
        "https://x.test/papers/attention.pdf", pdf("hello from a pdf", "A paper"), "application/pdf", "utf-8")
    assert source == "https://x.test/papers/attention.pdf"
    assert title == "A paper"
    assert "hello from a pdf" in text


def test_a_pdf_is_recognized_by_its_first_bytes_and_named_after_its_file():
    # Some servers send PDFs as application/octet-stream.
    _, title, text = sources.from_response(
        "https://x.test/files/my%20notes.pdf?download=1", pdf("plain words"), "application/octet-stream", "utf-8")
    assert title == "my notes.pdf"
    assert "plain words" in text
    assert sources.from_response("https://x.test/a.pdf", pdf("x"), "", "utf-8", title="Mine")[1] == "Mine"


def test_a_damaged_pdf_says_so():
    with pytest.raises(SystemExit, match="could not read the PDF"):
        sources.from_response("https://x.test/a.pdf", b"%PDF-1.7 not really", "application/pdf", "utf-8")


def test_a_page_is_still_read_as_a_page():
    html = ("<html><head><title>Reloading niri</title></head><body><article><h1>Reloading niri</h1>"
            + "<p>The compositor reloads its config when the file changes on disk.</p>" * 5 + "</article></body></html>")
    source, title, text = sources.from_response("https://x.test/niri", html.encode(), "text/html; charset=utf-8", "utf-8")
    assert (source, title) == ("https://x.test/niri", "Reloading niri")
    assert "reloads its config" in text


def test_load_fetches_a_url(monkeypatch):
    monkeypatch.setattr(sources, "_fetch", lambda url: (pdf("fetched words"), "application/pdf", "utf-8"))
    assert "fetched words" in sources.load("https://x.test/a.pdf")[2]


@pytest.fixture
def site():
    """A web server on this machine: /paper.pdf and /big."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = pdf("served over http", "Served")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data, kind = (body, "application/pdf") if self.path == "/paper.pdf" else (b"x" * 50_000, "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_fetch_reads_a_url_and_stops_at_the_size_limit(site, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    source, title, text = sources.load(f"{site}/paper.pdf")
    assert (source, title) == (f"{site}/paper.pdf", "Served") and "served over http" in text
    monkeypatch.setattr(sources, "MAX_FETCH_BYTES", 10_000)
    with pytest.raises(SystemExit, match="is larger than"):
        sources.load(f"{site}/big")
