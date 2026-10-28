"""Device API for the CrossPoint 2.x plugin: auth, paging, downloads, deliver framing."""

from __future__ import annotations

import asyncio

import pytest

from sendtox3.delivery import PAGE_SIZE
from sendtox3.ingest import Processor
from sendtox3.web.app import create_app

from test_web_fakes import add_item, epub_title, install_fakes

AUTH = {"Authorization": "Bearer device-test-token"}


@pytest.fixture
def fakes(monkeypatch):
    return install_fakes(monkeypatch)


@pytest.fixture
async def client(aiohttp_client, opts, store, fakes):
    return await aiohttp_client(create_app(opts, store, Processor(store, opts)))


def _assert_device_framing(resp, body: bytes) -> None:
    assert resp.headers["Content-Length"] == str(len(body))
    assert "Transfer-Encoding" not in resp.headers
    assert "Content-Encoding" not in resp.headers
    assert resp.headers.get("Connection", "").lower() == "close"
    assert "ETag" not in resp.headers and "Last-Modified" not in resp.headers


@pytest.mark.parametrize("path", ["/device/v1/catalog?page=1&limit=9", "/device/v1/deliver?ev=a-1&ts=1",
                                  "/device/v1/items/abcdefghij.epub"])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer "},
                                     {"Authorization": "Basic eDM6ZGV2aWNlLXRlc3QtdG9rZW4="}])
async def test_bad_or_missing_token_is_401_with_small_json(client, path, headers):
    resp = await client.get(path, headers=headers)
    body = await resp.read()
    assert resp.status == 401
    assert resp.headers["Content-Type"].startswith("application/json")
    assert len(body) < 200 and b'"ok":false' in body
    _assert_device_framing(resp, body)


async def test_query_token_is_accepted(client, store):
    add_item(store, "Q")
    resp = await client.get("/device/v1/catalog?page=1&limit=9&token=device-test-token")
    assert resp.status == 200


async def test_catalog_paging_nine_items(client, store):
    items = [add_item(store, f"Item {i}", created_at=1000 + i) for i in range(9)]
    resp = await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)
    body = await resp.read()
    assert resp.status == 200 and resp.headers["Content-Type"] == "application/json; charset=utf-8"
    _assert_device_framing(resp, body)
    rows = (await resp.json())["items"]
    assert rows[0]["id"].startswith("all-")  # "Get 9 new"; with it the device shows 8 rows + Next Page
    assert [r["id"] for r in rows[1:]] == [it.id for it in reversed(items)][:8]
    page2 = (await (await client.get("/device/v1/catalog?page=2&limit=9", headers=AUTH)).json())["items"]
    assert [r["id"] for r in page2] == [items[1].id, items[0].id]


async def test_catalog_paging_eight_items(client, store):
    items = [add_item(store, f"Item {i}", created_at=1000 + i) for i in range(8)]
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)).json())["items"]
    assert len(rows) == 9 and [r["id"] for r in rows[1:]] == [it.id for it in reversed(items)]
    page2 = (await (await client.get("/device/v1/catalog?page=2&limit=9", headers=AUTH)).json())["items"]
    assert [r["id"] for r in page2] == [items[0].id]


async def test_catalog_bad_params_use_defaults(client, store):
    for i in range(12):
        add_item(store, f"Item {i}", created_at=1000 + i)
    rows = (await (await client.get("/device/v1/catalog?page=x&limit={limit}", headers=AUTH)).json())["items"]
    assert len(rows) == 9


# What each CrossPoint build sends for the browse URL and when it shows Next Page
# (docs/ARCHITECTURE.md §7.2; PluginCatalogPaging.h and PluginCatalogActivity::substituted):
# - "2.0.0rc" (and develop 3f387a9): {page}, {limit} = page_size + 1, {offset} left as the
#   literal text; Next Page after a lookahead row.
# - "develop" (8f4d035 and the Send to X3 firmware) with this device.json: {offset} =
#   (page - 1) * page_size, {limit} = page_size + 1; Next Page after a lookahead row.
# - "develop-0.2.0-plugin": the same firmware with a device.json from 0.2.0 (no {offset}):
#   {limit} = page_size, no lookahead row; Next Page after a full page.
OLD_BROWSE_URL = "http://h/device/v1/catalog?page={page}&limit={limit}"
FIRMWARES = ("2.0.0rc", "develop", "develop-0.2.0-plugin")


