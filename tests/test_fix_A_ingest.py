"""Regression tests for review findings 1, 6, 7, 8, 10/14, 13, 18, 21, 22 and 23 (ingest)."""

from __future__ import annotations

import asyncio
import io
import sqlite3
import time
import zipfile

import pytest

from sendtox3 import delivery, ingest
from sendtox3.config import Options
from sendtox3.ingest import IngestError, Processor, StorageError, epub_to_document
from sendtox3.models import IngestRequest
from sendtox3.textmodel import Document, Paragraph

from test_web_fakes import epub_chapters, epub_title, install_fakes


@pytest.fixture
def web(monkeypatch):
    return install_fakes(monkeypatch)


@pytest.fixture
def proc(store, opts, web):
    return Processor(store, opts)


def _doc(store, item):
    return delivery.load_document(store, item.id)


# ------------------------------------------------------------- finding 1: several links


THREE = "https://example.com/one\nhttps://example.com/two\nhttps://example.com/three"


async def test_several_bare_links_become_one_url_item_each(proc, store, web):
    for n in ("one", "two", "three"):
        web.add(f"https://example.com/{n}", f"Article {n}")
    results = await proc.process_all(IngestRequest(text=THREE))
    assert [r.item.title for r in results] == ["Article one", "Article two", "Article three"]
    assert all(r.item.kind == "url" and r.item.status == "ready" and not r.duplicate for r in results)
    assert web.calls == ["https://example.com/one", "https://example.com/two", "https://example.com/three"]
    assert store.count() == 3
    # process_detailed reports the first item; a repeat is three duplicates.
    again = await proc.process_detailed(IngestRequest(text=THREE))
    assert again.duplicate and again.item.id == results[0].item.id
    assert store.count() == 3


async def test_ios_and_ha_style_link_lists_are_split_too(proc, store, web):
    # The iOS Shortcut sends url = first link plus the whole text; HA sends EXTRA_TEXT.
    results = await proc.process_all(IngestRequest(url="https://example.com/one", text=THREE, source="http"))
    assert len(results) == 3 and store.count() == 3
    ha = await proc.process_all(IngestRequest(text="https://example.org/a https://example.org/b",
                                              source="ha_share", sender="Pixel 8"))
    assert [r.item.source_url for r in ha] == ["https://example.org/a", "https://example.org/b"]


async def test_link_list_comment_is_kept_on_each_item(proc, store, web):
    results = await proc.process_all(IngestRequest(text="Two great reads: https://example.com/r1 https://example.com/r2"))
    assert len(results) == 2
    for r in results:
        assert _doc(store, r.item).notes == ["Comment: Two great reads"]


async def test_two_spellings_of_one_link_are_one_link(proc, store, web):
    results = await proc.process_all(IngestRequest(text="https://example.com/same https://www.example.com/same/"))
    assert len(results) == 1 and results[0].item.kind == "url"


async def test_too_many_links_or_too_much_text_stay_one_text_item(proc, store, web):
    many = " ".join(f"https://example.com/l{i}" for i in range(11))
    r = await proc.process_all(IngestRequest(text=many))
    assert len(r) == 1 and r[0].item.kind == "text" and r[0].item.source_url == "https://example.com/l0"
    prose = ("A long paragraph of my own notes that goes on for quite a while and mentions two pages, "
             "which are not the point of it at all, because the point is the notes themselves. ")
    r = await proc.process_all(IngestRequest(text=prose + "https://example.com/x https://example.com/y"))
    assert len(r) == 1 and r[0].item.kind == "text"
    assert web.calls == []


# ---------------------------------------------------------- finding 7: comment + link


@pytest.mark.parametrize("share", [
    "Check out this article https://example.com/a1",
    "Check out @NASA's post: https://example.com/a2",
    "Shared via Google News https://example.com/a3",
    "via Pocket https://example.com/a4",
])
async def test_share_sheet_template_plus_link_is_the_article(proc, store, web, share):
    url = share.rsplit(" ", 1)[1]
    web.add(url, "A real article", text=" ".join(f"word{i}" for i in range(200)))
    item = await proc.process(IngestRequest(text=share))
    assert item.kind == "url" and item.title == "A real article" and item.word_count == 200
    assert _doc(store, item).notes == []  # template text is not a comment worth keeping


