"""End-to-end flows through the real extractors and renderers (no fakes).

Share via the HTTP API -> catalog -> device download -> sleep auto-delivery -> OPDS,
against a local fixture web server. Skipped until the extract/render modules exist.
The fixture article is synthetic (written for these tests; CC0).
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import tempfile
import subprocess
import zipfile
from pathlib import Path

import pytest
from aiohttp import web
from lxml import etree

for _mod in ("sendtox3.extract.fetch", "sendtox3.extract.html", "sendtox3.extract.text",
             "sendtox3.render.names", "sendtox3.render.txt", "sendtox3.render.epub"):
    pytest.importorskip(_mod)

from sendtox3.ingest import Processor  # noqa: E402
from sendtox3.web.app import create_app  # noqa: E402

INGEST = {"Authorization": "Bearer ingest-test-token"}
DEVICE = {"Authorization": "Bearer device-test-token"}
OPDS = {"Authorization": "Basic " + base64.b64encode(b"x3:device-test-token").decode()}

PARAGRAPHS = [
    "Electronic paper does not glow. It reflects the light around it, the way a printed page does, "
    "and that single property explains most of what people like about reading on it for hours.",
    "Each pixel holds tiny charged pigment particles suspended in fluid. A voltage pulls the black or the "
    "white particles toward the surface, and once they are there they stay without any further power.",
    "That is why an e-reader can show the same page for weeks on one charge. Power is spent only when the "
    "picture changes, which also explains the brief flash you sometimes see as the screen clears ghosting.",
    "Refresh speed is the price of this design. Moving particles through a fluid takes far longer than "
    "switching a liquid crystal, so animation and fast scrolling are poor, while a page of text is perfect.",
    "Designers of reading software therefore favour simple layouts with generous margins, real paragraphs "
    "and justified text, and they avoid anything that needs constant redrawing of large parts of the screen.",
]
ARTICLE = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>The Quiet Craft of Electronic Paper | Fixture Daily</title>"
    "<meta name=\"author\" content=\"Ada Fixture\"><meta property=\"og:site_name\" content=\"Fixture Daily\">"
    "</head><body><nav><a href=\"/\">Home</a> <a href=\"/about\">About</a></nav><article>"
    "<h1>The Quiet Craft of Electronic Paper</h1><p class=\"byline\">By Ada Fixture</p>"
    + "".join(f"<p>{p}</p>" for p in PARAGRAPHS[:3])
    + "<h2>Why it is slow</h2>"
    + "".join(f"<p>{p}</p>" for p in PARAGRAPHS[3:])
    + "</article><footer>Copyright nobody; written for tests.</footer></body></html>"
)


@pytest.fixture
async def site(aiohttp_server):
    async def article(request):
        return web.Response(text=ARTICLE, content_type="text/html", charset="utf-8")

    async def slow(request):
        await asyncio.sleep(5)
        return web.Response(text=ARTICLE, content_type="text/html")

    async def missing(request):
        return web.Response(status=404, text="<html><title>Not found</title><p>Nope</p></html>",
                            content_type="text/html")

    app = web.Application()
    app.router.add_get("/article", article)
    app.router.add_get("/slow", slow)
    app.router.add_get("/missing", missing)
    return await aiohttp_server(app)


@pytest.fixture
def processor(store, opts):
    return Processor(store, opts)


@pytest.fixture
async def client(serve_like_main, opts, store, processor):
    return await serve_like_main(create_app(opts, store, processor))


def validate_epub(data: bytes) -> dict[str, bytes]:
    """Container rules from §6.2 plus well-formedness of every XML part."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip" and not infos[0].extra
        container = etree.fromstring(z.read("META-INF/container.xml"))
        rootfile = container.xpath("//*[local-name()='rootfile']")[0]
        assert rootfile.get("media-type") == "application/oebps-package+xml"
        parts = {i.filename: z.read(i.filename) for i in infos}
    for name, raw in parts.items():
        if name.endswith((".xml", ".opf", ".xhtml", ".ncx")):
            etree.fromstring(raw)  # strict XML, as CrossPoint's expat
            assert b"&nbsp;" not in raw
    return parts


