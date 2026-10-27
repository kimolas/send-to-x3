"""Deterministic stand-ins for the components other engineers own (extract/*, render/*),
plus helpers shared by the ingest, delivery, web, OPDS and plugin-bundle unit tests.

``install_fakes(monkeypatch)`` swaps the dependency seams in ``sendtox3.ingest`` and
``sendtox3.delivery`` so these tests pass without the real extractors and renderers.
The real modules are exercised by ``tests/integration/test_flow_*.py``.
"""

from __future__ import annotations

import asyncio
import html as _html
import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

from lxml import etree

from sendtox3 import delivery, ingest
from sendtox3.models import Item, new_item_id
from sendtox3.textmodel import Document, Heading, Paragraph, clean_text

URL_RE = re.compile(r"""https?://[^\s<>"']+""")


# --------------------------------------------------------------------------------------
# render/*
# --------------------------------------------------------------------------------------


def fake_safe_title(title: str, *, max_bytes: int = 90) -> str:
    t = clean_text(title)
    t = re.sub(r"\.{2,}", "…", t)
    t = re.sub(r'[{}*?"<>]', "", t)
    t = re.sub(r"[/\\|:]", "-", t)
    t = t.strip(" .")
    t = t.encode("utf-8")[:max_bytes].decode("utf-8", "ignore").strip(" .")
    return t or "Note"


def fake_unique_title(title: str, taken: set[str], *, max_bytes: int = 90) -> str:
    base = fake_safe_title(title, max_bytes=max_bytes)
    t, n = base, 2
    while t in taken:
        t = f"{base} ({n})"
        n += 1
    return t


def fake_render_txt(doc: Document) -> bytes:
    body = "\n\n".join(getattr(b, "text", "") for b in doc.blocks if getattr(b, "text", ""))
    tail = f"\n\n———\nSource: {doc.source_url}" if doc.source_url else ""
    return f"{doc.title}\n———\n\n{body}{tail}\n".encode("utf-8")


def _x(s: str) -> str:
    return _html.escape(s, quote=True)


def fake_render_epub(docs: list[Document], *, book_title: str = "", identifier: str = "") -> bytes:
    """A real (small) EPUB zip. Chapters are STORED so the size tracks the text length."""
    title = book_title or (docs[0].title if docs else "Untitled")
    ident = identifier or "urn:uuid:00000000-0000-0000-0000-000000000000"
    chapters = []
    for i, d in enumerate(docs, 1):
        paras = "".join(f"<p>{_x(getattr(b, 'text', ''))}</p>" for b in d.blocks if getattr(b, "text", ""))
        chapters.append((f"OEBPS/text/item-{i:03d}.xhtml",
                         '<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
                         f"<head><title>{_x(d.title)}</title></head><body><h1>{_x(d.title)}</h1>{paras}</body></html>"))
    manifest = "".join(f'<item id="c{i}" href="text/item-{i:03d}.xhtml" media-type="application/xhtml+xml"/>'
                       for i in range(1, len(chapters) + 1))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, len(chapters) + 1))
    opf = ('<?xml version="1.0" encoding="UTF-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
           'unique-identifier="bookid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="bookid">{_x(ident)}</dc:identifier><dc:title>{_x(title)}</dc:title>'
           f'<dc:language>en</dc:language></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>')
    container = ('<?xml version="1.0" encoding="UTF-8"?>\n<container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile '
                 'full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        zi = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        z.writestr(zi, b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(zipfile.ZipInfo("META-INF/container.xml", date_time=(1980, 1, 1, 0, 0, 0)), container)
        z.writestr(zipfile.ZipInfo("OEBPS/content.opf", date_time=(1980, 1, 1, 0, 0, 0)), opf)
        for name, data in chapters:
            z.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data.encode("utf-8"),
                       compress_type=zipfile.ZIP_STORED)
    return bio.getvalue()


def epub_title(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        opf = etree.fromstring(z.read("OEBPS/content.opf"))
    return opf.xpath("//*[local-name()='title']/text()")[0]


def epub_chapters(data: bytes) -> list[str]:
    """Chapter titles (h1) in spine order."""
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in sorted(n for n in z.namelist() if n.startswith("OEBPS/text/")):
            root = etree.fromstring(z.read(name))
            out.append(root.xpath("//*[local-name()='h1']/text()")[0])
    return out


# --------------------------------------------------------------------------------------
# extract/*
# --------------------------------------------------------------------------------------


def fake_first_url(text: str) -> str:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,;:!?)") if m else ""


def fake_classify(text: str, url: str = "", title: str = "") -> str:
    rem = " ".join(URL_RE.sub(" ", text or "").split())
    if not rem:
        return "url"
    if title and rem.casefold() in title.casefold():
        return "url"
    return "text"


def fake_derive_title(text: str) -> str:
    first = " ".join(URL_RE.sub(" ", text or "").split())
    return first[:80]