async def test_users_own_remark_is_kept_as_a_comment_note(proc, store, web):
    web.add("https://example.com/c1", "Some article")
    item = await proc.process(IngestRequest(text="Great read for the train https://example.com/c1"))
    assert item.kind == "url" and _doc(store, item).notes == ["Comment: Great read for the train"]


async def test_comment_on_a_failed_fetch_is_kept(proc, store, web):
    web.pages["https://example.com/down"] = RuntimeError("connection reset")
    item = await proc.process(IngestRequest(text="for the book club https://example.com/down"))
    assert item.status == "failed" and item.kind == "url"
    assert "for the book club" in store.file_bytes(item.id, "txt").decode()


# ------------------------------------------------- finding 8: notes on a queued link


async def test_selection_on_a_link_already_queued_is_a_new_text_item(proc, store, web):
    web.add("https://example.com/zz", "Some page")
    first = await proc.process_detailed(IngestRequest(url="https://example.com/zz"))
    sel = await proc.process_detailed(IngestRequest(url="https://example.com/zz", text="Great quote I want to keep",
                                                    title="Some page"))
    assert not sel.duplicate and sel.item.id != first.item.id
    assert sel.item.kind == "text" and sel.item.source_url == "https://example.com/zz"
    assert "Great quote I want to keep" in store.file_bytes(sel.item.id, "txt").decode()
    # Without a share title the queued item's title decides; no new fetch either way.
    sel2 = await proc.process_detailed(IngestRequest(url="https://example.com/zz", text="Another short quote"))
    assert not sel2.duplicate and sel2.item.kind == "text"
    assert web.calls == ["https://example.com/zz"]


async def test_comment_on_a_link_already_queued_is_a_new_item(proc, store, web):
    web.add("https://example.com/zz", "Some page")
    first = await proc.process_detailed(IngestRequest(url="https://example.com/zz"))
    noted = await proc.process_detailed(IngestRequest(
        url="https://example.com/zz", text="My comment on this: really insightful https://example.com/zz"))
    assert not noted.duplicate and noted.item.id != first.item.id and noted.item.kind == "url"
    assert _doc(store, noted.item).notes == ["Comment: My comment on this: really insightful"]
    # The same remark again is a duplicate of that item; the bare link of the first.
    again = await proc.process_detailed(IngestRequest(text="My comment on this: really insightful https://example.com/zz"))
    assert again.duplicate and again.item.id == noted.item.id
    bare = await proc.process_detailed(IngestRequest(text="https://example.com/zz"))
    assert bare.duplicate and bare.item.id == first.item.id


async def test_title_or_filler_next_to_a_queued_link_is_still_a_duplicate(proc, store, web):
    web.add("https://example.com/t", "The Quiet Revolution of E-Paper")
    first = await proc.process_detailed(IngestRequest(text="https://example.com/t"))
    for req in (IngestRequest(url="https://example.com/t", text="quiet revolution"),
                IngestRequest(text="The Quiet Revolution of E-Paper https://example.com/t"),
                IngestRequest(text="Check out this article https://example.com/t")):
        r = await proc.process_detailed(req)
        assert r.duplicate and r.item.id == first.item.id, req
    assert web.calls == ["https://example.com/t"]


async def test_mailto_link_is_never_a_source(proc, store, web):
    item = await proc.process(IngestRequest(url="mailto:ann@example.com", text="Call Ann about the trip"))
    assert item.kind == "text" and item.source_url == ""
    item2 = await proc.process(IngestRequest(kind="text", url="tel:+15551234", text="Dentist number"))
    assert item2.source_url == ""
    assert web.calls == []


# ---------------------------------------------------------- findings 10 and 14: budget


def test_link_shares_get_the_background_budget(store, web):
    ocr = Processor(store, Options(pdf_ocr=True))
    for source in ("http", "email", "ha_share", "admin"):
        budget = ocr.budget_for(IngestRequest(url="https://example.com/scan.pdf", source=source))
        assert budget == ingest.BACKGROUND_BUDGET_SECONDS + ingest.OCR_EXTRA_BUDGET_SECONDS
        assert budget > ocr.opts.fetch_timeout_seconds + 600  # the fetch, then the OCR conversion's own timeout
    plain = Processor(store, Options(fetch_timeout_seconds=120))
    assert plain.budget_for(IngestRequest(url="https://example.com/a")) > 120  # a long fetch timeout now matters


