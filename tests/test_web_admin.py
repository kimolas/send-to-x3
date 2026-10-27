"""Admin UI: ingress trust, token/cookie sessions, CSRF, forms, setup, bundle, install."""

from __future__ import annotations

import io
import json
import re
import time
import zipfile

import aiohttp
import pytest

from sendtox3 import plugin_bundle
from sendtox3.ingest import Processor
from sendtox3.web import admin
from sendtox3.web.app import create_app
from sendtox3.web.common import STATE_KEY

from test_web_fakes import add_item, install_fakes

INGRESS = {"X-Ingress-Path": "/api/hassio_ingress/AbC123_-x", "X-Remote-User-Id": "u1"}
PDF = b"%PDF-1.4\nBT (Uploaded) Tj ET\n%%EOF"


@pytest.fixture
def web(monkeypatch):
    w = install_fakes(monkeypatch)
    w.add("https://example.com/pasted", "Pasted article")
    return w


@pytest.fixture
def app(opts, store, web):
    return create_app(opts, store, Processor(store, opts))


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


@pytest.fixture
async def ingress_client(aiohttp_client, app):
    app[admin.INGRESS_IPS_KEY] = frozenset({"127.0.0.1"})  # the test client connects from loopback
    return await aiohttp_client(app)


async def login(client) -> str:
    """Sign in with ?token= and return the CSRF token of the session."""
    resp = await client.get("/admin?token=admin-test-token")
    html = await resp.text()
    assert resp.status == 200, html
    return csrf_of(html)


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field"
    return m.group(1)


# -------------------------------------------------------------------- authentication


@pytest.mark.parametrize("path", ["/", "/admin", "/admin/setup", "/admin/plugin-bundle.zip"])
async def test_lan_requires_login(client, path):
    resp = await client.get(path)
    html = await resp.text()
    assert resp.status == 401 and "Admin token" in html and "admin-test-token" not in html


async def test_wrong_token_is_rejected(client):
    assert (await client.get("/admin?token=wrong")).status == 401


