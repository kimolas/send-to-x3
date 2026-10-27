"""OPDS 1.2 feed for CrossPoint 1.6.5: auth, XML validity, limits, paging, downloads.

``test_feed_parses_in_crosspoint_opds_parser`` runs the feed through CrossPoint 1.6.5's
real OpdsParser (host build); it needs ``SENDTOX3_FIRMWARE_TESTS=1`` and the harness
binary (``SENDTOX3_OPDS_HARNESS``, default ``$FWH_WORK/opds/opds_check``, built by
``e2e/fwharness/opds/build.sh``).
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
import subprocess
from pathlib import Path

import pytest
from lxml import etree

from sendtox3.ingest import Processor
from sendtox3.web import opds
from sendtox3.web.app import create_app

from test_web_fakes import add_item, install_fakes

NS = {"a": "http://www.w3.org/2005/Atom"}
TOKEN = "device-test-token"


def basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


AUTH = basic("x3", TOKEN)


@pytest.fixture
async def client(aiohttp_client, opts, store, monkeypatch):
    install_fakes(monkeypatch)
    return await aiohttp_client(create_app(opts, store, Processor(store, opts)))


async def _feed(client, query: str = ""):
    resp = await client.get("/opds" + query, headers=AUTH)
    body = await resp.read()
    assert resp.status == 200, body
    assert resp.headers["Content-Length"] == str(len(body))
    assert resp.headers["Connection"].lower() == "close"
    assert resp.headers["Content-Type"].startswith("application/atom+xml;profile=opds-catalog;kind=acquisition")
    return body, etree.fromstring(body)


# ----------------------------------------------------------------------------- auth


@pytest.mark.parametrize("headers", [{}, basic("x3", "wrong"), basic("x3", ""), {"Authorization": "Basic !!!"},
                                     {"Authorization": "Bearer nope"}])
async def test_auth_required(client, headers):
    resp = await client.get("/opds", headers=headers)
    body = await resp.read()
    assert resp.status == 401 and len(body) < 300
    assert resp.headers["WWW-Authenticate"].startswith("Basic ")
    assert resp.headers["Content-Length"] == str(len(body))


@pytest.mark.parametrize("headers", [basic("x3", TOKEN), basic("anyone", TOKEN), basic("", TOKEN),
                                     {"Authorization": f"Bearer {TOKEN}"}])
async def test_auth_accepted(client, headers):
    assert (await client.get("/opds", headers=headers)).status == 200


async def test_download_needs_auth(client, store):
    it = add_item(store, "Secret")
    assert (await client.get(f"/opds/items/{it.id}.epub")).status == 401
    assert store.get(it.id).is_pending


# ------------------------------------------------------------------------------ feed


async def test_feed_structure(client, store):
    a = add_item(store, "Older", created_at=1_761_000_000, source_url="https://example.com/a")
    b = add_item(store, "Fish & Chips 'n' more", created_at=1_761_000_100, kind="email")
    body, root = await _feed(client)
    assert body.startswith(b"<?xml") and b"&nbsp;" not in body
    assert root.tag == "{http://www.w3.org/2005/Atom}feed"
    fetch, *entries = root.findall("a:entry", NS)
    assert fetch.findtext("a:title", namespaces=NS).startswith("Get 2 new · ")
    assert fetch.find("a:author", NS) is None  # the file name is just the title
    fetch_links = fetch.findall("a:link", NS)
    assert len(fetch_links) == 1 and fetch_links[0].get("type") == "application/epub+zip"
    assert re.fullmatch(r"/opds/items/all-[a-z2-7]{10}\.epub", fetch_links[0].get("href"))
    assert [e.findtext("a:title", namespaces=NS) for e in entries] == ["Fish & Chips 'n' more", "Older"]
    assert b"Fish &amp; Chips" in body
    first = entries[0]
    assert first.findtext("a:id", namespaces=NS) == f"urn:sendtox3:{b.id}"
    assert first.findtext("a:updated", namespaces=NS) == "2025-10-20T22:41:40Z"
    links = first.findall("a:link", NS)
    assert len(links) == 1
    assert links[0].get("rel") == "http://opds-spec.org/acquisition"
    assert links[0].get("type") == "application/epub+zip"
    assert links[0].get("href") == f"/opds/items/{b.id}.epub"
    assert entries[1].findtext("a:author/a:name", namespaces=NS) == "example.com"
    assert root.find("a:link[@rel='next']", NS) is None and root.find("a:link[@rel='previous']", NS) is None


async def test_all_hrefs_absolute_or_root_relative(client, store):
    for i in range(30):
        add_item(store, f"Item {i}", created_at=1000 + i)
    for query in ("", "?page=2"):
        _, root = await _feed(client, query)
        for link in root.iter("{http://www.w3.org/2005/Atom}link"):
            href = link.get("href")
            assert href.startswith("/") and not href.startswith("//") and "#" not in href and len(href) < 768


async def test_titles_single_line_within_limits(client, store):
    long_title = "A very long title " * 20
    add_item(store, long_title.strip())
    add_item(store, "  padded  ")
    _, root = await _feed(client)
    for e in root.findall("a:entry", NS):
        t = e.findtext("a:title", namespaces=NS)
        assert t == t.strip() and "\n" not in t and len(t.encode()) <= 120


async def test_author_omitted_when_filename_would_cut_title(client, store):
    title = "T" * 90
    add_item(store, title, source_url="https://a-rather-long-hostname.example.com/x")
    _, root = await _feed(client)
    entry = root.find("a:entry", NS)
    assert entry.find("a:author", NS) is None


async def test_paging_limits_and_links(client, store):
    n = opds.PAGE_SIZE * 2 + 3
    items = [add_item(store, f"Item {i:03d}", created_at=1000 + i) for i in range(n)]
    newest_first = [it.id for it in reversed(items)]
    assert opds.PAGE_SIZE <= 50
    _, p1 = await _feed(client)
    ids1 = [e.findtext("a:id", namespaces=NS).split(":")[-1] for e in p1.findall("a:entry", NS)]
    assert ids1[0].startswith("all-")  # "Get N new" leads page 1 only
    assert ids1[1:] == newest_first[: opds.PAGE_SIZE]
    assert p1.find("a:link[@rel='next']", NS).get("href") == "/opds?page=2"
    assert p1.find("a:link[@rel='previous']", NS) is None
    _, p2 = await _feed(client, "?page=2")
    assert p2.find("a:link[@rel='previous']", NS).get("href") == "/opds"
    assert p2.find("a:link[@rel='next']", NS).get("href") == "/opds?page=3"
    _, p3 = await _feed(client, "?page=3")
    ids3 = [e.findtext("a:id", namespaces=NS).split(":")[-1] for e in p3.findall("a:entry", NS)]
    assert ids3 == newest_first[2 * opds.PAGE_SIZE:]
    assert p3.find("a:link[@rel='next']", NS) is None
    assert p3.find("a:link[@rel='previous']", NS).get("href") == "/opds?page=2"


async def test_empty_and_out_of_range_pages(client, store):
    _, root = await _feed(client)
    assert root.findall("a:entry", NS) == []
    _, root = await _feed(client, "?page=abc")
    assert root.find("a:link[@rel='previous']", NS) is None
    _, root = await _feed(client, "?page=9")
    assert root.findall("a:entry", NS) == [] and root.find("a:link[@rel='previous']", NS) is not None


async def test_trailing_slash_alias(client):
    resp = await client.get("/opds/", headers=AUTH, allow_redirects=False)
    assert resp.status == 200


async def test_download_marks_opds(client, store):
    it = add_item(store, "Book")
    resp = await client.get(f"/opds/items/{it.id}.epub", headers=AUTH)
    body = await resp.read()
    assert resp.status == 200 and resp.headers["Content-Type"] == "application/epub+zip"
    assert body == store.file_bytes(it.id, "epub")
    assert resp.headers["Content-Length"] == str(len(body)) and "Content-Encoding" not in resp.headers
    assert "opds" in store.get(it.id).delivered
    assert (await client.get("/opds/items/abcdefghij.epub", headers=AUTH)).status == 404


def test_build_feed_escapes_and_strips_control_chars(store):
    it = add_item(store, "Ctrl\x01char & \"quotes\" 'single'")
    xml = opds.build_feed([store.get(it.id)], page=1, has_next=False)
    root = etree.fromstring(xml)
    assert "\x01" not in root.findtext("a:entry/a:title", namespaces=NS)


# ------------------------------------------------------------------- real firmware

def _default_harness() -> str:
    work = os.environ.get("FWH_WORK") or os.path.join(tempfile.gettempdir(), "sendtox3-fwharness")
    return os.path.join(work, "opds", "opds_check")


HARNESS = Path(os.environ.get("SENDTOX3_OPDS_HARNESS") or _default_harness())


@pytest.mark.skipif(os.environ.get("SENDTOX3_FIRMWARE_TESTS") != "1",
                    reason="set SENDTOX3_FIRMWARE_TESTS=1 to run against CrossPoint's OpdsParser")
async def test_feed_parses_in_crosspoint_opds_parser(client, store, tmp_path):
    if not HARNESS.exists():
        if os.environ.get("SENDTOX3_OPDS_HARNESS"):
            pytest.fail(f"SENDTOX3_OPDS_HARNESS={HARNESS} does not exist (build: e2e/fwharness/opds/build.sh)")
        pytest.skip(f"OPDS harness not built at {HARNESS} (build: e2e/fwharness/opds/build.sh)")
    titles = ["Plain title", "Ampersand & <angle> \"quotes\"", "Café — naïve façade … ellipsis", "T" * 95]
    for i in range(opds.PAGE_SIZE + 2):
        add_item(store, titles[i % len(titles)] + (f" ({i})" if i >= len(titles) else ""), created_at=1000 + i,
                 source_url="https://example.com/x" if i % 2 else "")
    server = "http://192.168.1.50:8765/opds"
    for query, current in (("", ""), ("?page=2", "/opds?page=2")):
        body, root = await _feed(client, query)
        feed = tmp_path / f"feed{query.replace('?', '_')}.xml"
        feed.write_bytes(body)
        for chunk in ("2048", "7"):  # the device's read size, and a nasty small one
            out = subprocess.run([str(HARNESS), str(feed), server, current, chunk], capture_output=True,
                                 text=True, timeout=30)
            assert out.returncode == 0, out.stdout + out.stderr
            assert "RESULT: OK" in out.stdout, out.stdout
            books = [ln for ln in out.stdout.splitlines() if ln.startswith("[BOOK]")]
            gets = [ln.strip()[4:] for ln in out.stdout.splitlines() if ln.strip().startswith("GET ")]
            entries = root.findall("a:entry", NS)
            assert len(books) == len(entries)
            for e, url in zip(entries, gets):
                item_id = e.findtext("a:id", namespaces=NS).split(":")[-1]
                assert url == f"http://192.168.1.50:8765/opds/items/{item_id}.epub"
            for e, line in zip(entries, books):
                assert f'title="{e.findtext("a:title", namespaces=NS)}"' in line
        if query == "":
            assert "Next Page" in out.stdout and "next=/opds?page=2" in out.stdout
        else:
            assert "Previous Page" in out.stdout
