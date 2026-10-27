"""Processor: classification (§7.1), dedupe, titles, kinds, fallbacks, budgets."""

from __future__ import annotations

import asyncio
import io
import time
import zipfile

import pytest

from sendtox3 import delivery, ingest
from sendtox3.ingest import (
    Processor,
    matches_title,
    sniff_file_type,
    text_dedupe_key,
    url_dedupe_key,
)
from sendtox3.models import IngestRequest
from sendtox3.textmodel import Document, Paragraph

from test_web_fakes import epub_title, fake_render_epub, install_fakes


@pytest.fixture
def web(monkeypatch):
    return install_fakes(monkeypatch)


@pytest.fixture
def proc(store, opts, web):
    return Processor(store, opts)


def _txt(store, item):
    return store.file_bytes(item.id, "txt").decode("utf-8")


# ------------------------------------------------------------------ classification


async def test_bare_url_becomes_url_item(proc, store, web):
    web.add("https://example.com/a", "How e-ink works")
    item = await proc.process(IngestRequest(text="  https://example.com/a \n"))
    assert item.kind == "url" and item.status == "ready" and item.source == "http"
    assert item.title == "How e-ink works"
    assert item.source_url == "https://example.com/a"
    assert web.calls == ["https://example.com/a"]
    assert epub_title(store.file_bytes(item.id, "epub")) == item.title
    assert store.get(item.id) is not None and delivery.load_document(store, item.id) is not None


async def test_url_field_with_empty_text_is_url(proc, web):
    web.add("https://example.com/b", "B page")
    item = await proc.process(IngestRequest(url="https://example.com/b", title="B page - Example"))
    assert item.kind == "url" and item.title == "B page"


async def test_url_plus_short_text_matching_page_title_is_url(proc, web):
    web.add("https://example.com/c", "The Quiet Revolution of E-Paper")
    item = await proc.process(IngestRequest(text="quiet revolution https://example.com/c"))
    assert item.kind == "url" and item.title == "The Quiet Revolution of E-Paper"
    assert web.calls == ["https://example.com/c"]  # fetched once, document reused


async def test_android_style_title_and_site_plus_url_is_url(proc, web):
    web.add("https://example.com/d", "Why Paper Still Wins")
    item = await proc.process(IngestRequest(text="Why Paper Still Wins | Example News\nhttps://example.com/d"))
    assert item.kind == "url"


async def test_url_plus_unrelated_short_text_is_url_with_comment(proc, store, web):
    # One link plus a few words: the article, with the words kept as a comment note.
    web.add("https://example.com/e", "Completely different title")
    item = await proc.process(IngestRequest(text="remember to buy milk https://example.com/e"))
    assert item.kind == "url" and item.title == "Completely different title"
    assert item.source_url == "https://example.com/e"
    assert delivery.load_document(store, item.id).notes == ["Comment: remember to buy milk"]


async def test_url_plus_long_text_is_text_without_fetch(proc, store, web):
    long_text = ("This is a selected passage from a page that I want to read later on the e-reader. " * 5
                 + "https://example.com/f")
    item = await proc.process(IngestRequest(text=long_text))
    assert item.kind == "text" and item.source_url == "https://example.com/f"
    assert web.calls == []


async def test_url_field_plus_selection_is_text(proc, web):
    selection = "A long selection of text copied from the page, more than thirty words long. " * 3
    item = await proc.process(IngestRequest(url="https://example.com/g", text=selection, title="Page"))
    assert item.kind == "text" and item.source_url == "https://example.com/g"
    assert web.calls == []


async def test_plain_text_gets_derived_title(proc, store):
    item = await proc.process(IngestRequest(text="Grocery list\nmilk\neggs"))
    assert item.kind == "text" and item.title == "Grocery list milk eggs"
    assert item.word_count == 4


async def test_explicit_kinds(proc, web):
    web.add("https://example.com/h", "H")
    as_url = await proc.process(IngestRequest(kind="url", text="see this: https://example.com/h and more words"))
    assert as_url.kind == "url"
    as_text = await proc.process(IngestRequest(kind="text", text="https://example.com/zzz"))
    assert as_text.kind == "text" and as_text.source_url == "https://example.com/zzz"


async def test_kind_url_without_link_fails(proc):
    item = await proc.process(IngestRequest(kind="url", text="no link here"))
    assert item.status == "failed" and "No link" in item.error


async def test_unknown_kind_fails(proc):
    item = await proc.process(IngestRequest(kind="video", text="x"))
    assert item.status == "failed" and "Unknown kind" in item.error