def _browse_template(firmware: str, opts) -> tuple[str, int]:
    from urllib.parse import urlsplit

    from sendtox3.plugin_bundle import build_device_json

    browse = build_device_json(base_url="http://h", device_token="device-test-token", opts=opts)["browse"]
    url = OLD_BROWSE_URL if firmware == "develop-0.2.0-plugin" else browse["url"]
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}", browse["page_size"]


async def _browse_like(firmware: str, client, opts, max_pages: int = 10) -> list[list[str]]:
    """The row ids the X3 shows on each page, following Next Page to the end."""
    from yarl import URL

    template, size = _browse_template(firmware, opts)
    pages: list[list[str]] = []
    for page in range(1, max_pages + 1):
        lookahead = firmware == "2.0.0rc" or "{offset}" in template
        url = template.replace("{page}", str(page)).replace("{limit}", str(size + 1 if lookahead else size))
        if firmware != "2.0.0rc":
            url = url.replace("{offset}", str((page - 1) * size))
        # Sent byte for byte, so 2.0.0rc's "offset={offset}" reaches the app unencoded.
        resp = await client.session.get(URL(str(client.make_url("/")).rstrip("/") + url, encoded=True),
                                        headers=AUTH)
        assert resp.status == 200, url
        rows = (await resp.json())["items"]
        pages.append([r["id"] for r in rows[:size]])
        if not (len(rows) > size if lookahead else len(rows) >= size):
            return pages
    raise AssertionError(f"{firmware}: more than {max_pages} pages")


