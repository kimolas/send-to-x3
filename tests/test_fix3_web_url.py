"""Round 3, item 4: a request's ``url`` field without a scheme ("example.com/article")
was stored as a text note. When the url field (not free text) is a host name with a
public-looking top-level domain, ingest now adds https:// and treats it as a link;
free text is classified as before.
"""

from __future__ import annotations

import pytest

from sendtox3.ingest import Processor, _web_url
from sendtox3.models import IngestRequest
from sendtox3.web.app import create_app

from test_web_fakes import install_fakes

AUTH = {"Authorization": "Bearer ingest-test-token"}


@pytest.mark.parametrize(
    "url, expected",
    [
        ("example.com/article", "https://example.com/article"),
        ("www.example.com", "https://www.example.com"),
        ("example.com", "https://example.com"),
        ("  Example.COM/Path?id=1#top  ", "https://Example.COM/Path?id=1#top"),
        ("news.bbc.co.uk/2/hi", "https://news.bbc.co.uk/2/hi"),
        ("bun.sh", "https://bun.sh"),
        ("example.com:8080/x", "https://example.com:8080/x"),
        ("xn--bcher-kva.example/buch", "https://xn--bcher-kva.example/buch"),
        ("blog.example.io/2026/10/post.html", "https://blog.example.io/2026/10/post.html"),
        # unchanged
        ("https://example.com/a", "https://example.com/a"),
        ("http://example.com", "http://example.com"),
        ("www.example.com/a", "https://www.example.com/a"),
        # not a web link
        ("mailto:bob@example.com", ""),
        ("bob@example.com", ""),
        ("tel:+15551234", ""),
        ("notes.txt", ""),
        ("report.PDF", ""),
        ("index.html", ""),
        ("localhost:8080/x", ""),
        ("192.168.1.10/admin", ""),
        ("v1.2", ""),
        ("e.g", ""),
        ("just some words", ""),
        ("example.com/a b", ""),
        ("-bad-.com", ""),
        ("", ""),
    ],
)
def test_web_url(url, expected):
    assert _web_url(url) == expected


@pytest.fixture
def web(monkeypatch):
    w = install_fakes(monkeypatch)
    w.add("https://example.com/article", "A fine article")
    return w


@pytest.fixture
def proc(store, opts, web):
    return Processor(store, opts)


@pytest.mark.parametrize(
    "url, fetched",
    [
        ("example.com/article", "https://example.com/article"),
        ("www.example.com", "https://www.example.com"),
        ("example.com", "https://example.com"),
    ],
)
async def test_schemeless_url_field_is_a_link(proc, web, url, fetched):
    item = await proc.process(IngestRequest(url=url))
    assert item.kind == "url" and item.status == "ready"
    assert web.calls == [fetched]
    assert item.source_url == fetched


async def test_schemeless_url_field_dedupes_with_the_full_link(proc, web):
    first = await proc.process(IngestRequest(url="https://example.com/article"))
    again = await proc.process_detailed(IngestRequest(url="example.com/article"))
    assert again.duplicate and again.item.id == first.id
    assert web.calls == ["https://example.com/article"]


async def test_schemeless_url_field_with_a_selection_cites_the_page(proc, web):
    text = "A quote from the page that is long enough to be a selection, and not the page's title at all."
    item = await proc.process(IngestRequest(url="example.com/article", text=text))
    assert item.kind == "text" and item.source_url == "https://example.com/article"


@pytest.mark.parametrize("url", ["notes.txt", "mailto:bob@example.com", "localhost:8080/x"])
async def test_non_web_url_field_is_still_a_note(proc, web, store, url):
    item = await proc.process(IngestRequest(url=url))
    assert item.kind == "text" and web.calls == []
    assert url in store.file_bytes(item.id, "txt").decode("utf-8")


async def test_free_text_classification_is_unchanged(proc, web):
    """A bare host name in the *text* is not a link (only http(s) and www. are)."""
    item = await proc.process(IngestRequest(text="example.com/article"))
    assert item.kind == "text" and web.calls == []
    item = await proc.process(IngestRequest(text="Have a look at example.com/article for the details, please."))
    assert item.kind == "text" and web.calls == []


async def test_http_api_url_without_scheme(aiohttp_client, opts, store, web):
    client = await aiohttp_client(create_app(opts, store, Processor(store, opts)))
    resp = await client.post("/api/v1/ingest", json={"url": "example.com/article"}, headers=AUTH)
    data = await resp.json()
    assert resp.status == 200 and data["ok"] is True, data
    assert data["title"] == "A fine article"
    item = store.get(data["id"])
    assert item.kind == "url" and item.source_url == "https://example.com/article"