async def test_linked_pdf_slower_than_25_s_is_not_cut_off(proc, store, web, monkeypatch):
    # Scaled down: a "linked PDF" taking longer than the old 25 s HTTP budget (here 0.25)
    # but within the background budget is stored ready, not "Timed out ... fetching".
    proc.http_budget = 0.25  # the old link-share budget; no longer used
    proc.background_budget = 2.0
    web.delay = 0.4
    web.add("https://example.com/report.pdf", "Annual report")
    item = await proc.process(IngestRequest(url="https://example.com/report.pdf"))
    assert item.status == "ready" and item.title == "Annual report"


# --------------------------------------------------------- finding 13: storage errors


def _enospc(*a, **kw):
    raise OSError(28, "No space left on device")


async def test_disk_full_raises_storage_error_and_keeps_the_share_spooled(proc, store, web, monkeypatch):
    monkeypatch.setattr(store, "add_item", _enospc)
    with pytest.raises(StorageError, match="No space left on device"):
        await proc.process_detailed(IngestRequest(text="A note that must not be lost"))
    assert store.count() == 0
    assert [e.request.text for e in store.spooled_requests()] == ["A note that must not be lost"]


async def test_disk_full_while_storing_the_failed_fallback_raises(proc, store, web, monkeypatch):
    web.pages["https://example.com/boom"] = RuntimeError("parser exploded")
    monkeypatch.setattr(store, "add_item", _enospc)
    with pytest.raises(StorageError):
        await proc.process(IngestRequest(text="https://example.com/boom"))
    with pytest.raises(StorageError):
        await proc.process_many([IngestRequest(text="first"), IngestRequest(text="second")])
    assert store.count() == 0


async def test_database_error_raises_storage_error(proc, store, web, monkeypatch):
    def locked(*a, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "find_duplicate", locked)
    with pytest.raises(StorageError, match="disk I/O error"):
        await proc.process(IngestRequest(text="hello"))


async def test_spool_write_failure_raises_storage_error(proc, store, web, monkeypatch):
    monkeypatch.setattr(store, "spool_add", _enospc)
    with pytest.raises(StorageError):
        await proc.process(IngestRequest(text="hello"))
    assert store.count() == 0


async def test_storage_error_retry_reuses_the_spool_entry(proc, store, web, monkeypatch):
    real = store.add_item
    monkeypatch.setattr(store, "add_item", _enospc)
    with pytest.raises(StorageError):
        await proc.process(IngestRequest(text="retry me", source="ha_share"))
    monkeypatch.setattr(store, "add_item", real)
    item = await proc.process(IngestRequest(text="retry me", source="ha_share", received_at=1))
    assert item.status == "ready" and store.spool_count() == 0


async def test_other_failures_still_store_a_failed_item(proc, store, web):
    web.pages["https://example.com/broken"] = RuntimeError("parser exploded")
    item = await proc.process(IngestRequest(text="https://example.com/broken"))
    assert item.status == "failed" and store.get(item.id) is not None and store.spool_count() == 0


# --------------------------------------------------------- finding 18: email sources


async def test_email_text_keeps_the_given_url_even_when_empty(proc, store, web):
    body = ("Hello readers, this week we look at e-paper.\n\nSponsored by Acme: "
            "https://click.convertkit-mail.com/abc123/xyz\n\nMore news below.")
    item = await proc.process(IngestRequest(kind="text", source="email", text=body, title="Weekly",
                                            sender="Ann Newsletter <ann@news.example>"))
    assert item.source_url == "" and item.site == ""
    assert "Source:" not in store.file_bytes(item.id, "txt").decode()
    picked = await proc.process(IngestRequest(kind="text", source="email", text="Selected words",
                                              url="https://example.com/page", sender="ann@news.example"))
    assert picked.source_url == "https://example.com/page"


async def test_failed_email_text_does_not_cite_a_body_link(proc, store, monkeypatch):
    async def slow_thread(fn, *a, **kw):
        await asyncio.sleep(1)

    proc.background_budget = 0.05
    monkeypatch.setattr(ingest.asyncio, "to_thread", slow_thread)
    item = await proc.process(IngestRequest(kind="text", source="email", text="Hi\nhttps://tracker.example/x"))
    assert item.status == "failed" and item.source_url == ""