@pytest.mark.parametrize("firmware", FIRMWARES)
@pytest.mark.parametrize("n", [0, 7, 8, 9, 15, 20])
async def test_catalog_pages_on_every_firmware(client, store, opts, firmware, n):
    """Every row exactly once, in order, the first row on page 1 only, on every firmware;
    the only flaw left is develop with a 0.2.0 plugin: an empty last page when the rows
    fill whole pages (reinstalling the plugin removes it)."""
    items = [add_item(store, f"Item {i}", created_at=1000 + i) for i in range(n)]
    pages = await _browse_like(firmware, client, opts)
    shown = [row for page in pages for row in page]
    assert shown[1:] == [it.id for it in reversed(items)], pages  # none missing, none twice
    assert shown[0] == pages[0][0] and (shown[0].startswith("all-") if n else shown[0] == "none")
    assert not any(r.startswith("all-") or r == "none" for r in shown[1:])
    rows = n + 1  # the first row and the items
    if firmware == "develop-0.2.0-plugin" and rows % PAGE_SIZE == 0:
        assert len(pages) == rows // PAGE_SIZE + 1 and pages[-1] == [], pages
    else:
        assert all(pages) and len(pages) == -(-rows // PAGE_SIZE), pages


@pytest.mark.parametrize("offset", ["{offset}", "%7Boffset%7D", "", "-8", "8.0", "٨", "x"])
async def test_catalog_offset_must_be_ascii_digits(client, store, offset):
    """Anything but plain digits (2.0.0rc's literal "{offset}", a sign, an Arabic-Indic
    digit) is ignored: the page and limit decide."""
    items = [add_item(store, f"Item {i}", created_at=1000 + i) for i in range(12)]
    rows = (await (await client.get(f"/device/v1/catalog?page=2&limit=9&offset={offset}", headers=AUTH)).json())
    assert [r["id"] for r in rows["items"]] == [it.id for it in reversed(items)][7:]


async def test_catalog_offset_wins_and_huge_values_are_empty(client, store):
    items = [add_item(store, f"Item {i}", created_at=1000 + i) for i in range(12)]
    newest = [it.id for it in reversed(items)]
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9&offset=8", headers=AUTH)).json())["items"]
    assert [r["id"] for r in rows] == newest[7:]  # the offset counts the first row
    rows = (await (await client.get("/device/v1/catalog?page=3&limit=9&offset=0", headers=AUTH)).json())["items"]
    assert rows[0]["id"].startswith("all-") and [r["id"] for r in rows[1:]] == newest[:8]
    # 4301 digits and more: int() refuses them (ValueError), which used to be a 500.
    for big in ("9" * 19, "9" * 20, "9" * 40, str(1 << 63), "9" * 4300, "9" * 4301, "9" * 5000):
        resp = await client.get(f"/device/v1/catalog?page=1&limit=9&offset={big}", headers=AUTH)
        assert resp.status == 200 and (await resp.json()) == {"items": []}, len(big)
    resp = await client.get(f"/device/v1/catalog?page={'9' * 40}&limit=9", headers=AUTH)
    assert resp.status == 200 and (await resp.json()) == {"items": []}


@pytest.mark.parametrize("query,old", [
    ("page=1&limit=8", True),  # develop c3b16da+ (the Send to X3 firmware too) with a 0.2.0 device.json
    ("page=2&limit=8", True),
    ("page=1&limit=9&offset=0", False),  # the same firmware with this version's device.json
    ("page=2&limit=9&offset=8", False),
    ("page=1&limit=9&offset={offset}", False),  # 2.0.0rc with this version's device.json
    ("page=1&limit=9", False),  # 2.0.0rc with a 0.2.0 device.json: nothing to tell from it
    ("page=1&limit=8&offset=0", False),
])
async def test_catalog_warns_about_a_plugin_from_0_2_0(client, store, app_state_from, query, old):
    from yarl import URL

    from sendtox3.web.device_api import OLD_PLUGIN_WARNING

    for i in range(12):
        add_item(store, f"Item {i}", created_at=1000 + i)
    resp = await client.session.get(URL(str(client.make_url("/device/v1/catalog")) + "?" + query, encoded=True),
                                    headers=AUTH)
    assert resp.status == 200 and (await resp.json())["items"]
    st = app_state_from(client).channel_status("device_api")
    if old:
        assert st["state"] == "warning" and st["detail"] == OLD_PLUGIN_WARNING
        assert "install it again from Setup" in st["detail"] and "Open" in st["detail"]
    else:
        assert st["state"] == "ok" and "catalog page" in st["detail"]


async def test_old_plugin_warning_stays_until_the_new_device_json_is_seen(client, store, app_state_from):
    """The download that follows a catalog page must not hide the warning."""
    from sendtox3.web.device_api import OLD_PLUGIN_WARNING

    it = add_item(store, "Pick me")
    status = lambda: app_state_from(client).channel_status("device_api")  # noqa: E731
    await client.get("/device/v1/catalog?page=1&limit=8", headers=AUTH)
    assert (await client.get(f"/device/v1/items/{it.id}.epub?confirm=1", headers=AUTH)).status == 200
    st = status()
    assert st["state"] == "warning" and st["detail"].startswith(OLD_PLUGIN_WARNING) and "Pick me" in st["detail"]
    await client.get("/device/v1/catalog?page=1&limit=9&offset=0", headers=AUTH)  # reinstalled
    assert status()["state"] == "ok"
    assert (await client.get(f"/device/v1/items/{it.id}.epub?confirm=1", headers=AUTH)).status == 200
    st = status()
    assert st["state"] == "ok" and "Pick me" in st["detail"]


async def test_catalog_empty(client):
    resp = await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)
    assert resp.status == 200 and await resp.json() == {"items": [{
        "id": "none", "title": "Nothing new right now",
        "subtitle": "Share something to Send to X3, then open this list again."}]}


@pytest.mark.parametrize("fmt,ctype", [("epub", "application/epub+zip"), ("txt", "text/plain; charset=utf-8")])
async def test_item_download_marks_catalog(client, store, fmt, ctype):
    it = add_item(store, "Download me")
    resp = await client.get(f"/device/v1/items/{it.id}.{fmt}", headers={**AUTH, "Accept-Encoding": "gzip"})
    body = await resp.read()
    assert resp.status == 200 and resp.headers["Content-Type"] == ctype
    assert body == store.file_bytes(it.id, fmt) and len(body) > 0
    _assert_device_framing(resp, body)
    assert "catalog" in store.get(it.id).delivered


