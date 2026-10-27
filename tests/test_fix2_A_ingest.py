"""Round-2 regression tests for group A (ingest): R2-A1 (EPUB entities under an XHTML
1.1 DOCTYPE), R2-A2 (EPUB chapter/total caps, cheap walker for big chapters), R2-A3
(the in-book headline keeps its leading words), R2-A5 (matches_title with short titles
and site affixes) and R2-A6 (PDF uploads convert cancellably; the timeout says so)."""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

from sendtox3 import delivery, ingest
from sendtox3.ingest import Processor, display_title, epub_to_document, matches_title, strip_display_affixes
from sendtox3.models import IngestRequest

from test_web_fakes import epub_title, install_fakes

XHTML11 = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n')


@pytest.fixture
def web(monkeypatch):
    return install_fakes(monkeypatch)


@pytest.fixture
def proc(store, opts, web):
    return Processor(store, opts)


def _epub(chapters: dict[str, str], spine: list[str] | None = None, *, doctype: str = XHTML11,
          raw: bool = False) -> bytes:
    spine = list(chapters) if spine is None else spine
    manifest = "".join(f'<item id="{k}" href="{k}.xhtml" media-type="application/xhtml+xml"/>' for k in chapters)
    refs = "".join(f'<itemref idref="{k}"/>' for k in spine)
    opf = ('<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
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
            z.writestr(f"OEBPS/{k}.xhtml", body if raw else (
                doctype + '<html xmlns="http://www.w3.org/1999/xhtml"><head><title/>'
                f"<style>p {{ margin: 0 }}</style></head><body>{body}</body></html>"))
    return bio.getvalue()


def _texts(doc) -> list[str]:
    return [getattr(b, "text", "").replace(" ", " ") for b in doc.blocks]


# ------------------------------------------------------------- R2-A1: entities


ENTITY_BODY = "<p>Hello&nbsp;world &mdash; dash</p><p>Caf&eacute; &amp; &lt;bar&gt; &hellip; &#8220;q&#x201d;</p>"
ENTITY_TEXTS = ["Hello world — dash", "Café & <bar> … “q”"]


def test_xhtml11_doctype_chapter_keeps_html_named_entities():
    doc = epub_to_document(_epub({"c1": ENTITY_BODY}))
    assert _texts(doc) == ENTITY_TEXTS


def test_entities_survive_without_doctype_and_in_the_big_chapter_walker(monkeypatch):
    assert _texts(epub_to_document(_epub({"c1": ENTITY_BODY}, doctype='<?xml version="1.0"?>\n'))) == ENTITY_TEXTS
    monkeypatch.setattr(ingest, "_EPUB_FAST_CHAPTER_BYTES", 10)  # every chapter takes the cheap walker
    assert _texts(epub_to_document(_epub({"c1": ENTITY_BODY}))) == ENTITY_TEXTS


def test_xhtml_self_closing_tags_still_mean_what_they_say():
    # <title/> and <a id="..."/> are XML: an HTML parser would open them and swallow text.
    body = '<p>Before <a id="page7"/>the anchor.</p><div/><p>Next&nbsp;one</p>'
    assert _texts(epub_to_document(_epub({"c1": body}))) == ["Before the anchor.", "Next one"]


def test_numeric_entities_leaves_xml_and_unknown_names_alone():
    assert ingest._numeric_entities("a&nbsp;b&amp;c&lt;&bogus;&#38;") == "a&#xa0;b&amp;c&lt;&bogus;&#38;"


async def test_uploaded_xhtml11_epub_txt_has_the_entities(proc, store):
    item = await proc.process(IngestRequest(data=_epub({"c1": ENTITY_BODY}), filename="book.epub"))
    txt = store.file_bytes(item.id, "txt").decode().replace(" ", " ")
    assert item.status == "ready" and "Hello world — dash" in txt and "Helloworld" not in txt


# ------------------------------------------------------------- R2-A2: caps


def test_one_long_chapter_is_cut_and_the_next_still_read(monkeypatch):
    monkeypatch.setattr(ingest, "_MAX_EPUB_CHAPTER_BYTES", 3000)
    long = "".join(f"<p>Long chapter paragraph {i} with some words.</p>" for i in range(400))  # ~16 KB
    doc = epub_to_document(_epub({"c1": long, "c2": "<p>Second chapter.</p>"}))
    texts = _texts(doc)
    assert texts[0] == "Long chapter paragraph 0 with some words."
    assert "Long chapter paragraph 399 with some words." not in texts
    assert texts[-1] == "Second chapter."
    assert doc.notes == [ingest._EPUB_NOTE_CHAPTER_CUT]


def test_total_read_cap_stops_the_text_copy(monkeypatch):
    monkeypatch.setattr(ingest, "_MAX_EPUB_READ_BYTES", 2500)
    chapters = {f"c{n}": f"<p>Chapter {n} " + "word " * 100 + "</p>" for n in range(10)}  # ~800 B each
    doc = epub_to_document(_epub(chapters))
    firsts = [t.split(" word")[0] for t in _texts(doc)]
    assert firsts[:2] == ["Chapter 0", "Chapter 1"] and "Chapter 5" not in firsts
    assert doc.notes[-1] == ingest._EPUB_NOTE_STOPS_EARLY


def test_cut_inside_a_multibyte_character_keeps_utf8(monkeypatch):
    body = "<p>" + "é" * 3000 + "</p><p>end</p>"
    for limit in (1001, 1002, 1003):  # cut before, inside and after a 2-byte character
        monkeypatch.setattr(ingest, "_MAX_EPUB_CHAPTER_BYTES", limit)
        text = "".join(_texts(epub_to_document(_epub({"c1": body}))))
        assert text and set(text) == {"é"}, limit


def test_utf8_prefix():
    assert ingest._utf8_prefix(b"ab") == b"ab"
    assert ingest._utf8_prefix("aé".encode()) == "aé".encode()
    assert ingest._utf8_prefix("aé".encode()[:-1]) == b"a"
    assert ingest._utf8_prefix("a€".encode()[:-1]) == b"a"
    assert ingest._utf8_prefix("a😀".encode()[:-2]) == b"a"
    assert ingest._utf8_prefix("a😀".encode()) == "a😀".encode()


def test_big_chapters_use_the_cheap_walker_and_keep_loose_text(monkeypatch):
    calls: list[int] = []
    real = ingest.html_fragment_to_blocks

    def spy(fragment, *, xml=False):
        calls.append(len(fragment))
        return real(fragment, xml=xml)

    monkeypatch.setattr(ingest, "html_fragment_to_blocks", spy)
    monkeypatch.setattr(ingest, "_EPUB_FAST_CHAPTER_BYTES", 600)
    big = ("<div><h2>Chapter 1</h2>It was a dark and stormy night.<br/>Except at occasional intervals."
           "<p>Second <em>paragraph</em>.</p>Tail text.</div><section>Text in a section<span> more</span></section>"
           "<ul><li>one</li><li>two</li></ul>" + "<p>filler text</p>" * 40)
    doc = epub_to_document(_epub({"small": "<p>Small chapter.</p>", "big": big}))
    assert len(calls) == 1  # only the small chapter went through the full converter
    texts = _texts(doc)
    assert texts[:9] == ["Small chapter.", "Chapter 1", "It was a dark and stormy night.",
                         "Except at occasional intervals.", "Second paragraph.", "Tail text.",
                         "Text in a section more", "one", "two"]
    assert type(doc.blocks[1]).__name__ == "Heading"


def test_cheap_walker_is_linear_on_deep_nesting():
    import lxml.html

    # 1500 levels (libxml2 stops at 2048), each with a block to mark: the old walker
    # scanned every element's descendants (quadratic).
    deep = "<div>" * 1500 + "<p>leaf</p>" + "x</div>" * 1500
    root = lxml.html.document_fromstring("<html><body>" + deep + "</body></html>",
                                         parser=lxml.html.HTMLParser(huge_tree=True))
    t0 = time.monotonic()
    blocks = ingest._html_leaf_blocks(root)
    assert time.monotonic() - t0 < 2
    assert blocks[0].text == "leaf" and len(blocks) == 1501 and {b.text for b in blocks[1:]} == {"x"}


def test_a_16_mb_chapter_is_read_only_up_to_the_chapter_cap():
    body = "<p>a</p>" * (16 * 1024 * 1024 // 8)  # ~25 KB zipped
    data = _epub({"c1": body, "c2": "<p>after</p>"})
    t0 = time.monotonic()
    doc = epub_to_document(data)
    assert time.monotonic() - t0 < 60  # was 45 s and 1.2 GB for the whole chapter
    assert len(doc.blocks) <= ingest._MAX_EPUB_CHAPTER_BYTES // 8 + 1
    assert ingest._EPUB_NOTE_CHAPTER_CUT in doc.notes and _texts(doc)[-1] == "after"


def test_damaged_chapter_is_skipped(monkeypatch):
    data = _epub({"c1": "<p>first</p>", "c2": "<p>second</p>"})
    real_open = zipfile.ZipFile.open

    def bad_open(self, name, *a, **kw):
        if str(getattr(name, "filename", name)).endswith("c1.xhtml"):
            raise zipfile.BadZipFile("Bad CRC-32")
        return real_open(self, name, *a, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "open", bad_open)
    assert _texts(epub_to_document(data)) == ["second"]


# ------------------------------------------------------------- R2-A3: headline affixes


@pytest.mark.parametrize("title,site,url,expected", [
    ("Python: the language", "python.org", "https://www.python.org/about/", "Python: the language"),
    ("Python: the language", "Python", "https://www.python.org/about/", "Python: the language"),  # < 4 words
    ("The Verge: Apple announces a new phone today", "The Verge", "", "Apple announces a new phone today"),
    ("Verge: Apple announces a new phone today", "The Verge", "", "Verge: Apple announces a new phone today"),
    ("Story | The Guardian", "The Guardian", "https://www.theguardian.com/x", "Story"),
    ("Story - theguardian.com", "", "https://www.theguardian.com/x", "Story"),
    ("Story — The Guardian", "The Guardian", "", "Story"),
    ("Story · The Guardian", "The Guardian", "", "Story"),
    ("Story | Section | The Guardian", "The Guardian", "", "Story | Section"),
    ("Bun 1.2 - bun.sh", "", "https://bun.sh/blog/bun-v1.2", "Bun 1.2"),
    ("Python (programming language) - Wikipedia", "", "https://en.wikipedia.org/wiki/P", "Python (programming language)"),
    ("A story | Medium", "", "https://medium.com/@x/y", "A story"),
    ("Steak: rare - medium", "", "", "Steak: rare - medium"),
    ("Go | Go", "Go", "", "Go | Go"),  # nothing would remain
    ("Release notes: Python", "Python", "", "Release notes: Python"),  # ": " is no trailing affix
    ("GitHub - psf/requests: HTTP for humans", "GitHub", "https://github.com/psf/requests",
     "psf/requests: HTTP for humans"),
])
def test_strip_display_affixes(title, site, url, expected):
    assert strip_display_affixes(title, site, url) == expected


def test_display_title_keeps_leading_words_and_counter():
    assert display_title("Python: the language", "Python - the language", site="python.org",
                         url="https://python.org/") == "Python: the language"
    assert display_title("Python: the language", "Python - the language (2)", site="python.org") == \
        "Python: the language (2)"


async def test_book_headline_keeps_its_leading_words(proc, store, web):
    web.add("https://www.python.org/about/", "Python: the language", site="python.org")
    item = await proc.process(IngestRequest(url="https://www.python.org/about/"))
    assert epub_title(store.file_bytes(item.id, "epub")) == "Python: the language"
    assert delivery.load_document(store, item.id).title == "Python: the language"


# ------------------------------------------------------------- R2-A5: matches_title


@pytest.mark.parametrize("text,title,url,expected", [
    ("Bun 1.2 | Bun Blog", "Bun 1.2", "https://bun.sh/blog/bun-v1.2", True),  # Safari's title, short page title
    ("Bun 1.2 - Bun Blog", "Bun 1.2", "", True),
    ("Bun 1.2 – Bun Blog", "Bun 1.2", "", True),
    ("Bun 1.2 — Bun Blog", "Bun 1.2", "", True),
    ("Bun 1.2 · Bun Blog", "Bun 1.2", "", True),
    ("Bun Blog: Bun 1.2", "Bun 1.2", "", True),
    ("Bun 1.2", "Bun 1.2 | Bun Blog", "", True),
    ("bun 1.2", "Bun 1.2", "", True),
    ("Bun 1.2 bun.sh", "Bun 1.2", "https://bun.sh/blog/bun-v1.2", True),  # title + host
    ("Bun 1.2 Bun", "Bun 1.2", "https://bun.sh/blog/bun-v1.2", True),  # title + site name
    ("Go", "Go", "https://go.dev/", True),
    ("Go | The Go Programming Language", "Go", "https://go.dev/", True),
    ("Go home", "Go", "https://go.dev/", False),
    ("Bun 1.2 | read before the meeting on Monday please", "Bun 1.2", "", False),  # affix of 7 words
    ("Read this before Monday", "Bun 1.2", "https://bun.sh/x", False),
    ("Bun 1.2 is out, finally a fast bundler", "Bun 1.2", "https://bun.sh/x", False),
    ("quiet revolution", "The Quiet Revolution of E-Paper", "", True),
    ("Why Paper Still Wins | Example News", "Why Paper Still Wins", "", True),
    ("buy milk", "Why Paper Still Wins", "", False),
    ("", "x", "", False),
])
def test_matches_title(text, title, url, expected):
    assert matches_title(text, title, url=url) is expected


def test_matches_title_with_site_name():
    assert matches_title("Story The Guardian", "Story", site="The Guardian")
    assert not matches_title("Story tonight", "Story", site="The Guardian")


BUN = "https://bun.sh/blog/bun-v1.2"
HA_BOX = f"Bun 1.2 | Bun Blog\n\n{BUN}"  # ha_share's unedited iOS box: Safari's title + the link


async def test_unedited_share_box_gets_no_comment(proc, store, web):
    web.add(BUN, "Bun 1.2", site="Bun Blog")
    item = await proc.process(IngestRequest(text=HA_BOX, source="ha_share"))
    assert item.kind == "url" and item.title == "Bun 1.2" and item.dedupe_key == ingest.url_dedupe_key(BUN)
    assert not any("Comment" in n for n in delivery.load_document(store, item.id).notes)


async def test_unedited_share_box_dedupes_with_the_plain_share(proc, store, web):
    web.add(BUN, "Bun 1.2")
    plain = await proc.process(IngestRequest(url=BUN))
    again = await proc.process_detailed(IngestRequest(text=HA_BOX, source="ha_share"))
    assert again.duplicate and again.item.id == plain.id and store.count() == 1
    assert web.calls == [BUN]  # the stored title answered it: no second fetch


async def test_plain_share_after_the_box_is_a_duplicate(proc, store, web):
    web.add(BUN, "Bun 1.2")
    first = await proc.process(IngestRequest(text=HA_BOX, source="ha_share"))
    again = await proc.process_detailed(IngestRequest(url=BUN))
    assert again.duplicate and again.item.id == first.id and store.count() == 1


async def test_a_real_comment_next_to_a_short_title_is_kept(proc, store, web):
    web.add(BUN, "Bun 1.2")
    item = await proc.process(IngestRequest(text=f"Read before the meeting\n\n{BUN}", source="ha_share"))
    assert "Comment: Read before the meeting" in delivery.load_document(store, item.id).notes


# ------------------------------------------------------------- R2-A6: cancellable PDF

FAKE_PDF = b"%PDF-1.7\n fake, never parsed by the fake workers below"
SLEEPER = ("import os, sys, time\n"
           "open(sys.argv[1], 'a').write(f'{os.getpid()}\\n')\n"
           "sys.stdin.buffer.read()\n"
           "time.sleep(60)\n")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def _until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


async def test_pdf_upload_awaits_the_cancellable_converter_with_the_remaining_budget(proc, monkeypatch):
    seen: dict = {}

    async def fake_async(data, *, title_hint="", source_url="", ocr=False, timeout=None):
        seen.update(title_hint=title_hint, ocr=ocr, timeout=timeout)
        from sendtox3.textmodel import Document, Paragraph

        return Document(title="Scanned", blocks=[Paragraph("converted text " * 5)])

    monkeypatch.setattr(ingest, "pdf_to_document_async", fake_async)
    item = await proc.process(IngestRequest(data=FAKE_PDF, filename="scan.pdf"))
    assert item.kind == "pdf" and item.status == "ready"
    budget = proc.budget_for(IngestRequest())
    assert budget - ingest.PDF_BUDGET_MARGIN_SECONDS - 2 <= seen["timeout"] <= budget - ingest.PDF_BUDGET_MARGIN_SECONDS
    assert seen["title_hint"] == "scan" and seen["ocr"] is False


async def test_budget_expiry_kills_the_pdf_worker_and_says_converting(store, opts, web, monkeypatch, tmp_path):
    pytest.importorskip("pypdfium2")
    from sendtox3.extract import pdf

    monkeypatch.setattr(ingest, "pdf_to_document", ingest._DEFAULT_PDF_SEAMS[0])  # undo the test fake
    monkeypatch.setattr(ingest, "PDF_BUDGET_MARGIN_SECONDS", -60.0)  # so the share's budget runs out first
    pid_file = tmp_path / "pids"
    monkeypatch.setattr(pdf, "_command", lambda o: [sys.executable, "-c", SLEEPER, str(pid_file)])
    proc = Processor(store, opts)
    proc.background_budget = 1.0
    t0 = time.monotonic()
    item = await proc.process(IngestRequest(data=FAKE_PDF, filename="scan.pdf"))
    assert time.monotonic() - t0 < 10
    assert item.status == "failed" and item.kind == "pdf"
    assert item.error == "Timed out after 1 s while converting the PDF"
    pids = [int(x) for x in Path(pid_file).read_text().split()] if pid_file.exists() else []
    assert len(pids) == 1
    assert await _until(lambda: not _alive(pids[0])), "the PDF worker outlived the share's budget"
    assert pdf._slots.free == pdf.MAX_CONCURRENT


async def test_timeout_message_names_the_phase(proc, monkeypatch):
    async def slow(url, *, opts, title_hint=""):
        await asyncio.sleep(5)

    monkeypatch.setattr(ingest, "url_to_document", slow)
    proc.background_budget = 0.2
    item = await proc.process(IngestRequest(url="https://example.com/slow"))
    assert item.error == "Timed out after 0.2 s while fetching the page"

    def slow_render(doc, item_id):  # the page was fetched; storing it is what ran out of time
        time.sleep(0.5)
        raise AssertionError("not reached in time")

    monkeypatch.setattr(ingest, "url_to_document", lambda url, **kw: _ready(url))
    monkeypatch.setattr(Processor, "_render_pair", staticmethod(slow_render))
    item = await proc.process(IngestRequest(url="https://example.com/fetched"))
    assert item.error == "Timed out after 0.2 s while processing the share"


async def _ready(url):
    from sendtox3.textmodel import Document, Paragraph

    return Document(title="Fetched", blocks=[Paragraph("text " * 20)], source_url=url)
