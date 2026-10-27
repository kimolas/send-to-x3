"""Round-2 regression tests for extract/html.py (R2-B1 to R2-B4).

B1: the mailing-list chrome filter deleted ordinary sentences of short emails ("Have
    you received this invoice yet?"), because every block of a short email is in the
    first-3 / last-8 window and the patterns matched anywhere in a block.
B2: tag patterns that stop at "<" lost a <meta>/<link>/<title>/<html> tag whose quoted
    attribute value holds "<" (content="Why 1 < 2 matters").
B3: the attribute regex (and the <html lang> one) were quadratic on crafted heads.
B4: a linked PDF was converted in a thread, which a cancelled budget cannot stop.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from aiohttp import web

import sendtox3
from sendtox3.extract import html as html_mod
from sendtox3.extract.html import html_to_document, url_to_document
from sendtox3.ingest import Processor
from sendtox3.models import IngestRequest
from sendtox3.textmodel import Document, Paragraph

# --------------------------------------------------------------------------------------
# B1: mailing-list chrome only in strict forms, only in long emails
# --------------------------------------------------------------------------------------

# The verifier's examples: ordinary sentences that the old filter deleted.
ORDINARY = [
    "Have you received this invoice yet? It is due Friday.",
    "We no longer want to receive paper statements, please switch us to email.",
    "Please update your subscription by Friday so the renewal goes through.",
    "Here is how to manage your subscriptions in the app settings.",
    "Can you open this in your browser to see the chart?",
    "View this email in your browser to see the chart.",
    "You received this invoice because you ordered a lamp.",
    "Can you unsubscribe me from the club list? It sends three emails a day.",
]

# Real newsletter footers / headers (Mailchimp, Substack, Campaign Monitor, Buttondown).
FOOTERS = [
    "View this email in your browser",
    "Having trouble viewing this email? View it in your browser.",
    "Email not displaying correctly? View it in your browser.",
    "Can't see this email? View it online.",
    "You are receiving this email because you opted in via our website.",
    "You're receiving this email because you subscribed to The Quiet Letter.",
    "You received this email because you signed up for our newsletter.",
    "Want to change how you receive these emails? You can update your preferences or unsubscribe from this list.",
    "unsubscribe from this list | update subscription preferences",
    "Unsubscribe | Manage preferences",
    "View in browser · Unsubscribe",
    "Manage your email preferences",
    "Click here to unsubscribe.",
    "Unsubscribe",
]

BODY = [f"Paragraph {i} of this issue looks at e-paper displays and long reading sessions." for i in range(16)]


def _html(paras: list[str]) -> str:
    return "<html><body>" + "".join(f"<p>{p}</p>" for p in paras) + "</body></html>"


@pytest.mark.parametrize("sentence", ORDINARY)
def test_short_email_keeps_every_sentence(sentence):
    doc = html_to_document(_html(["Hi Bob,", sentence, "Thanks, Ann"]), whole=True)
    assert doc.plain_text() == f"Hi Bob,\n\n{sentence}\n\nThanks, Ann"


def test_verifiers_invoice_email_is_kept_whole():
    doc = html_to_document(
        _html(["Hi Bob,", "Have you received this invoice yet? It is due Friday.", "Thanks, Ann"]), whole=True
    )
    assert "Have you received this invoice yet? It is due Friday." in doc.plain_text()


def test_long_email_keeps_ordinary_sentences_at_its_edges():
    """Even where chrome is looked for (first 3 / last 8 blocks of a long email), a
    sentence that only mentions receiving, subscriptions or a browser stays."""
    paras = ORDINARY[:3] + BODY + ORDINARY[3:]
    assert len(paras) > html_mod._MAIL_CHROME_MIN_BLOCKS
    text = html_to_document(_html(paras), whole=True).plain_text()
    for sentence in ORDINARY:
        assert sentence in text, sentence


@pytest.mark.parametrize("footer", FOOTERS)
def test_long_newsletter_footer_is_stripped(footer):
    text = html_to_document(_html(BODY + [footer]), whole=True).plain_text()
    assert footer not in text
    assert all(p in text for p in BODY)


@pytest.mark.parametrize("header", FOOTERS[:4])
def test_long_newsletter_header_is_stripped(header):
    text = html_to_document(_html([header] + BODY), whole=True).plain_text()
    assert header not in text and BODY[0] in text


def test_mailchimp_footer_blocks_are_stripped_together():
    footer = [
        "Copyright © 2026 The Quiet Letter, All rights reserved.",
        "You are receiving this email because you opted in via our website.",
        "Want to change how you receive these emails?<br>You can <a href='https://x.example/p'>update your "
        "preferences</a> or <a href='https://x.example/u'>unsubscribe from this list</a>.",
    ]
    text = html_to_document(_html(["View this email in your browser"] + BODY + footer), whole=True).plain_text()
    assert "View this email" not in text and "receiving this email" not in text and "unsubscribe" not in text
    assert "Copyright © 2026 The Quiet Letter" in text  # not chrome by the strict rules: kept


def test_short_newsletter_keeps_its_footer():
    """Fewer than 13 blocks: nothing is stripped (the edge windows would cover it all)."""
    paras = BODY[:4] + ["Unsubscribe"]
    assert html_to_document(_html(paras), whole=True).plain_text().endswith("Unsubscribe")


def test_chrome_like_block_over_25_words_is_kept():
    long_footer = (
        "You are receiving this email because you signed up at our shop, and we think you will like the new "
        "lamps, rugs, chairs and tables that arrive in our stores next week."
    )
    assert len(long_footer.split()) > 25
    assert long_footer in html_to_document(_html(BODY + [long_footer]), whole=True).plain_text()


def test_chrome_only_in_the_edge_windows():
    middle = BODY[:8] + ["Unsubscribe"] + BODY[8:]
    assert "Unsubscribe" in html_to_document(_html(middle), whole=True).plain_text()


async def test_invoice_email_reaches_the_txt_through_the_processor(store, opts):
    """End to end from ingest (email source, so ``whole=True``) to the stored TXT."""
    html = _html(["Hi Bob,", "Have you received this invoice yet? It is due Friday.", "Thanks, Ann"])
    req = IngestRequest(kind="html", source="email", html=html, title="Invoice", sender="Ann <ann@example.com>")
    item = await Processor(store, opts).process(req)
    assert item.status == "ready"
    txt = store.file_bytes(item.id, "txt").decode("utf-8")
    assert "Have you received this invoice yet? It is due Friday." in txt


# --------------------------------------------------------------------------------------
# B2: quoted attribute values may hold "<"
# --------------------------------------------------------------------------------------


def test_meta_with_lt_inside_a_quoted_value():
    html = '<html><head><meta property="og:title" content="Why 1 < 2 matters"></head><body></body></html>'
    assert html_mod._meta_tags(html) == {"og:title": "Why 1 < 2 matters"}


def test_meta_with_gt_and_lt_in_single_quotes():
    html = "<head><meta name='description' content='a > b and c < d'><meta name=author content=Ann></head>"
    assert html_mod._meta_tags(html) == {"description": "a > b and c < d", "author": "Ann"}


def test_link_with_lt_inside_a_quoted_value():
    html = '<head><link title="1 < 2" rel="canonical" href="https://other.example/a"></head>'
    assert html_mod._canonical_link(html) == "https://other.example/a"


def test_title_and_html_tags_with_lt_inside_a_quoted_value():
    html = '<html data-note="a<b" lang="fr-CA"><head><title data-x="1 < 2">Le vrai titre</title></head></html>'
    assert html_mod._title_tag(html) == "Le vrai titre"
    assert html_mod._html_lang(html) == "fr"


def test_unclosed_meta_does_not_swallow_the_next_one():
    html = '<head><meta name="a" <meta name="b" content="c"></head>'
    assert html_mod._meta_tags(html) == {"b": "c"}


@pytest.mark.parametrize(
    "tag, lang",
    [
        ('<html lang="en">', "en"),
        ("<html lang=de>", "de"),
        ('<html xml:lang="ja" xmlns="http://www.w3.org/1999/xhtml">', "ja"),
        ("<html\n  class='no-js'\n  lang='pt_BR'>", "pt"),
        ('<html data-lang="fr">', ""),  # not the lang attribute
        ("<html>", ""),
    ],
)
def test_html_lang(tag, lang):
    assert html_mod._html_lang(tag + "<head></head><body></body></html>") == lang


def test_og_title_with_lt_reaches_the_document():
    html = (
        '<html><head><meta property="og:title" content="Why 1 < 2 matters"><meta property="og:site_name" '
        'content="Numbers"></head><body><p>' + "Counting is older than writing. " * 40 + "</p></body></html>"
    )
    assert html_to_document(html, url="https://numbers.example/why").title == "Why 1 < 2 matters"


# --------------------------------------------------------------------------------------
# B3: linear time on crafted heads (300 KB)
# --------------------------------------------------------------------------------------

_N = 300_000
# "</head>" keeps the crafted tag inside the scanned head (without it only 300 KB are read)
_ADVERSARIAL = {
    "meta attribute-name run": "<head><meta " + "a" * _N + "></head>",
    "link attribute-name run": "<head><link " + "a" * _N + "></head>",
    "html attribute-name run": "<html " + "a" * (_N // 20) + " lang=en>",  # <html> is read in the first 20 KB
    "whitespace before =": "<head><meta a" + " " * _N + "></head>",
    "many a=": "<head><meta " + "a=" * (_N // 2) + "></head>",
    'many a="': "<head><meta " + 'a="' * (_N // 3) + "></head>",
    "unclosed metas": "<head>" + "<meta " * (_N // 6),
    "unclosed metas with quotes": "<head>" + "<meta '\"'\"" * (_N // 10),
    "unclosed quote then metas": '<head><meta content="' + "<meta name=a content=b>" * (_N // 23),
    "unclosed html tags": "<html " * (_N // 6),
    "html lang whitespace": "<html lang" + " " * _N,
    "unclosed titles": "<title" * (_N // 6),
}

_TIMER = r"""
import json, sys, time
from sendtox3.extract import html as h
cases = json.loads(sys.stdin.read())
out = {}
for name, s in cases.items():
    t = time.perf_counter()
    h._meta_tags(s); h._canonical_link(s); h._html_lang(s); h._title_tag(s)
    helpers = time.perf_counter() - t
    t = time.perf_counter()
    h.html_to_document(s, url="https://example.com/x")
    out[name] = [helpers, time.perf_counter() - t]