async def test_html_request(proc, store):
    html = "<html><head><title>Saved page</title></head><body><p>Hello from html.</p></body></html>"
    item = await proc.process(IngestRequest(html=html, url="https://example.com/saved"))
    assert item.kind == "url" and item.title == "Saved page" and item.source_url == "https://example.com/saved"
    item2 = await proc.process(IngestRequest(kind="html", html="<p>Only a snippet here.</p>", title="Snippet"))
    assert item2.kind == "text" and item2.title == "Snippet"


async def test_email_source_kind_and_sender_byline(proc):
    item = await proc.process(IngestRequest(source="email", text="Hi, here are my notes.\nSecond line.",
                                            title="Notes", sender="Alice Example <alice@example.com>"))
    assert item.kind == "email" and item.source == "email" and item.byline == "Alice Example"


# ---------------------------------------------------------------------------- dedupe


async def test_same_url_twice_returns_existing(proc, store, web):
    web.add("https://example.com/x", "X")
    r1 = await proc.process_detailed(IngestRequest(text="https://www.example.com/x/"))
    r2 = await proc.process_detailed(IngestRequest(url="http://example.com/x#section"))
    assert not r1.duplicate and r2.duplicate
    assert r1.item.id == r2.item.id and store.count() == 1
    assert len(web.calls) == 1


async def test_same_text_with_different_whitespace_is_duplicate(proc, store):
    a = await proc.process(IngestRequest(text="Hello   world\n\nagain"))
    b = await proc.process(IngestRequest(text="Hello world again "))
    assert a.id == b.id and store.count() == 1


async def test_duplicate_window_is_24_hours(proc, store):
    old = await proc.process(IngestRequest(text="An old note"))
    store._db.execute("UPDATE items SET created_at=? WHERE id=?", (int(time.time()) - 86_400 - 60, old.id))
    new = await proc.process(IngestRequest(text="An old note"))
    assert new.id != old.id and new.title == "An old note (2)"


async def test_failed_duplicate_is_retried_and_replaced(proc, store, web):
    web.pages["https://example.com/flaky"] = RuntimeError("connection reset")
    first = await proc.process(IngestRequest(text="https://example.com/flaky"))
    assert first.status == "failed"
    web.add("https://example.com/flaky", "Flaky page")
    second = await proc.process(IngestRequest(text="https://example.com/flaky"))
    assert second.status == "ready" and second.id != first.id
    assert store.get(first.id) is None  # the undelivered failed attempt was superseded
    assert second.title == "Flaky page"


async def test_failed_duplicate_already_delivered_is_kept(proc, store, web):
    web.pages["https://example.com/f2"] = RuntimeError("boom")
    first = await proc.process(IngestRequest(text="https://example.com/f2"))
    store.mark_delivered(first.id, "auto")
    web.add("https://example.com/f2", "F2")
    second = await proc.process(IngestRequest(text="https://example.com/f2"))
    assert second.status == "ready" and store.get(first.id) is not None


async def test_concurrent_identical_shares_become_one_item(proc, store, web):
    web.add("https://example.com/slow", "Slow")
    web.delay = 0.2
    a, b = await asyncio.gather(proc.process_detailed(IngestRequest(text="https://example.com/slow")),
                                proc.process_detailed(IngestRequest(text="https://example.com/slow")))
    assert a.item.id == b.item.id and store.count() == 1
    assert sorted([a.duplicate, b.duplicate]) == [False, True]
    assert len(web.calls) == 1


def test_dedupe_keys():
    assert url_dedupe_key("https://www.Example.com/a/b/?q=1#frag") == "url:example.com/a/b?q=1"
    assert url_dedupe_key("http://example.com:80/") == "url:example.com/"
    assert url_dedupe_key("http://example.com:8080/x") == "url:example.com:8080/x"
    assert text_dedupe_key("A  b\n") == text_dedupe_key("a b")


# ---------------------------------------------------------------------------- titles


async def test_titles_are_unique(proc, web):
    web.add("https://example.com/1", "Same title")
    web.add("https://example.com/2", "Same title")
    a = await proc.process(IngestRequest(text="https://example.com/1"))
    b = await proc.process(IngestRequest(text="https://example.com/2"))
    assert (a.title, b.title) == ("Same title", "Same title (2)")


async def test_concurrent_different_shares_with_same_title_get_unique_titles(proc, web):
    web.delay = 0.05
    for i in range(4):
        web.add(f"https://example.com/p{i}", "Popular")
    items = await asyncio.gather(*(proc.process(IngestRequest(text=f"https://example.com/p{i}")) for i in range(4)))
    assert sorted(i.title for i in items) == ["Popular", "Popular (2)", "Popular (3)", "Popular (4)"]


