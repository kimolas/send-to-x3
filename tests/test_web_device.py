"""Device API for the CrossPoint 2.x plugin: auth, paging, downloads, deliver framing."""

from __future__ import annotations

import asyncio

import pytest

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
    assert [r["id"] for r in rows] == [it.id for it in reversed(items)]
    page2 = (await (await client.get("/device/v1/catalog?page=2&limit=9", headers=AUTH)).json())["items"]
    assert [r["id"] for r in page2] == [items[0].id]


async def test_catalog_bad_params_use_defaults(client, store):
    for i in range(12):
        add_item(store, f"Item {i}", created_at=1000 + i)
    rows = (await (await client.get("/device/v1/catalog?page=x&limit={limit}", headers=AUTH)).json())["items"]
    assert len(rows) == 9


async def test_catalog_empty(client):
    resp = await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)
    assert resp.status == 200 and await resp.json() == {"items": []}


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


async def test_deliver_and_replay_identical_bytes(client, store):
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