print(json.dumps(out))
"""


def test_crafted_heads_are_parsed_in_linear_time():
    """Run in a child process with a hard timeout: a quadratic regex cannot be
    interrupted in-process (the old code needed minutes for these inputs)."""
    pkg_root = str(Path(sendtox3.__file__).resolve().parents[1])
    env = dict(os.environ, PYTHONPATH=pkg_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.run(
        [sys.executable, "-c", _TIMER], input=json.dumps(_ADVERSARIAL), capture_output=True, text=True,
        timeout=120, env=env,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    timings = json.loads(proc.stdout)
    slow = {name: t for name, t in timings.items() if t[0] >= 0.5 or t[1] >= 2.0}
    assert not slow, slow


@pytest.mark.parametrize("tag", ["meta", "link"])
def test_long_attribute_name_run_in_process(tag):
    """40k letters took 10 s before (kept small so a regression fails in seconds)."""
    html = f"<head><{tag} " + "a" * 40_000 + "></head>"
    t0 = time.perf_counter()
    html_mod._meta_tags(html)
    html_mod._canonical_link(html)
    assert time.perf_counter() - t0 < 0.5


def test_html_lang_on_many_unclosed_html_tags():
    """The old lazy <html ...lang> pattern rescanned the first 20 KB for every "<html"
    (about 0.5 s); now one pass."""
    t0 = time.perf_counter()
    assert html_mod._html_lang("<html " * 3_400) == ""
    assert time.perf_counter() - t0 < 0.1


# --------------------------------------------------------------------------------------
# B4: linked PDFs go through the cancellable async converter
# --------------------------------------------------------------------------------------

FAKE_PDF = b"%PDF-1.7\n fake, never parsed by the fake workers below"


@pytest.fixture
async def pdf_site(aiohttp_server):
    async def paper(request):
        return web.Response(body=FAKE_PDF, content_type="application/pdf")

    app = web.Application()
    app.router.add_get("/paper.pdf", paper)
    srv = await aiohttp_server(app, host="127.0.0.1")
    return str(srv.make_url("/paper.pdf"))


@pytest.fixture
def fake_async_pdf(monkeypatch):
    calls: list[dict] = []
    mod = types.ModuleType("sendtox3.extract.pdf")

    def blocking(*a, **kw):
        raise AssertionError("the blocking pdf_to_document must not be used from url_to_document")

    async def pdf_to_document_async(data: bytes, **kw) -> Document:
        calls.append(dict(kw, data=data))
        return Document(title=kw.get("title_hint") or "PDF", blocks=[Paragraph("pdf text")])

    mod.pdf_to_document = blocking
    mod.pdf_to_document_async = pdf_to_document_async
    monkeypatch.setitem(sys.modules, "sendtox3.extract.pdf", mod)
    return calls


@pytest.mark.parametrize("ocr, timeout", [(False, 120.0), (True, 600.0)])
async def test_linked_pdf_uses_the_async_converter_with_a_timeout(pdf_site, opts, fake_async_pdf, ocr, timeout):
    doc = await url_to_document(pdf_site, opts=dataclasses.replace(opts, pdf_ocr=ocr), title_hint="Paper")
    assert doc.blocks == [Paragraph("pdf text")]
    [call] = fake_async_pdf
    assert call == {"data": FAKE_PDF, "title_hint": "Paper", "source_url": pdf_site, "ocr": ocr, "timeout": timeout}


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


async def test_cancelled_link_kills_the_pdf_worker_and_frees_its_slot(pdf_site, opts, monkeypatch, tmp_path):
    """The caller's budget (ingest's wait_for) running out must stop the conversion,
    not leave it running and holding a converter slot until its own timeout."""
    pdf = pytest.importorskip("sendtox3.extract.pdf")
    if not hasattr(pdf, "_command"):
        pytest.skip("PDF worker internals changed")
    pid_file = tmp_path / "pids"
    monkeypatch.setattr(pdf, "_command", lambda o: [sys.executable, "-c", SLEEPER, str(pid_file)])

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(url_to_document(pdf_site, opts=opts), 2.0)
    pids = [int(x) for x in pid_file.read_text().split()] if pid_file.exists() else []
    assert len(pids) == 1, "the worker was never started"
    deadline = time.monotonic() + 5
    while _alive(pids[0]) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not _alive(pids[0]), "the cancelled PDF worker is still running"
    assert pdf._slots.free == pdf.MAX_CONCURRENT