def fake_text_to_document(text: str, *, title: str = "", source_url: str = "") -> Document:
    paras = [clean_text(p) for p in re.split(r"\n+", text or "") if clean_text(p)]
    return Document(title=title or fake_derive_title(text), blocks=[Paragraph(p) for p in paras],
                    source_url=source_url)


def fake_html_to_document(html: str, *, url: str = "") -> Document:
    import lxml.html

    root = lxml.html.document_fromstring(html or "<p></p>")
    title = clean_text(" ".join(root.xpath("//title/text()")))
    paras = [clean_text(p.text_content()) for p in root.iter("p") if clean_text(p.text_content())]
    return Document(title=title, blocks=[Paragraph(p) for p in paras], source_url=url)


def fake_pdf_to_document(data: bytes, *, title_hint: str = "", source_url: str = "", **_kw) -> Document:
    text = data.decode("latin-1", "replace")
    words = re.findall(r"\(([^)]*)\)", text)  # text inside PDF string literals
    return Document(title=title_hint or "PDF", blocks=[Paragraph(" ".join(words) or "pdf text")],
                    source_url=source_url)


def fake_decode_html(body: bytes, charset_hint: str | None) -> str:
    return body.decode(charset_hint or "utf-8", "replace")


Page = Union[Document, BaseException, Callable[[str], Document]]


@dataclass
class FakeWeb:
    """``url_to_document`` replacement: pages by URL, call log, optional delay."""

    pages: dict[str, Page] = field(default_factory=dict)
    delay: float = 0.0
    calls: list[str] = field(default_factory=list)

    def add(self, url: str, title: str, text: str = "Some article text that is long enough.", **kw: Any) -> None:
        self.pages[url] = Document(title=title, blocks=[Paragraph(p) for p in text.split("\n\n") if p],
                                   source_url=url, site=kw.pop("site", ""), **kw)

    async def __call__(self, url: str, *, opts: Any, title_hint: str = "") -> Document:
        self.calls.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        page = self.pages.get(url)
        if page is None:
            return Document(title=title_hint or url, blocks=[Paragraph(f"Fetched {url}")], source_url=url)
        if isinstance(page, BaseException):
            raise page
        if callable(page):
            return page(url)
        return page


def install_fakes(monkeypatch, web: Optional[FakeWeb] = None) -> FakeWeb:
    web = web or FakeWeb()
    patches = {
        "url_to_document": web,
        "html_to_document": fake_html_to_document,
        "text_to_document": fake_text_to_document,
        "classify": fake_classify,
        "first_url": fake_first_url,
        "derive_title": fake_derive_title,
        "pdf_to_document": fake_pdf_to_document,
        "clean_url": lambda u: u.strip(),
        "decode_html": fake_decode_html,
        "unique_title": fake_unique_title,
        "render_txt": fake_render_txt,
        "render_epub": fake_render_epub,
    }
    for name, fn in patches.items():
        monkeypatch.setattr(ingest, name, fn)
    monkeypatch.setattr(delivery, "render_epub", fake_render_epub)
    monkeypatch.setattr(delivery, "safe_title", fake_safe_title)
    return web


# --------------------------------------------------------------------------------------
# Store helpers
# --------------------------------------------------------------------------------------

_counter = [0]


def add_item(store, title: str, *, created_at: Optional[int] = None, words: int = 50, text: str = "",
             delivered: Optional[dict] = None, status: str = "ready", source_url: str = "", kind: str = "text",
             passthrough: bool = False, epub_size: Optional[int] = None, site: str = "") -> Item:
    """Store an item rendered with the fakes (doc.json included unless ``passthrough``).

    ``epub_size`` pads the stored EPUB to (about) that many bytes, for size-cap tests.
    """
    _counter[0] += 1
    created_at = created_at if created_at is not None else int(time.time()) - 1000 + _counter[0]
    body = text or " ".join(f"word{i}" for i in range(words))
    doc = Document(title=title, blocks=[Heading("Intro", 2), Paragraph(body)] if body else [], source_url=source_url,
                   site=site)
    item = Item(id=new_item_id(), created_at=created_at, kind=kind, source="test", title=title, status=status,
                word_count=len(body.split()), source_url=source_url, site=site, delivered=dict(delivered or {}))
    if epub_size:
        filler = "x" * max(0, epub_size - len(fake_render_epub([doc])) - 20)
        doc = Document(title=title, blocks=[Paragraph(body), Paragraph(filler)], source_url=source_url, site=site)
    epub = fake_render_epub([doc], book_title=title)
    if not passthrough:
        delivery.save_document(store, item.id, doc)
    store.add_item(item, epub=epub, txt=fake_render_txt(doc))
    return item


def test_fake_epub_is_a_zip_with_title():
    data = fake_render_epub([Document(title="A & B", blocks=[Paragraph("x")])])
    assert data[:4] == b"PK\x03\x04" and epub_title(data) == "A & B"
    assert epub_chapters(data) == ["A & B"]