async def test_title_falls_back_to_host(proc, web):
    web.pages["https://example.org/x"] = Document(title="", blocks=[Paragraph("text " * 20)],
                                                  source_url="https://example.org/x")
    item = await proc.process(IngestRequest(text="https://example.org/x"))
    assert item.title == "example.org"


async def test_unique_title_failure_uses_basic_sanitizer(proc, monkeypatch):
    def broken(title, taken):
        raise RuntimeError("names module bug")

    monkeypatch.setattr(ingest, "unique_title", broken)
    item = await proc.process(IngestRequest(text="Wait... what? {x} a/b\nmore"))
    assert ".." not in item.title and "{" not in item.title and "/" not in item.title
    assert item.status == "ready"


# ------------------------------------------------------------------------- failures


async def test_extractor_exception_gives_failed_item_with_fallback(proc, store, web):
    web.pages["https://example.com/broken"] = RuntimeError("parser exploded")
    item = await proc.process(IngestRequest(text="https://example.com/broken", title="Broken page"))
    assert item.status == "failed" and "parser exploded" in item.error
    assert item.title == "Broken page" and item.kind == "url"
    assert item.source_url == "https://example.com/broken" and item.is_pending
    txt = _txt(store, item)
    assert "could not process" in txt and "https://example.com/broken" in txt
    assert store.file_bytes(item.id, "epub")[:4] == b"PK\x03\x04"


async def test_fetch_error_reason_is_used(proc, web):
    class FetchError(Exception):
        def __init__(self, reason):
            super().__init__(reason)
            self.reason = reason

    web.pages["https://example.com/403"] = FetchError("The site refused the request (HTTP 403)")
    item = await proc.process(IngestRequest(text="https://example.com/403"))
    assert item.error == "The site refused the request (HTTP 403)"


async def test_timeout_budget_keeps_raw_url(proc, store, web):
    web.delay = 2.0
    proc.background_budget = 0.2
    t0 = time.monotonic()
    item = await proc.process(IngestRequest(text="https://example.com/slowpage"))
    assert time.monotonic() - t0 < 1.5
    assert item.status == "failed" and item.error == "Timed out after 0.2 s while fetching the page"
    assert item.source_url == "https://example.com/slowpage"
    assert "https://example.com/slowpage" in _txt(store, item)
    # A later share of the same link retries instead of returning the failed item.
    web.delay = 0
    again = await proc.process(IngestRequest(text="https://example.com/slowpage"))
    assert again.status == "ready" and store.get(item.id) is None


async def test_timeout_keeps_shared_text(proc, store, monkeypatch):
    async def slow_thread(fn, *a, **kw):
        await asyncio.sleep(1)

    proc.background_budget = 0.1
    monkeypatch.setattr(ingest.asyncio, "to_thread", slow_thread)
    item = await proc.process(IngestRequest(text="Important note that must not be lost.\nSecond paragraph."))
    assert item.status == "failed"
    txt = _txt(store, item)
    assert "Important note that must not be lost." in txt and "Second paragraph." in txt


async def test_zero_word_document_is_failed_with_visible_note(proc, store, web):
    web.pages["https://example.com/wall"] = Document(title="Paywalled", blocks=[], source_url="https://example.com/wall",
                                                     notes=["Could not fetch the article (cloudflare-challenge)."])
    item = await proc.process(IngestRequest(text="https://example.com/wall"))
    assert item.status == "failed" and item.error.startswith("Could not fetch the article")
    assert item.word_count == 0
    assert "cloudflare-challenge" in _txt(store, item)