async def test_token_login_sets_httponly_cookie(client):
    resp = await client.get("/admin?token=admin-test-token")
    assert resp.status == 200
    cookie = resp.headers["Set-Cookie"]
    assert cookie.startswith(f"{admin.SESSION_COOKIE}=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "admin-test-token" not in cookie
    resp = await client.get("/admin/setup")  # the cookie now authenticates
    assert resp.status == 200


async def test_login_form(client):
    resp = await client.post("/admin/login", data={"token": "nope"})
    assert resp.status == 401
    resp = await client.post("/admin/login", data={"token": "admin-test-token"})
    assert resp.status == 200 and "Signed in." in await resp.text()
    assert (await client.get("/admin")).status == 200
    assert (await client.get("/admin/login")).status == 200


async def test_tampered_and_expired_cookies(client, app):
    await login(client)
    jar = client.session.cookie_jar
    value = next(c.value for c in jar if c.key == admin.SESSION_COOKIE)
    issued, sid, sig = value.split(".")
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({admin.SESSION_COOKIE: f"{issued}.{sid}x.{sig}"})
    assert (await client.get("/admin")).status == 401

    class Req:  # minimal request stand-in for signing an old cookie
        def __init__(self):
            self.app = app

    old = admin.make_session_cookie(Req(), "oldsid", issued=int(time.time()) - admin.SESSION_MAX_AGE - 10)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({admin.SESSION_COOKIE: old})
    assert (await client.get("/admin")).status == 401


async def test_rotating_admin_token_invalidates_sessions(client, app):
    await login(client)
    app[STATE_KEY].opts.admin_token = "rotated"
    assert (await client.get("/admin")).status == 401


async def test_logout(client):
    csrf = await login(client)
    resp = await client.post("/admin/logout", data={"csrf": csrf})
    assert resp.status == 200
    assert (await client.get("/admin")).status == 401


# --------------------------------------------------------------------------- ingress


async def test_ingress_is_trusted_and_links_use_prefix(ingress_client):
    resp = await ingress_client.get("/", headers=INGRESS)
    html = await resp.text()
    assert resp.status == 200
    assert 'href="/api/hassio_ingress/AbC123_-x/admin/setup"' in html
    assert 'action="/api/hassio_ingress/AbC123_-x/admin/send"' in html
    assert "/api/hassio_ingress/AbC123_-x/admin/static/admin.css" in html
    assert "Sign out" not in html
    assert "Set-Cookie" not in resp.headers


async def test_ingress_header_from_untrusted_ip_is_ignored(client):
    resp = await client.get("/admin", headers=INGRESS)  # default trusted IP is 172.30.32.2 only
    assert resp.status == 401


async def test_ingress_ip_without_header_needs_login(ingress_client):
    assert (await ingress_client.get("/admin")).status == 401


async def test_ingress_bad_prefix_is_not_used_in_links(ingress_client):
    resp = await ingress_client.get("/admin", headers={"X-Ingress-Path": '/x"><script>'})
    html = await resp.text()
    assert resp.status == 200 and "<script>" not in html.split("<main>")[0]


async def test_ingress_post_with_csrf(ingress_client, store):
    html = await (await ingress_client.get("/admin", headers=INGRESS)).text()
    csrf = csrf_of(html)
    resp = await ingress_client.post("/admin/send", data={"csrf": csrf, "content": "From ingress"}, headers=INGRESS)
    assert resp.status == 200 and store.count() == 1
    other_user = {**INGRESS, "X-Remote-User-Id": "u2"}
    resp = await ingress_client.post("/admin/send", data={"csrf": csrf, "content": "x"}, headers=other_user)
    assert resp.status == 403


# ------------------------------------------------------------------------------ CSRF


@pytest.mark.parametrize("path", ["/admin/send", "/admin/upload", "/admin/install-plugin", "/admin/logout",
                                  "/admin/items/abcdefghij/delete", "/admin/items/abcdefghij/requeue"])
async def test_posts_require_csrf(client, store, path):
    await login(client)
    for data in ({"content": "x"}, {"content": "x", "csrf": "forged"}):
        resp = await client.post(path, data=data)
        assert resp.status == 403
    assert store.count() == 0


async def test_csrf_of_another_session_is_rejected(aiohttp_client, app, store):
    c1 = await aiohttp_client(app)
    c2 = await aiohttp_client(app)
    csrf1 = await login(c1)
    await login(c2)
    resp = await c2.post("/admin/send", data={"csrf": csrf1, "content": "x"})
    assert resp.status == 403 and store.count() == 0


async def test_cross_origin_post_is_rejected(client, store):
    csrf = await login(client)
    resp = await client.post("/admin/send", data={"csrf": csrf, "content": "x"},
                             headers={"Origin": "http://evil.example"})
    assert resp.status == 403 and store.count() == 0


async def test_unauthenticated_post_shows_login(client, store):
    resp = await client.post("/admin/send", data={"csrf": "x", "content": "x"})
    assert resp.status == 401 and store.count() == 0


# ----------------------------------------------------------------------------- pages


async def test_queue_page_lists_items_with_badges(client, store):
    add_item(store, "Waiting one")
    add_item(store, "Delivered one", delivered={"auto": int(time.time())})
    add_item(store, "Broken one", status="failed")
    csrf = await login(client)
    html = await (await client.get("/admin")).text()
    assert "Waiting one" in html and "Delivered one" in html and "Broken one" in html
    assert "badge pending" in html and "badge delivered" in html and "status-failed" in html
    assert csrf in html


async def test_queue_pagination(client, store):
    for i in range(admin.PAGE_SIZE + 5):
        add_item(store, f"Item {i:03d}", created_at=1000 + i)
    await login(client)
    p1 = await (await client.get("/admin")).text()
    p2 = await (await client.get("/admin?page=2")).text()
    assert "Item 054" in p1 and "Item 000" not in p1
    assert "Item 000" in p2 and "Page 2 of 2" in p2


async def test_security_headers(client):
    resp = await client.get("/admin")
    assert "frame-ancestors 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Cache-Control"] == "no-store" and resp.headers["Referrer-Policy"] == "no-referrer"


