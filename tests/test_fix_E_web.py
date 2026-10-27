"""Regression tests for review findings 11, 13, 17, 27/43, 29, 34, 44, 46, 50 and 52 (web layer)."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import markupsafe
import pytest
from lxml import etree

from sendtox3 import plugin_bundle
from sendtox3.ingest import ProcessResult, Processor, StorageError
from sendtox3.models import Item, new_item_id
from sendtox3.web import admin, ingest_api
from sendtox3.web.app import create_app
from sendtox3.web.common import STATE_KEY

from test_web_admin import login
from test_web_fakes import add_item, install_fakes

NS = {"a": "http://www.w3.org/2005/Atom"}
INGEST_AUTH = {"Authorization": "Bearer ingest-test-token"}
DEVICE_AUTH = {"Authorization": "Bearer device-test-token"}
OPDS_AUTH = {"Authorization": "Basic " + base64.b64encode(b"x3:device-test-token").decode()}


@pytest.fixture
def fakes(monkeypatch):
    w = install_fakes(monkeypatch)
    w.add("https://example.com/pasted", "Pasted article")
    return w


@pytest.fixture
def app(opts, store, fakes):
    return create_app(opts, store, Processor(store, opts))


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


# ---------------------------------------------------------------- finding 17


async def test_opds_feed_survives_xml_incompatible_characters(client, store):
    add_item(store, "Newsletter", site="Weekly￿News￾")  # emailed HTML: no source_url, so site is shown
    bad = Item(id=new_item_id(), created_at=int(time.time()), kind="text", source="test",
               title="Bad￿Title", word_count=3)
    store.add_item(bad, epub=b"PK", txt=b"t")
    resp = await client.get("/opds", headers=OPDS_AUTH)
    body = await resp.read()
    assert resp.status == 200, body  # used to be a 500 (lxml: "All strings must be XML compatible")
    feed = etree.fromstring(body)
    assert "Weekly News" in feed.xpath("//a:entry/a:author/a:name/text()", namespaces=NS)
    assert "Bad Title" in feed.xpath("//a:entry/a:title/text()", namespaces=NS)
    assert "￿".encode() not in body and "￾".encode() not in body


# ---------------------------------------------------------------- finding 29


async def test_deliver_is_404_when_auto_delivery_is_off(client, app, store):
    it = add_item(store, "Waiting")
    app[STATE_KEY].opts.auto_delivery = False
    resp = await client.get("/device/v1/deliver?ev=0a1b2c3d-1&ts=1761400000", headers=DEVICE_AUTH)
    body = await resp.read()
    assert resp.status == 404 and body == b"nothing new\n"
    assert resp.headers["Content-Length"] == str(len(body))
    assert store.get(it.id).is_pending and store.delivery_stats() == []  # not marked "auto"
    st = app[STATE_KEY].channel_status("device_api")
    assert st["state"] == "warning" and "reinstall the plugin" in st["detail"]
    app[STATE_KEY].opts.auto_delivery = True
    resp = await client.get("/device/v1/deliver?ev=0a1b2c3d-2&ts=1761400001", headers=DEVICE_AUTH)
    assert resp.status == 200 and "auto" in store.get(it.id).delivered


async def test_deliver_still_requires_the_token_when_auto_delivery_is_off(client, app):
    app[STATE_KEY].opts.auto_delivery = False
    assert (await client.get("/device/v1/deliver?ev=a-1&ts=1")).status == 401


# ---------------------------------------------------------------- finding 44


async def test_login_rejects_large_bodies_without_reading_them(client):
    big = "token=wrong&pad=" + "x" * 5000
    resp = await client.post("/admin/login", data=big,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status == 413  # used to read (up to ~31 MB) and answer 401

    async def chunked():
        yield b"token=admin-test-token"

    resp = await client.post("/admin/login", data=chunked(),
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status == 413  # no Content-Length: refused


async def test_login_form_still_works(client):
    resp = await client.post("/admin/login", data={"token": "nope"})
    assert resp.status == 401 and "not the admin token" in await resp.text()
    resp = await client.post("/admin/login", data={"token": " admin-test-token "})
    assert resp.status == 200 and "Signed in." in await resp.text()
    assert (await client.get("/admin")).status == 200


async def test_login_body_read_has_a_deadline(client, monkeypatch):
    monkeypatch.setattr(admin, "LOGIN_READ_TIMEOUT", 0.1)

    async def slow():
        yield b"token=adm"
        await asyncio.sleep(1)
        yield b"in-test-token"

    resp = await client.post("/admin/login", data=slow(),
                             headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "22"})
    assert resp.status == 400


# ---------------------------------------------------------------- findings 40/50


async def test_bundle_and_install_refuse_a_container_internal_guess(client, app, monkeypatch):
    calls = []

    async def fake_install(host, **kw):
        calls.append(host)
        return ["Done: the plugin is installed."]

    monkeypatch.setattr(plugin_bundle, "install_to_device", fake_install)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")  # only refused under Home Assistant (round 2, R2-E1)
    state = app[STATE_KEY]
    state.base_url, state.base_url_source = "http://172.30.33.5:8765", "local address"
    csrf = await login(client)
    resp = await client.get("/admin/plugin-bundle.zip")
    assert resp.status == 409 and "inside Docker" in await resp.text()
    html = await (await client.post("/admin/install-plugin", data={"csrf": csrf, "device_host": "192.168.1.60"})).text()
    assert "The plugin was not installed." in html and "inside Docker" in html and calls == []
    setup = await (await client.get("/admin/setup")).text()
    assert "cannot be installed yet" in setup
    # The same address typed into server_host is the user's explicit choice (some LANs use 172.16/12).
    state.base_url_source = "options"
    assert (await client.get("/admin/plugin-bundle.zip")).status == 200


# ---------------------------------------------------------------- finding 46


async def test_invalid_server_host_shows_a_specific_setup_error(aiohttp_client, opts, store, fakes):
    opts.server_host = "http://192.168.1.50:8765/opds?x=1"
    app = create_app(opts, store, Processor(store, opts))
    assert app[STATE_KEY].base_url == "" and app[STATE_KEY].base_url_source == plugin_bundle.INVALID_SERVER_HOST
    client = await aiohttp_client(app)
    await login(client)
    html = await (await client.get("/admin/setup")).text()
    assert "is not a usable" in html and "could not be detected" not in html
    resp = await client.get("/admin/plugin-bundle.zip")
    assert resp.status == 409 and "server_host option is not a usable address" in await resp.text()


# ---------------------------------------------------------------- findings 27/43


async def test_setup_explains_that_the_device_token_is_readable_from_the_x3(client):
    await login(client)
    html = await (await client.get("/admin/setup")).text()
    assert "device.json" in html and "File Transfer screen is open any device on your Wi-Fi can read" in html


@pytest.fixture
async def generated_client(aiohttp_client, opts, store, fakes):
    opts.device_token = ""  # generated on first start, so it can be rotated
    app = create_app(opts, store, Processor(store, opts))
    return app, await aiohttp_client(app)


async def test_rotate_generated_device_token(generated_client, store, monkeypatch):
    app, client = generated_client
    state = app[STATE_KEY]
    old = state.opts.device_token
    installs = []

    async def fake_install(host, *, base_url, device_token, opts):
        installs.append((host, device_token))
        return ["Connecting ...", "Done: the plugin is installed."]

    monkeypatch.setattr(plugin_bundle, "install_to_device", fake_install)
    assert (await client.get("/device/v1/catalog", headers={"Authorization": f"Bearer {old}"})).status == 200
    csrf = await login(client)
    assert "New device token" in await (await client.get("/admin/setup")).text()
    assert (await client.post("/admin/rotate-device-token", data={"device_host": "192.168.1.60"})).status == 403
    resp = await client.post("/admin/rotate-device-token", data={"csrf": csrf, "device_host": "192.168.1.60"})
    html = await resp.text()
    new = state.opts.device_token
    assert resp.status == 200 and new != old and new in html and "The plugin on the X3 now uses it." in html
    assert installs == [("192.168.1.60", new)]
    assert store.secret("device_token") == new  # survives a restart
    assert (await client.get("/device/v1/catalog", headers={"Authorization": f"Bearer {old}"})).status == 401
    assert (await client.get("/device/v1/catalog", headers={"Authorization": f"Bearer {new}"})).status == 200

    # Without an X3 address: rotated, and the page says to install again.
    html = await (await client.post("/admin/rotate-device-token", data={"csrf": csrf})).text()
    assert state.opts.device_token != new and "install the plugin again" in html and len(installs) == 1


async def test_configured_device_token_is_not_rotated(client, app):
    csrf = await login(client)
    assert "New device token" not in await (await client.get("/admin/setup")).text()
    resp = await client.post("/admin/rotate-device-token", data={"csrf": csrf})
    assert resp.status == 409 and "set in the app options" in await resp.text()
    assert app[STATE_KEY].opts.device_token == "device-test-token"


# ---------------------------------------------------------------- finding 52


async def test_setup_warns_about_an_unusable_configured_device_token(aiohttp_client, opts, store, fakes):
    opts.device_token = "my x3 secret"
    client = await aiohttp_client(create_app(opts, store, Processor(store, opts)))
    await login(client)
    html = await (await client.get("/admin/setup")).text()
    assert "cannot be used by the X3 plugin" in html


# ---------------------------------------------------------------- finding 11


async def test_setup_page_explains_sleep_delivery_on_2_0_0rc(client, app):
    await login(client)
    html = await (await client.get("/admin/setup")).text()
    assert "every time the X3 goes to sleep" not in html
    assert "2.0.0rc" in html and "facaa87" in html and "next time the X3 joins Wi-Fi" in html


# ---------------------------------------------------------------- finding 34


async def test_setup_offers_android_import_and_no_ios_download(client, app):
    from sendtox3 import phone

    await login(client)
    html = await (await client.get("/admin/setup")).text()
    assert "Download iOS Shortcut" not in html
    assert str(markupsafe.escape(phone.IOS_SHORTCUT_NOTE)) in html
    assert "/admin/setup/android.json" in html
    assert (await client.get("/admin/static/send-to-x3.shortcut")).status == 404


async def test_android_import_download(aiohttp_client, app):
    from sendtox3 import phone

    anon = await aiohttp_client(app)
    assert (await anon.get("/admin/setup/android.json")).status == 401
    await login(anon)
    resp = await anon.get("/admin/setup/android.json")
    body = await resp.read()
    assert resp.status == 200
    assert resp.headers["Content-Disposition"] == 'attachment; filename="send-to-x3-http-shortcuts.json"'
    assert resp.headers["Content-Length"] == str(len(body))
    assert body == phone.android_import_json("http://127.0.0.1:8765", "ingest-test-token")
    json.loads(body)


# ---------------------------------------------------------------- finding 13 (+ decision 5)


class _BrokenStoreProcessor:
    async def process_detailed(self, req):
        raise StorageError("OSError: [Errno 28] No space left on device")

    process_all = None


async def test_ingest_storage_error_is_503_not_ok(aiohttp_client, opts, store):
    app = create_app(opts, store, _BrokenStoreProcessor())
    client = await aiohttp_client(app)
    resp = await client.post("/api/v1/ingest", json={"text": "hello"}, headers=INGEST_AUTH)
    data = json.loads(await resp.read())
    assert resp.status == 503 and data["ok"] is False and "No space left" in data["error"]
    assert "Not saved" in data["error"]
    await login(client)
    html = await (await client.get("/admin")).text()
    assert "Not saved" in html and "free space" in html  # shown on the queue page


async def test_admin_send_storage_error_is_an_error_not_saved(aiohttp_client, opts, store):
    client = await aiohttp_client(create_app(opts, store, _BrokenStoreProcessor()))
    csrf = await login(client)
    resp = await client.post("/admin/send", data={"csrf": csrf, "content": "hello"})
    html = await resp.text()
    assert resp.status == 503 and "Not saved" in html and "flash error" in html


async def test_background_storage_error_is_logged_and_flagged(aiohttp_client, opts, store, monkeypatch, caplog):
    class Slow:
        async def process_detailed(self, req):
            await asyncio.sleep(0.2)
            raise StorageError("database or disk is full")

    monkeypatch.setattr(ingest_api, "RESPONSE_WAIT_SECONDS", 0.02)
    app = create_app(opts, store, Slow())
    client = await aiohttp_client(app)
    resp = await client.post("/api/v1/ingest", json={"text": "hello"}, headers=INGEST_AUTH)
    assert resp.status == 202
    for _ in range(50):
        if app[STATE_KEY].status.get(ingest_api.STORAGE_STATUS):
            break
        await asyncio.sleep(0.02)
    assert "database or disk is full" in app[STATE_KEY].status[ingest_api.STORAGE_STATUS]["detail"]
    assert "could not store a share" in caplog.text


async def test_ingest_reports_every_item_of_a_multi_item_share(aiohttp_client, opts, store):
    def item(title):
        return Item(id=new_item_id(), created_at=1, kind="url", source="http", title=title)

    class Many:
        async def process_all(self, req):
            return [ProcessResult(item("First"), False), ProcessResult(item("Second"), False)]

        async def process_detailed(self, req):  # pragma: no cover - must not be used
            raise AssertionError("process_all is preferred")

    client = await aiohttp_client(create_app(opts, store, Many()))
    resp = await client.post("/api/v1/ingest", json={"text": "https://a.example/1 https://b.example/2"},
                             headers=INGEST_AUTH)
    data = await resp.json()
    assert resp.status == 200 and data["ok"] is True and data["title"] == "First"
    assert [i["title"] for i in data["items"]] == ["First", "Second"]
    assert "First" in data["message"] and "Second" in data["message"] and "2 items" in data["message"]
