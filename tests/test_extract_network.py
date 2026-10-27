"""Sanity checks against real public sites (skipped unless SENDTOX3_NETWORK_TESTS=1).

These use the production SSRF policy (``allow_private_urls=False``) and real DNS, so
they also exercise the guarded resolver end to end.
"""

from __future__ import annotations

import dataclasses

import pytest

from sendtox3.extract.fetch import decode_html, fetch_url
from sendtox3.extract.html import html_to_document, url_to_document
from sendtox3.textmodel import Heading, Preformatted

pytestmark = pytest.mark.network


@pytest.fixture
def net_opts(opts):
    return dataclasses.replace(opts, allow_private_urls=False, fetch_timeout_seconds=25)


async def test_wikipedia_article_via_api(net_opts):
    doc = await url_to_document("https://en.wikipedia.org/wiki/Electronic_paper?utm_source=test", opts=net_opts)
    assert doc.site == "Wikipedia" and doc.title == "Electronic paper"
    assert doc.word_count() > 1500
    assert sum(isinstance(b, Heading) for b in doc.blocks) >= 5
    text = doc.plain_text()
    assert "[edit]" not in text and "displaystyle" not in text and "Authority control" not in text


async def test_wikipedia_page_html_extraction():
    res = await fetch_url("https://en.wikipedia.org/wiki/Electronic_paper")
    assert res.status == 200 and res.blocked is None and res.kind == "html"
    doc = html_to_document(decode_html(res.body, res.charset), url=res.url)
    assert doc.title == "Electronic paper" and doc.site == "Wikipedia" and doc.byline == ""
    text = doc.plain_text()
    assert "[edit]" not in text and "Authority control" not in text and "citation needed" not in text
    assert doc.word_count() > 1500


async def test_github_readme_via_api(net_opts):
    doc = await url_to_document("https://github.com/python/cpython", opts=net_opts)
    assert doc.site == "GitHub" and doc.byline == "python"
    assert sum(isinstance(b, Heading) for b in doc.blocks) >= 3
    assert any(isinstance(b, Preformatted) for b in doc.blocks)
    assert doc.word_count() > 300


async def test_simple_page(net_opts):
    doc = await url_to_document("https://example.com/", opts=net_opts)
    assert doc.title == "Example Domain" and doc.blocks


@pytest.mark.parametrize("host", ["127.0.0.1.nip.io", "localtest.me"])
async def test_public_name_resolving_to_loopback_is_refused(net_opts, host):
    # Public DNS names whose A record is 127.0.0.1: refused after resolution. (A router
    # with DNS-rebinding protection may instead refuse to resolve them at all.)
    doc = await url_to_document(f"http://{host}:8765/device/v1/catalog", opts=net_opts)
    assert doc.blocks == []
    assert "private network address" in doc.notes[0] or "Could not find the server" in doc.notes[0]