# ------------------------------------------------------ finding 21: in-book headline


async def test_book_keeps_the_original_headline(proc, store, web):
    headline = "Why do cats purr? Scientists finally have an answer"
    web.add("https://example.com/cats", headline)
    item = await proc.process(IngestRequest(text="https://example.com/cats"))
    assert item.title == "Why do cats purr Scientists finally have an answer"  # file-name safe
    epub = store.file_bytes(item.id, "epub")
    assert epub_title(epub) == headline and epub_chapters(epub) == [headline]
    assert store.file_bytes(item.id, "txt").decode().startswith(headline + "\n")
    assert _doc(store, item).title == headline


async def test_long_headline_is_not_cut_at_90_bytes_in_the_book(proc, store, web):
    headline = ("Officials say the new regional transit plan, approved late on Tuesday after months of "
                "debate, will affect commuters in three counties")
    web.add("https://example.com/long", headline)
    item = await proc.process(IngestRequest(text="https://example.com/long"))
    assert len(item.title.encode()) <= 90
    assert epub_title(store.file_bytes(item.id, "epub")) == headline


async def test_counter_suffix_is_kept_in_the_book_title(proc, store, web):
    web.add("https://example.com/q1", "Is it over?")
    web.add("https://example.com/q2", "Is it over?")
    a = await proc.process(IngestRequest(text="https://example.com/q1"))
    b = await proc.process(IngestRequest(text="https://example.com/q2"))
    assert (a.title, b.title) == ("Is it over", "Is it over (2)")
    assert epub_title(store.file_bytes(b.id, "epub")) == "Is it over? (2)"


async def test_digest_chapters_use_the_original_headlines(proc, store, web):
    web.add("https://example.com/h1", "What next? A guide")
    web.add("https://example.com/h2", "Q&A: \"quotes\" and colons")
    await proc.process(IngestRequest(text="https://example.com/h1"))
    await proc.process(IngestRequest(text="https://example.com/h2"))
    data = delivery.build_auto_delivery(store, "ev", "1")
    assert epub_chapters(data) == ["What next? A guide", "Q&A: \"quotes\" and colons"]


# ------------------------------------------------------- finding 22: sender as byline


async def test_sender_is_not_the_author_of_a_fetched_article(proc, store, web):
    web.add("https://www.nytimes.com/2026/10/25/a.html", "An article")  # no byline
    item = await proc.process(IngestRequest(url="https://www.nytimes.com/2026/10/25/a.html", source="ha_share",
                                            sender="Pixel 8"))
    assert item.byline == ""
    assert "Pixel 8" not in store.file_bytes(item.id, "epub").decode("utf-8", "ignore")
    emailed = await proc.process(IngestRequest(kind="url", source="email", url="https://example.com/m",
                                               sender="Ann <ann@example.com>"))
    assert emailed.byline == ""
    pdf = await proc.process(IngestRequest(data=b"%PDF-1.4\n(Hello) Tj\n%%EOF", filename="r.pdf", source="email",
                                           sender="Ann <ann@example.com>"))
    assert pdf.kind == "pdf" and pdf.byline == ""


async def test_sender_is_still_the_author_of_notes_and_text_files(proc, store, web):
    note = await proc.process(IngestRequest(text="My own thoughts on the matter", source="ha_share", sender="Pixel 8"))
    assert note.byline == "Pixel 8"
    txt = await proc.process(IngestRequest(data=b"Notes from the meeting", filename="m.txt", source="email",
                                           sender="Ann <ann@example.com>"))
    assert txt.byline == "Ann"
    body = await proc.process(IngestRequest(kind="html", source="email", html="<p>Dear all, see below.</p>",
                                            title="Update", sender="Bob <bob@example.com>"))
    assert body.byline == "Bob"


async def test_article_byline_wins_over_nothing(proc, store, web):
    web.add("https://example.com/by", "Bylined", byline="Jane Writer")
    item = await proc.process(IngestRequest(url="https://example.com/by", sender="Pixel 8", source="ha_share"))
    assert item.byline == "Jane Writer"


