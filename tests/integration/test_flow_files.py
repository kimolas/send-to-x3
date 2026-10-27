"""File shares through the real extractors/renderers: PDF, EPUB passthrough, HTML, TXT.

The PDF is generated here (synthetic text, CC0). Skipped until the modules exist.
"""

from __future__ import annotations

import io
import zipfile

import aiohttp
import pytest
from lxml import etree

for _mod in ("sendtox3.extract.fetch", "sendtox3.extract.html", "sendtox3.extract.text",
             "sendtox3.render.names", "sendtox3.render.txt", "sendtox3.render.epub"):
    pytest.importorskip(_mod)

from sendtox3 import delivery  # noqa: E402
from sendtox3.ingest import Processor  # noqa: E402
from sendtox3.render.epub import render_epub  # noqa: E402
from sendtox3.textmodel import Document, Heading, Paragraph  # noqa: E402
from sendtox3.web.app import create_app  # noqa: E402

INGEST = {"Authorization": "Bearer ingest-test-token"}


def make_pdf(lines: list[str], title: str = "") -> bytes:
    """A small valid single-page PDF with real text (Helvetica, one line per entry)."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = "BT /F1 12 Tf 72 740 Td 16 TL " + " ".join(f"({esc(l)}) '" for l in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
        "/Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
    ]
    if title:
        objs.append(f"<< /Title ({esc(title)}) >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    info = f" /Info {len(objs)} 0 R" if title else ""
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R{info} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@pytest.fixture
async def client(aiohttp_client, opts, store):
    return await aiohttp_client(create_app(opts, store, Processor(store, opts)))


async def _upload(client, data: bytes, filename: str, ctype: str, **fields):
    form = aiohttp.FormData()
    for k, v in fields.items():
        form.add_field(k, v)
    form.add_field("file", data, filename=filename, content_type=ctype)
    resp = await client.post("/api/v1/ingest", data=form, headers=INGEST)
    body = await resp.json()
    assert resp.status == 200, body
    return body


async def test_pdf_upload(client, store):
    pytest.importorskip("sendtox3.extract.pdf")
    lines = ["Quarterly reading notes", "The first paragraph of the report talks about paper.",
             "It continues on a second line of text.", "", "A second paragraph closes the report."]
    data = await _upload(client, make_pdf(lines, title="Reading Report"), "report.pdf", "application/pdf")
    item = store.get(data["id"])
    assert item.kind == "pdf" and item.status == "ready" and item.word_count >= 15
    assert "second paragraph" in store.file_bytes(item.id, "txt").decode().lower()


async def test_epub_passthrough(client, store):
    book = render_epub([Document(title="A Shared Book", blocks=[Heading("One", 2), Paragraph("Chapter one text.")],
                                 byline="An Author")])
    data = await _upload(client, book, "shared.epub", "application/epub+zip")
    item = store.get(data["id"])
    assert item.title == "A Shared Book" and item.byline == "An Author"
    assert store.file_bytes(item.id, "epub") == book
    assert "Chapter one text." in store.file_bytes(item.id, "txt").decode()
    assert delivery.load_document(store, item.id) is None
    assert delivery.build_auto_delivery(store, "e", "1") == book  # delivered alone, unchanged


async def test_html_and_txt_files(client, store):
    html = ("<html><head><title>Saved Page</title></head><body><article><h1>Saved Page</h1>"
            + "".join(f"<p>Paragraph {i} of the saved article says something different about reading on "
                      f"paper, number {i}, so nothing is a duplicate.</p>" for i in range(6))
            + "</article></body></html>").encode()
    h = await _upload(client, html, "saved.html", "text/html")
    assert store.get(h["id"]).status == "ready" and store.get(h["id"]).word_count > 50
    t = await _upload(client, "Café notes\r\nline two\r\n".encode("cp1252"), "cafe-notes.txt", "text/plain")
    txt = store.file_bytes(t["id"], "txt").decode()
    assert "Café notes" in txt and "\r" not in txt


async def test_digest_of_files_is_valid(client, store):
    await _upload(client, "First text file with a few words.".encode(), "one.txt", "text/plain")
    await _upload(client, "Second text file with a few words.".encode(), "two.txt", "text/plain")
    data = delivery.build_auto_delivery(store, "e", "1")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.namelist()[0] == "mimetype"
        chapters = [n for n in z.namelist() if n.startswith("OEBPS/text/")]
        assert len(chapters) == 2
        for n in z.namelist():
            if n.endswith((".opf", ".xhtml", ".ncx", ".xml")):
                etree.fromstring(z.read(n))