async def test_renderer_failure_uses_minimal_epub(proc, store, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(ingest, "render_epub", broken)
    item = await proc.process(IngestRequest(text="Some text"))
    assert item.status == "failed" and "renderer bug" in item.error
    data = store.file_bytes(item.id, "epub")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.namelist()[0] == "mimetype" and z.read("mimetype") == b"application/epub+zip"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        from lxml import etree

        for name in z.namelist():
            if name.endswith((".xml", ".opf", ".xhtml", ".ncx")):
                etree.fromstring(z.read(name))


async def test_empty_request_is_failed(proc):
    item = await proc.process(IngestRequest(text="   "))
    assert item.status == "failed" and "Nothing to read" in item.error


async def test_store_failure_raises_storage_error(proc, monkeypatch):
    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(proc.store, "add_item", boom)
    with pytest.raises(ingest.StorageError, match="No space left on device"):
        await proc.process(IngestRequest(text="hello"))
    assert proc.store.count() == 0


# ---------------------------------------------------------------------------- files

PDF_BYTES = b"%PDF-1.4\n1 0 obj << >> stream\nBT (Hello) Tj (from) Tj (a) Tj (PDF) Tj ET\nendstream\n%%EOF"


async def test_pdf_upload(proc, store):
    item = await proc.process(IngestRequest(source="admin", data=PDF_BYTES, filename="Annual_report.pdf",
                                            content_type="application/pdf"))
    assert item.kind == "pdf" and item.status == "ready" and item.title == "Annual report"
    assert "Hello from a PDF" in _txt(store, item)


async def test_pdf_disabled(store, opts, web):
    opts.pdf_enabled = False
    item = await Processor(store, opts).process(IngestRequest(data=PDF_BYTES, filename="x.pdf"))
    assert item.status == "failed" and "pdf_enabled" in item.error and item.kind == "pdf"


async def test_same_file_twice_is_duplicate(proc, store):
    a = await proc.process(IngestRequest(data=PDF_BYTES, filename="a.pdf"))
    b = await proc.process(IngestRequest(data=PDF_BYTES, filename="b.pdf"))
    assert a.id == b.id and store.count() == 1


async def test_txt_and_html_files(proc, store):
    t = await proc.process(IngestRequest(data="Café notes\nline two".encode("cp1252"), filename="notes.txt"))
    assert t.kind == "text" and t.title == "notes" and "Café notes" in _txt(store, t)
    h = await proc.process(IngestRequest(data=b"<html><title>Page T</title><p>Body here.</p></html>",
                                         filename="page.html", content_type="text/html"))
    assert h.kind == "text" and h.title == "Page T"


async def test_epub_passthrough(proc, store):
    original = fake_render_epub([Document(title="My Book", blocks=[Paragraph("Chapter text here.")])])
    item = await proc.process(IngestRequest(data=original, filename="book.epub"))
    assert item.status == "ready" and item.title == "My Book"
    assert store.file_bytes(item.id, "epub") == original  # byte for byte
    assert "Chapter text here." in _txt(store, item)
    assert delivery.load_document(store, item.id) is None  # delivered alone, never in a digest


async def test_invalid_epub_and_unknown_file(proc):
    bad = await proc.process(IngestRequest(data=b"PK\x03\x04garbage", filename="x.epub"))
    assert bad.status == "failed" and "EPUB" in bad.error
    unknown = await proc.process(IngestRequest(data=b"\x00\x01\x02\xff" * 10, filename="x.bin"))
    assert unknown.status == "failed" and "Unsupported file type" in unknown.error


def test_sniff_file_type():
    assert sniff_file_type("", "", PDF_BYTES) == "pdf"
    assert sniff_file_type("a.EPUB", "", b"PK\x03\x04xx") == "epub"
    assert sniff_file_type("", "", fake_render_epub([Document(title="t")])) == "epub"
    assert sniff_file_type("a.htm", "", b"x") == "html"
    assert sniff_file_type("", "", b"  <!DOCTYPE html><html>") == "html"
    assert sniff_file_type("a.txt", "", b"\xff\xfe") == "txt"
    assert sniff_file_type("", "application/octet-stream", "plain words".encode()) == "txt"
    assert sniff_file_type("x.bin", "", b"\x00\x9f\x92\x96") == "unknown"


# ------------------------------------------------------------------------- misc


def test_matches_title():
    assert matches_title("quiet revolution", "The Quiet Revolution of E-Paper")
    assert matches_title("Why Paper Still Wins | Example News", "Why Paper Still Wins")
    assert not matches_title("buy milk", "Why Paper Still Wins")
    assert not matches_title("", "x") and not matches_title("x", "")


async def test_process_many_keeps_order(proc):
    items = await proc.process_many([IngestRequest(text="first note"), IngestRequest(text="second note")])
    assert [i.title for i in items] == ["first note", "second note"]


def test_budgets(proc):
    # Every share gets the background budget: a link may be a PDF (the HTTP API answers
    # "still processing" after 25 s and the work goes on).
    assert proc.budget_for(IngestRequest(text="x")) == 150
    assert proc.budget_for(IngestRequest(url="https://example.com/a.pdf", source="ha_share")) == 150
    assert proc.budget_for(IngestRequest(data=b"x", source="http")) == 150
    assert proc.budget_for(IngestRequest(data=b"x", source="email")) == 150


def test_sender_name_keeps_bare_device_names():
    from sendtox3.ingest import _sender_name

    assert _sender_name("Qiong's iPhone 17") == "Qiong's iPhone 17"
    assert _sender_name("The Quiet Letter <news@quiet.example>") == "The Quiet Letter"
    assert _sender_name("news@quiet.example") == "news@quiet.example"
    assert _sender_name("") == ""


def test_ocr_budget(proc):
    proc.opts.pdf_ocr = True
    assert proc.budget_for(IngestRequest(data=b"x", source="email")) > 600