async def test_item_download_unknown(client):
    resp = await client.get("/device/v1/items/abcdefghij.epub", headers=AUTH)
    assert resp.status == 404 and (await resp.json())["ok"] is False
    resp = await client.get("/device/v1/items/NOT-AN-ID.epub", headers=AUTH)
    assert resp.status == 404
    resp = await client.get("/device/v1/items/abcdefghij.pdf", headers=AUTH)
    assert resp.status == 404


async def test_deliver_nothing_pending_is_404_with_body(client):
    resp = await client.get("/device/v1/deliver?ev=0a1b2c3d-1&ts=1761400000", headers=AUTH)
    body = await resp.read()
    assert resp.status == 404 and body and len(body) < 100
    _assert_device_framing(resp, body)


async def test_deliver_and_replay_identical_bytes(client, store, opts):
    opts.auto_delivery = True
    a = add_item(store, "First", created_at=1)
    add_item(store, "Second", created_at=2)
    url = "/device/v1/deliver?ev=0a1b2c3d-1&ts=1761400000"
    r1 = await client.get(url, headers=AUTH)
    b1 = await r1.read()
    assert r1.status == 200 and r1.headers["Content-Type"] == "application/epub+zip"
    _assert_device_framing(r1, b1)
    assert epub_title(b1).startswith("Inbox: 2 items")
    assert "auto" in store.get(a.id).delivered
    add_item(store, "Third", created_at=3)
    r2 = await client.get(url, headers=AUTH)
    assert r2.status == 200 and await r2.read() == b1  # same (ev, ts): same bytes
    r3 = await client.get("/device/v1/deliver?ev=0a1b2c3d-1&ts=1761400999", headers=AUTH)
    assert r3.status == 200 and epub_title(await r3.read()) == "Third"
    r4 = await client.get("/device/v1/deliver?ev=0a1b2c3d-2&ts=1761401000", headers=AUTH)
    assert r4.status == 404


async def test_deliver_requires_ev_and_ts(client, store):
    add_item(store, "Pending")
    for qs in ("", "?ev=a", "?ts=1", "?ev=&ts=1", "?ev=a%20b&ts=1"):
        resp = await client.get(f"/device/v1/deliver{qs}", headers=AUTH)
        assert resp.status == 400
    assert store.delivery_stats() == []


async def test_head_does_not_deliver(client, store):
    it = add_item(store, "Pending")
    resp = await client.head("/device/v1/deliver?ev=a-1&ts=1", headers=AUTH)
    assert resp.status == 405
    assert store.get(it.id).is_pending


async def test_no_redirects_for_trailing_slashes(client):
    for path in ("/device/v1/catalog/", "/device/v1/deliver/", "/device/v1"):
        resp = await client.get(path, headers=AUTH, allow_redirects=False)
        assert resp.status == 404
        assert resp.headers["Content-Length"] == str(len(await resp.read()))


async def test_wire_framing_closes_connection(client, store):
    """Talk raw HTTP exactly like the device (keep-alive request) and read to EOF."""
    it = add_item(store, "Wire")
    expected = store.file_bytes(it.id, "epub")
    reader, writer = await asyncio.open_connection(client.server.host, client.server.port)
    writer.write((f"GET /device/v1/items/{it.id}.epub HTTP/1.1\r\nHost: {client.server.host}:{client.server.port}\r\n"
                  "User-Agent: CrossPoint-ESP32-2.0.0\r\nConnection: keep-alive\r\n"
                  "Authorization: Bearer device-test-token\r\n\r\n").encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=5)  # returns only if the server closes
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    assert lines[0] == "HTTP/1.1 200 OK"
    headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    assert headers["content-length"] == str(len(expected)) and body == expected
    assert headers["connection"].lower() == "close"
    assert "transfer-encoding" not in headers and "content-encoding" not in headers


async def test_device_status_is_recorded(client, store, app_state_from):
    await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)
    st = app_state_from(client).channel_status("device_api")
    assert st["state"] == "ok" and "catalog" in st["detail"]


@pytest.fixture
def app_state_from():
    from sendtox3.web.common import STATE_KEY

    return lambda client: client.server.app[STATE_KEY]


async def test_healthz_is_open(client):
    resp = await client.get("/healthz")
    data = await resp.json()
    assert resp.status == 200 and data["ok"] is True and "version" in data