def opf_title(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        container = etree.fromstring(z.read("META-INF/container.xml"))
        path = container.xpath("//*[local-name()='rootfile']/@full-path")[0]
        opf = etree.fromstring(z.read(path))
    return opf.xpath("//*[local-name()='title']/text()")[0]


async def _ingest(client, **kw):
    kw["headers"] = {**INGEST, **kw.get("headers", {})}
    resp = await client.post("/api/v1/ingest", **kw)
    data = await resp.json()
    assert resp.status == 200 and data["ok"], data
    return data


async def test_share_catalog_deliver_opds(client, site, store, opts):
    opts.auto_delivery = True  # the sleep delivery part below
    url = str(site.make_url("/article"))
    shared = await _ingest(client, json={"url": url})
    assert shared["status"] == "ready", shared
    assert "Electronic Paper" in shared["title"] and "Fixture Daily" not in shared["title"]
    item = store.get(shared["id"])
    assert item.kind == "url" and item.word_count > 150
    epub = store.file_bytes(item.id, "epub")
    validate_epub(epub)
    assert opf_title(epub) == item.title
    txt = store.file_bytes(item.id, "txt").decode("utf-8")
    assert txt.startswith(item.title) and "Why it is slow".upper() in txt and "Home" not in txt.split("\n")[0]

    note = await _ingest(client, data="Remember: buy more paper.\n\nAnd return the library book.".encode(),
                         headers={**INGEST, "Content-Type": "text/plain; charset=utf-8"})
    assert note["status"] == "ready" and store.get(note["id"]).kind == "text"

    # Catalog: "Get 2 new" first, then newest first, mapped fields only, firmware paging
    # (limit = page_size + 1).
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9", headers=DEVICE)).json())["items"]
    assert rows[0]["id"].startswith("all-") and rows[0]["title"].startswith("Get 2 new · ")
    assert [r["id"] for r in rows[1:]] == [note["id"], shared["id"]]
    assert rows[2]["subtitle"].startswith("New · ") and "127.0.0.1" in rows[2]["subtitle"]

    # Sleep delivery: one digest of both, oldest first; replay is byte-identical; then 404.
    deliver = "/device/v1/deliver?ev=0a1b2c3d-1&ts=1761400000"
    r1 = await client.get(deliver, headers=DEVICE)
    digest = await r1.read()
    assert r1.status == 200 and r1.headers["Content-Length"] == str(len(digest))
    parts = validate_epub(digest)
    assert opf_title(digest).startswith("Inbox: 2 items (")
    assert len([n for n in parts if n.startswith("OEBPS/text/")]) == 2
    assert await (await client.get(deliver, headers=DEVICE)).read() == digest
    assert (await client.get("/device/v1/deliver?ev=0a1b2c3d-2&ts=1761400500", headers=DEVICE)).status == 404
    assert all("auto" in store.get(i).delivered for i in (shared["id"], note["id"]))

    # Catalog download of a delivered item still works and says it was read on the X3.
    dl = await client.get(f"/device/v1/items/{shared['id']}.epub", headers=DEVICE)
    assert dl.status == 200 and await dl.read() == epub
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9", headers=DEVICE)).json())["items"]
    assert rows[2]["subtitle"].startswith("Read on X3 ")
    # Nothing new: the first row says so, and fetching it is a 404 that writes nothing.
    assert rows[0]["id"] == "none" and rows[0]["title"] == "Nothing new right now"
    assert (await client.get("/device/v1/items/none.epub", headers=DEVICE)).status == 404

    # OPDS for 1.6.5.
    feed = await client.get("/opds", headers=OPDS)
    root = etree.fromstring(await feed.read())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    assert [e.findtext("a:title", namespaces=ns) for e in root.findall("a:entry", ns)] == \
        [store.get(note["id"]).title, item.title]
    got = await client.get(f"/opds/items/{item.id}.epub", headers=OPDS)
    assert await got.read() == epub and "opds" in store.get(item.id).delivered

    # Something new again: "Get 1 new" in the catalog and as the first OPDS entry.
    later = await _ingest(client, data=b"One more note to read later.", headers={**INGEST, "Content-Type": "text/plain"})
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9", headers=DEVICE)).json())["items"]
    assert rows[0]["title"].startswith("Get 1 new · ") and rows[1]["id"] == later["id"]
    root = etree.fromstring(await (await client.get("/opds", headers=OPDS)).read())
    first = root.findall("a:entry", ns)[0]
    assert first.findtext("a:title", namespaces=ns) == rows[0]["title"]
    href = first.find("a:link", ns).get("href")
    assert href == f"/opds/items/{rows[0]['id']}.epub"
    got = await client.get(href, headers=OPDS)
    assert got.status == 200 and await got.read() == store.file_bytes(later["id"], "epub")
    for _ in range(150):  # marked once the client's TCP stack acknowledged every byte
        if "fetch" in store.get(later["id"]).delivered:
            break
        await asyncio.sleep(0.02)
    assert "fetch" in store.get(later["id"]).delivered

    if os.environ.get("SENDTOX3_FIRMWARE_TESTS") == "1":
        harness = Path(os.environ.get("SENDTOX3_OPDS_HARNESS") or os.path.join(
            os.environ.get("FWH_WORK") or os.path.join(tempfile.gettempdir(), "sendtox3-fwharness"), "opds", "opds_check"))
        if harness.exists():
            path = Path(store.dir) / "feed.xml"
            path.write_bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8"))
            out = subprocess.run([str(harness), str(path), "http://192.168.1.50:8765/opds"], capture_output=True,
                                 text=True, timeout=30)
            # The latest feed: "Get 1 new" (a book to the firmware too) plus the three items.
            assert "RESULT: OK (4 rows)" in out.stdout, out.stdout
            assert f'[BOOK] title="{rows[0]["title"]}" author=""' in out.stdout, out.stdout


async def test_duplicate_share_returns_existing(client, site):
    url = str(site.make_url("/article"))
    a = await _ingest(client, json={"url": url})
    b = await _ingest(client, json={"text": f"The quiet craft {url}?utm_source=share"})
    assert b["duplicate"] is True and b["id"] == a["id"]


async def test_url_plus_title_text_is_a_link_share(client, site, store):
    url = str(site.make_url("/article"))
    data = await _ingest(client, data={"text": f"The Quiet Craft of Electronic Paper - Fixture Daily\n{url}"})
    assert store.get(data["id"]).kind == "url" and store.get(data["id"]).word_count > 150


async def test_unreachable_and_missing_pages_are_kept(client, site, store):
    data = await _ingest(client, json={"url": str(site.make_url("/missing"))})
    item = store.get(data["id"])
    assert item.is_pending and item.source_url.endswith("/missing")
    validate_epub(store.file_bytes(item.id, "epub"))
    dead = await _ingest(client, json={"url": "http://127.0.0.1:9/nothing-listens-here"})
    assert store.get(dead["id"]).is_pending
    assert "127.0.0.1:9/nothing-listens-here" in store.file_bytes(dead["id"], "txt").decode()


async def test_timeout_budget_with_real_fetcher(client, site, store, processor):
    processor.background_budget = 1.0
    data = await _ingest(client, json={"url": str(site.make_url("/slow"))})
    assert data["status"] == "failed" and "Timed out" in data["error"]
    assert str(site.make_url("/slow")) in store.file_bytes(data["id"], "txt").decode()
    validate_epub(store.file_bytes(data["id"], "epub"))


async def test_unsafe_title_is_firmware_safe(client, store):
    data = await _ingest(client, json={"text": "Body text here.", "title": "Wait... what? {url} a/b ~1"})
    title = data["title"]
    assert ".." not in title and "{" not in title and "/" not in title and "~1" not in title
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9", headers=DEVICE)).json())["items"]
    assert rows[1]["title"] == title and rows[0]["title"].startswith("Get 1 new · ")


async def test_email_requests_become_items(processor, store):
    email_mod = pytest.importorskip("sendtox3.extract.email")
    raw = (b"From: Alice Example <alice@example.com>\r\nTo: me+x3@example.com\r\nSubject: Fwd: Notes for later\r\n"
           b"Message-ID: <fixture-1@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
           b"Here are the notes I promised. They are long enough to be a proper text item for reading.\r\n\r\n"
           b"Second paragraph with more words.\r\n")
    parsed = email_mod.parse_email(raw)
    items = await processor.process_many(parsed.requests)
    assert items and all(i.source == "email" for i in items)
    assert items[0].kind == "email" and items[0].status == "ready"
    validate_epub(store.file_bytes(items[0].id, "epub"))