async def test_paste_text_and_url(client, store, web):
    csrf = await login(client)
    resp = await client.post("/admin/send", data={"csrf": csrf, "content": "https://example.com/pasted"})
    html = await resp.text()
    assert resp.status == 200 and "Sent to X3: Pasted article" in html
    item = store.list_items()[0]
    assert item.kind == "url" and item.source == "admin"
    resp = await client.post("/admin/send", data={"csrf": csrf, "content": "  "})
    assert resp.status == 422


async def test_upload_pdf_and_rejections(client, store):
    csrf = await login(client)
    form = aiohttp.FormData()
    form.add_field("csrf", csrf)
    form.add_field("file", PDF, filename="Statement.pdf", content_type="application/pdf")
    resp = await client.post("/admin/upload", data=form)
    assert resp.status == 200, await resp.text()
    item = store.list_items()[0]
    assert item.kind == "pdf" and item.title == "Statement" and item.source == "admin"
    form = aiohttp.FormData()
    form.add_field("csrf", csrf)
    form.add_field("file", b"\x00\x01\x02" * 10, filename="x.bin", content_type="application/octet-stream")
    assert (await client.post("/admin/upload", data=form)).status == 422
    form = aiohttp.FormData()
    form.add_field("csrf", csrf)
    assert (await client.post("/admin/upload", data=form)).status == 422


async def test_item_preview_download_requeue_delete(client, store):
    it = add_item(store, "Preview me", text="Hello <b>preview</b> text", delivered={"opds": 5})
    csrf = await login(client)
    html = await (await client.get(f"/admin/items/{it.id}")).text()
    assert "Hello &lt;b&gt;preview&lt;/b&gt; text" in html  # escaped
    resp = await client.get(f"/admin/items/{it.id}/download/epub")
    assert resp.status == 200 and await resp.read() == store.file_bytes(it.id, "epub")
    assert "attachment" in resp.headers["Content-Disposition"]
    assert store.get(it.id).delivered == {"opds": 5}  # admin downloads do not count as delivery
    await client.post(f"/admin/items/{it.id}/requeue", data={"csrf": csrf})
    assert store.get(it.id).is_pending
    resp = await client.post(f"/admin/items/{it.id}/delete", data={"csrf": csrf})
    assert resp.status == 200 and store.get(it.id) is None
    assert (await client.get(f"/admin/items/{it.id}")).status == 404


async def test_channel_health_rendering(client, app):
    state = app[STATE_KEY]
    state.set_status("lan_push", "error", "X3 not reachable")
    state.status["imap"] = "weird string status"
    state.opts.device_host = "192.168.1.60"
    await login(client)
    html = await (await client.get("/admin")).text()
    assert "X3 not reachable" in html and "state-error" in html
    assert "LAN push to the X3" in html
    assert re.search(r"X3 plugin \(CrossPoint 2\.x\)</td>\s*<td><span class=\"badge state-waiting\">waiting", html)


async def test_repeated_delivery_warning(client, store):
    store.save_delivery("e", "1", ["x"], b"EPUB")
    store.get_delivery("e", "1")
    await login(client)
    assert "may be failing to save it" in await (await client.get("/admin")).text()


async def test_setup_page(client, app):
    await login(client)
    html = await (await client.get("/admin/setup")).text()
    assert "http://127.0.0.1:8765/api/v1/ingest" in html and "http://127.0.0.1:8765/opds" in html
    for token in ("ingest-test-token", "device-test-token", "admin-test-token"):
        assert token in html
    assert html.count("<svg") == 3 and 'class="qr"' in html
    assert "/admin/plugin-bundle.zip" in html and "Install plugin on my X3 now" in html
    assert "Download iOS Shortcut" not in html  # never offered: it must be signed on a Mac


