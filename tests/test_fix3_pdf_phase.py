"""Round 3, item 3: a link that turns out to be a PDF is converted inside
extract/html.py; when the share's budget ran out there, ingest's timeout message said
"while fetching the page". ``url_to_document`` now reports the step to an optional
``on_phase`` callback, and ingest's ``_Context.phase`` follows it, so the message says
"Timed out after N s while converting the PDF".

The page fetch is faked (no network); the PDF converter is a slow fake (or, in the last
test, the real worker running a sleeper instead of the converter).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from sendtox3 import ingest
from sendtox3.extract import fetch as fetch_mod
from sendtox3.extract import html as html_mod
from sendtox3.extract.fetch import FetchResult
from sendtox3.ingest import Processor
from sendtox3.models import IngestRequest
from sendtox3.textmodel import Document, Paragraph

PDF_URL = "https://papers.example.org/paper.pdf"
PAGE_URL = "https://news.example.org/story"
FAKE_PDF = b"%PDF-1.7\n fake, never parsed by the fake converters below"
PAGE = ("<html><head><title>A story</title></head><body><article>"
        + "<p>Some words about e-paper and reading on small screens for a long time.</p>" * 12
        + "</article></body></html>").encode()


@pytest.fixture
def fake_net(monkeypatch):
    async def fake_fetch(url, **kw):
        if url.endswith(".pdf"):
            return FetchResult(url=url, status=200, content_type="application/pdf", charset=None, body=FAKE_PDF)
        return FetchResult(url=url, status=200, content_type="text/html", charset="utf-8", body=PAGE)

    monkeypatch.setattr(fetch_mod, "fetch_url", fake_fetch)


@pytest.fixture
def converter(monkeypatch):
    """Replace the linked-PDF converter; ``state["delay"]`` makes it slow,
    ``state["fail"]`` makes it raise."""
    pdf = pytest.importorskip("sendtox3.extract.pdf")
    state: dict = {"delay": 0.0, "fail": False, "calls": 0}

    async def fake_async(data, *, title_hint="", source_url="", ocr=False, timeout=None):
        state["calls"] += 1
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        if state["fail"]:
            raise RuntimeError("broken PDF")
        return Document(title=title_hint or "Paper", blocks=[Paragraph("converted pdf text " * 20)])

    monkeypatch.setattr(pdf, "pdf_to_document_async", fake_async)
    return state


# ---------------------------------------------------------------- url_to_document


async def test_pdf_link_reports_converting_then_fetching(opts, fake_net, converter):
    phases: list[str] = []
    doc = await html_mod.url_to_document(PDF_URL, opts=opts, on_phase=phases.append)
    assert doc.blocks and converter["calls"] == 1
    assert phases == [html_mod.PHASE_CONVERTING_PDF, html_mod.PHASE_FETCHING]
    assert phases == ["converting the PDF", "fetching the page"]


async def test_failed_pdf_conversion_also_reports_the_end(opts, fake_net, converter):
    converter["fail"] = True
    phases: list[str] = []
    doc = await html_mod.url_to_document(PDF_URL, opts=opts, title_hint="Paper", on_phase=phases.append)
    assert doc.notes == ["This link is a PDF that could not be read."]
    assert phases == ["converting the PDF", "fetching the page"]


async def test_cancelled_pdf_conversion_leaves_the_phase_at_converting(opts, fake_net, converter):
    converter["delay"] = 30
    phases: list[str] = []
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(html_mod.url_to_document(PDF_URL, opts=opts, on_phase=phases.append), 0.3)
    assert phases == ["converting the PDF"]


async def test_html_page_reports_no_phase_change(opts, fake_net, converter):
    phases: list[str] = []
    doc = await html_mod.url_to_document(PAGE_URL, opts=opts, on_phase=phases.append)
    assert doc.title == "A story" and phases == [] and converter["calls"] == 0


async def test_on_phase_is_optional(opts, fake_net, converter):
    doc = await html_mod.url_to_document(PDF_URL, opts=opts)
    assert doc.blocks


# ----------------------------------------------------------------------- ingest


async def test_budget_expiry_while_converting_a_linked_pdf_says_so(store, opts, fake_net, converter):
    converter["delay"] = 30
    proc = Processor(store, opts)
    proc.background_budget = 0.5
    t0 = time.monotonic()
    item = await proc.process(IngestRequest(url=PDF_URL))
    assert time.monotonic() - t0 < 5
    assert item.status == "failed" and item.kind == "url"
    assert item.error == "Timed out after 0.5 s while converting the PDF"


async def test_budget_expiry_while_fetching_still_says_fetching(store, opts, monkeypatch, converter):
    async def slow_fetch(url, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(fetch_mod, "fetch_url", slow_fetch)
    proc = Processor(store, opts)
    proc.background_budget = 0.5
    item = await proc.process(IngestRequest(url=PDF_URL))
    assert item.error == "Timed out after 0.5 s while fetching the page"


async def test_phase_is_cleared_after_the_link_is_fetched(store, opts, fake_net, converter, monkeypatch):
    """A converted PDF link that then runs out of time while being stored says
    "processing the share", not "converting the PDF"."""
    def slow_render(doc, item_id):
        time.sleep(1.0)
        raise AssertionError("not reached in time")

    monkeypatch.setattr(Processor, "_render_pair", staticmethod(slow_render))
    proc = Processor(store, opts)
    proc.background_budget = 0.5
    item = await proc.process(IngestRequest(url=PDF_URL))
    assert converter["calls"] == 1
    assert item.error == "Timed out after 0.5 s while processing the share"


async def test_substitute_without_on_phase_still_works(store, opts, monkeypatch):
    seen: dict = {}

    async def old_style(url, *, opts, title_hint=""):
        seen["url"] = url
        return Document(title="Old style", blocks=[Paragraph("text " * 30)], source_url=url)

    monkeypatch.setattr(ingest, "url_to_document", old_style)
    item = await Processor(store, opts).process(IngestRequest(url=PAGE_URL))
    assert item.status == "ready" and item.title == "Old style" and seen["url"] == PAGE_URL


SLEEPER = (
    "import os, sys, time\n"
    "open(sys.argv[1], 'a').write(f'{os.getpid()}\\n')\n"
    "sys.stdin.buffer.read()\n"
    "time.sleep(60)\n"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def test_real_worker_is_killed_and_the_message_names_the_pdf(store, opts, fake_net, monkeypatch, tmp_path):
    pytest.importorskip("pypdfium2")
    from sendtox3.extract import pdf

    if not hasattr(pdf, "_command"):
        pytest.skip("PDF worker internals changed")
    pid_file = tmp_path / "pids"
    monkeypatch.setattr(pdf, "_command", lambda o: [sys.executable, "-c", SLEEPER, str(pid_file)])
    proc = Processor(store, opts)
    proc.background_budget = 1.0
    item = await proc.process(IngestRequest(url=PDF_URL))
    assert item.error == "Timed out after 1 s while converting the PDF"
    pids = [int(x) for x in Path(pid_file).read_text().split()] if pid_file.exists() else []
    assert len(pids) == 1, "the worker was never started"
    deadline = time.monotonic() + 5
    while _alive(pids[0]) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not _alive(pids[0]), "the PDF worker outlived the share's budget"
    assert pdf._slots.free == pdf.MAX_CONCURRENT