# ---------------------------------------------------------------- EPUB uploads (6, 23)


def _epub(chapters: dict[str, str], spine: list[str]) -> bytes:
    manifest = "".join(f'<item id="{k}" href="{k}.xhtml" media-type="application/xhtml+xml"/>' for k in chapters)
    refs = "".join(f'<itemref idref="{k}"/>' for k in spine)
    opf = ('<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book</dc:title>'
           '<dc:language>en</dc:language></metadata>'
           f"<manifest>{manifest}</manifest><spine>{refs}</spine></package>")
    container = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 "</rootfiles></container>")
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for k, body in chapters.items():
            z.writestr(f"OEBPS/{k}.xhtml",
                       '<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
                       f"<head><title>{k}</title></head><body>{body}</body></html>")
    return bio.getvalue()


def _texts(doc: Document) -> list[str]:
    return [getattr(b, "text", "") for b in doc.blocks]


def test_epub_spine_repeats_are_read_once():
    chapter = "".join(f"<p>Paragraph number {i} of the only chapter.</p>" for i in range(50))
    once = epub_to_document(_epub({"c1": chapter}, ["c1"]))
    many = epub_to_document(_epub({"c1": chapter}, ["c1"] * 400))
    assert len(once.blocks) == 50 and _texts(many) == _texts(once)


def test_epub_text_is_capped(monkeypatch):
    monkeypatch.setattr(ingest, "_MAX_EPUB_TEXT_CHARS", 500)
    chapters = {f"c{n}": "".join(f"<p>{'x' * 90} {n}-{i}</p>" for i in range(20)) for n in range(5)}
    doc = epub_to_document(_epub(chapters, list(chapters)))
    assert sum(len(t) for t in _texts(doc)) <= 500
    assert doc.notes and "stops early" in doc.notes[0]


def test_epub_extraction_stops_at_the_deadline():
    chapters = {f"c{n}": "<p>text</p>" for n in range(3)}
    with pytest.raises(IngestError, match="Timed out"):
        epub_to_document(_epub(chapters, list(chapters)), deadline=time.monotonic() - 1)


def test_epub_text_outside_paragraphs_is_kept():
    body1 = ("<div><h2>Chapter 1</h2>It was a dark and stormy night.<br/>Except at occasional intervals."
             "<p>Second paragraph.</p></div><section>Text in a section<span> more</span></section>")
    body2 = "Body text directly in body.<br/>More lines."
    doc = epub_to_document(_epub({"c1": body1, "c2": body2}, ["c1", "c2"]))
    text = "\n".join(_texts(doc))
    for passage in ("Chapter 1", "It was a dark and stormy night.", "Except at occasional intervals.",
                    "Second paragraph.", "Text in a section more", "Body text directly in body.", "More lines."):
        assert passage in text, passage


async def test_uploaded_epub_txt_has_all_text(proc, store):
    data = _epub({"c1": "<div>Loose text in a div.</div>"}, ["c1"])
    item = await proc.process(IngestRequest(data=data, filename="book.epub"))
    assert item.status == "ready" and "Loose text in a div." in store.file_bytes(item.id, "txt").decode()
    assert store.file_bytes(item.id, "epub") == data


async def test_uploaded_epub_with_a_repeated_spine_is_read_once(proc, store):
    chapter = "".join(f"<p>{'z' * 200} {i}</p>" for i in range(400))  # ~80 KB of text, ~2 KB zipped
    data = _epub({"c1": chapter}, ["c1"] * 300)
    item = await proc.process(IngestRequest(data=data, filename="bomb.epub"))
    assert item.status == "ready" and item.word_count == 800


def test_fallback_document_byline_and_comment():
    ctx = ingest._Context(url="https://example.com/x", kind="url", comment="for later")
    doc = ingest._fallback_document(IngestRequest(kind="url", url="https://example.com/x", sender="Pixel 8"),
                                    ctx, "boom")
    assert doc.byline == "" and any(getattr(b, "text", "") == "Comment: for later" for b in doc.blocks)
    note = ingest._fallback_document(IngestRequest(text="my note", sender="Pixel 8"), ingest._Context(kind="text"),
                                     "boom")
    assert note.byline == "Pixel 8"
    assert Paragraph("my note") in note.blocks