async def test_setup_page_without_base_url(client, app):
    app[STATE_KEY].base_url = ""
    await login(client)
    html = await (await client.get("/admin/setup")).text()
    assert "could not be detected" in html and "<svg" not in html
    assert (await client.get("/admin/plugin-bundle.zip")).status == 409


async def test_setup_warns_about_docker_address(client, app):
    # A detected address; a server_host typed outside Home Assistant is not second-guessed.
    app[STATE_KEY].base_url, app[STATE_KEY].base_url_source = "http://172.30.33.5:8765", "local address"
    await login(client)
    assert "inside Docker" in await (await client.get("/admin/setup")).text()


async def test_bundle_download(client):
    await login(client)
    resp = await client.get("/admin/plugin-bundle.zip")
    data = await resp.read()
    assert resp.status == 200 and resp.headers["Content-Type"] == "application/zip"
    assert resp.headers["Content-Length"] == str(len(data))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        dj = json.loads(z.read(".crosspoint/plugins/sendtox3/device.json"))
    assert dj["browse"]["url"].startswith("http://127.0.0.1:8765/device/v1/catalog")
    assert dj["browse"]["headers"]["Authorization"] == "Bearer device-test-token"


async def test_install_plugin_button(client, monkeypatch):
    calls = []

    async def fake_install(host, *, base_url, device_token, opts):
        calls.append((host, base_url, device_token))
        return ["Connecting ...", "Done: the plugin is installed."]

    monkeypatch.setattr(plugin_bundle, "install_to_device", fake_install)
    csrf = await login(client)
    resp = await client.post("/admin/install-plugin", data={"csrf": csrf, "device_host": "192.168.1.77"})
    html = await resp.text()
    assert resp.status == 200 and "Plugin installed." in html and "Done: the plugin is installed." in html
    assert calls == [("192.168.1.77", "http://127.0.0.1:8765", "device-test-token")]


async def test_install_plugin_failure_is_shown(client, monkeypatch):
    async def fake_install(host, **kw):
        return ["Error: could not reach the X3 at 10.0.0.9."]

    monkeypatch.setattr(plugin_bundle, "install_to_device", fake_install)
    csrf = await login(client)
    html = await (await client.post("/admin/install-plugin", data={"csrf": csrf, "device_host": "10.0.0.9"})).text()
    assert "The plugin was not installed." in html and "could not reach the X3" in html


async def test_static_files(client):
    resp = await client.get("/admin/static/admin.css")  # no auth needed for static assets
    assert resp.status == 200 and resp.headers["Content-Type"].startswith("text/css")
    assert resp.headers["Content-Length"] == str(len(await resp.read()))
    assert (await client.get("/admin/static/admin.js")).status == 200
    for bad in ("/admin/static/../app.py", "/admin/static/%2e%2e%2fapp.py", "/admin/static/missing.css",
                "/admin/static/__init__.py"):
        assert (await client.get(bad)).status == 404


async def test_no_ios_shortcut_download_even_when_a_file_exists(client, monkeypatch, tmp_path):
    # The app never ships a signed .shortcut (it must be built and signed on a Mac, see
    # docs/phone-ios.md), so the Setup page explains that instead of offering a download.
    (tmp_path / "send-to-x3.shortcut").write_bytes(b"AEA1fake")
    monkeypatch.setattr(admin, "STATIC_DIR", tmp_path)
    await login(client)
    assert "Download iOS Shortcut" not in await (await client.get("/admin/setup")).text()
    assert (await client.get("/admin/static/send-to-x3.shortcut")).status == 404


async def test_never_redirects(client):
    for path in ("/admin/", "/admin/setup/", "/opds/"):
        resp = await client.get(path, allow_redirects=False)
        assert not 300 <= resp.status < 400
