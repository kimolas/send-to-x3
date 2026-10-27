"""CrossPoint 2.x plugin bundle (§10), LAN install flow, server-address detection."""

from __future__ import annotations

import io
import json
import re
import zipfile

import pytest
from aiohttp import web

from sendtox3 import __version__, plugin_bundle
from sendtox3.plugin_bundle import (
    PLUGIN_DIR,
    PLUGIN_NAME,
    build_bundle_zip,
    build_device_json,
    device_json_bytes,
    install_to_device,
    looks_container_internal,
    normalize_base_url,
    resolve_base_url,
)

BASE = "http://192.168.1.50:8765"
TOKEN = "device-test-token"


def test_device_json_matches_spec_exactly(opts):
    assert build_device_json(base_url=BASE, device_token=TOKEN, opts=opts) == {
        "title": "Send to X3",
        "description": "Articles and notes you shared",
        "browse": {
            "format": "json",
            "url": "http://192.168.1.50:8765/device/v1/catalog?page={page}&limit={limit}",
            "headers": {"Authorization": "Bearer device-test-token"},
            "items": "items",
            "fields": {"title": "title", "author": "subtitle", "id": "id"},
            "page_size": 8,
        },
        "download": {
            "url": "http://192.168.1.50:8765/device/v1/items/{id}.epub",
            "headers": {"Authorization": "Bearer device-test-token"},
            "dest_dir": "/Inbox",
            "filename": "{title}.epub",
        },
        "events": {
            "sleep.enter": {
                "download": {
                    "url": "http://192.168.1.50:8765/device/v1/deliver?ev={event.id}&ts={event.ts}",
                    "headers": {"Authorization": "Bearer device-test-token"},
                    "dest": "/Inbox/{event.ts}-{event.id}.epub",
                },
                "toast": "Send to X3: new reading delivered",
            }
        },
    }


def test_auto_delivery_off_removes_events(opts):
    opts.auto_delivery = False
    assert "events" not in build_device_json(base_url=BASE, device_token=TOKEN, opts=opts)


def test_txt_delivery_format(opts):
    opts.delivery_format = "txt"
    dj = build_device_json(base_url=BASE, device_token=TOKEN, opts=opts)
    assert dj["download"]["url"].endswith("/device/v1/items/{id}.txt") and dj["download"]["filename"] == "{title}.txt"
    assert dj["events"]["sleep.enter"]["download"]["dest"].endswith(".epub")  # auto-delivery is always EPUB
    opts.delivery_format = "mobi"
    assert build_device_json(base_url=BASE, device_token=TOKEN, opts=opts)["download"]["filename"] == "{title}.epub"


def test_custom_inbox_folder(opts):
    opts.inbox_folder = "Reading/Shared/"
    dj = build_device_json(base_url=BASE, device_token=TOKEN, opts=opts)
    assert dj["download"]["dest_dir"] == "/Reading/Shared"
    assert dj["events"]["sleep.enter"]["download"]["dest"] == "/Reading/Shared/{event.ts}-{event.id}.epub"
    with zipfile.ZipFile(io.BytesIO(build_bundle_zip(base_url=BASE, device_token=TOKEN, opts=opts))) as z:
        assert "Reading/Shared/.keep" in z.namelist() and "Reading/" in z.namelist()


def test_strict_json_and_size(opts):
    data = device_json_bytes(base_url=BASE, device_token=TOKEN, opts=opts)
    assert len(data) <= 8192
    text = data.decode("utf-8")
    assert json.loads(text) == build_device_json(base_url=BASE, device_token=TOKEN, opts=opts)
    assert "//" not in text.replace("http://", "") and "/*" not in text  # no comments
    placeholders = set(re.findall(r"\{[a-z.]+\}", text))
    assert placeholders == {"{page}", "{limit}", "{id}", "{title}", "{event.id}", "{event.ts}"}
    long_base = "http://" + "h" * 200 + ".example:8765"
    assert len(device_json_bytes(base_url=long_base, device_token="t" * 64, opts=opts)) <= 8192


@pytest.mark.parametrize("bad", ["", "192.168.1.50:8765", "ftp://x", "http://", "http://a b", "http://h/{x}",
                                 "http://h/?q=1", "http://h/#f", 'http://h"x'])
def test_invalid_base_urls(bad):
    with pytest.raises(ValueError):
        normalize_base_url(bad)


def test_base_url_normalization():
    assert normalize_base_url(" http://192.168.1.50:8765/ ") == "http://192.168.1.50:8765"
    assert normalize_base_url("https://x3.example/prefix/") == "https://x3.example/prefix"


@pytest.mark.parametrize("token", ["", "a b", "tok{en}", 'q"', "back\\slash", "é"])
def test_invalid_tokens(opts, token):
    with pytest.raises(ValueError):
        build_device_json(base_url=BASE, device_token=token, opts=opts)


def test_invalid_inbox(opts):
    for bad in ("/", "../x", "/a/{b}", "/a:b"):
        opts.inbox_folder = bad
        with pytest.raises(ValueError):
            build_device_json(base_url=BASE, device_token=TOKEN, opts=opts)


def test_bundle_zip_layout(opts):
    data = build_bundle_zip(base_url=BASE, device_token=TOKEN, opts=opts)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.testzip() is None
        names = set(z.namelist())
        for n in (".crosspoint/plugins/sendtox3/device.json", ".crosspoint/plugins/sendtox3/manifest.json",
                  ".crosspoint/plugins/sendtox3/README.md", "Inbox/.keep"):
            assert n in names
        assert not any(n.startswith("/") or ".." in n for n in names)
        assert z.read(".crosspoint/plugins/sendtox3/device.json") == device_json_bytes(
            base_url=BASE, device_token=TOKEN, opts=opts)
        manifest = json.loads(z.read(".crosspoint/plugins/sendtox3/manifest.json"))
        assert manifest == {"title": "Send to X3", "description": "Articles and notes you shared",
                            "version": __version__}
        readme = z.read(".crosspoint/plugins/sendtox3/README.md").decode()
        assert BASE in readme and TOKEN not in readme
        assert z.read("Inbox/.keep") == b""
    assert len(PLUGIN_NAME.encode()) <= 23 and PLUGIN_DIR == "/.crosspoint/plugins/sendtox3"


# ----------------------------------------------------------------------- install


class FakeDevice:
    """Mirrors the DeviceClient contract: status() is None when unreachable, mkdir()
    returns a bool, write_plugin_file() returns the transport or raises."""

    def __init__(self, *, version="2.0.0-rc+abc", plugins_on=True, write_error=None, reachable=True,
                 mkdir_ok=True, status_raises=False):
        self.version = version
        self.plugins_on = plugins_on
        self.write_error = write_error
        self.reachable = reachable
        self.mkdir_ok = mkdir_ok
        self.status_raises = status_raises
        self.dirs: set[str] = {"/.crosspoint"}
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.closed = False

    async def status(self):
        self.calls.append("status")
        if self.status_raises:
            raise ConnectionError("boom")
        if not self.reachable:
            return None
        st = {"version": self.version, "device": "X3", "ip": "192.168.1.60"}
        if self.version.startswith("2"):
            st["pluginApi"] = 1
        return st

    async def mkdir(self, parent, name):
        path = parent.rstrip("/") + "/" + name
        self.calls.append(f"mkdir {path}")
        if self.mkdir_ok:
            self.dirs.add(path)
        return self.mkdir_ok

    async def write_plugin_file(self, plugin_name, sd_path, data):
        self.calls.append(f"write {plugin_name} {sd_path}")
        if self.write_error:
            raise RuntimeError(self.write_error)
        self.files[sd_path] = data
        return "plugin-fs" if self.plugins_on else "upload"

    async def plugins_enabled(self):
        return self.plugins_on

    async def set_settings(self, values):
        self.calls.append(f"settings {values}")

    async def close(self):
        self.closed = True


async def test_install_happy_path(opts):
    dev = FakeDevice()
    lines = await install_to_device("192.168.1.60", base_url=BASE, device_token=TOKEN, opts=opts, client=dev)
    assert lines[-1].startswith("Done: the plugin is installed. Leave File Transfer"), lines
    assert not any(l.startswith(("Error", "Warning")) for l in lines)
    assert json.loads(dev.files[f"{PLUGIN_DIR}/device.json"])["browse"]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert set(dev.files) == {f"{PLUGIN_DIR}/device.json", f"{PLUGIN_DIR}/manifest.json", f"{PLUGIN_DIR}/README.md"}
    assert "/Inbox" in dev.dirs and dev.closed
    assert not any(c.startswith("settings") for c in dev.calls)  # never changes device settings


async def test_install_with_plugins_off_tells_the_user(opts):
    dev = FakeDevice(plugins_on=False)
    lines = await install_to_device("x3", base_url=BASE, device_token=TOKEN, opts=opts, client=dev)
    assert lines[-1].startswith("Done") and "Plugins is off" in lines[-1]
    assert "Settings > Connectivity > Plugins" in lines[-1]
    assert any("via upload" in l for l in lines)


async def test_install_write_failure(opts):
    dev = FakeDevice(write_error="upload of 'device.json' refused: HTTP 400 Failed to create file on SD card")
    lines = await install_to_device("x3", base_url=BASE, device_token=TOKEN, opts=opts, client=dev)
    assert lines[-1].startswith("Error: writing /.crosspoint/plugins/sendtox3/device.json failed") and dev.closed


@pytest.mark.parametrize("kw", [{"reachable": False}, {"status_raises": True}])
async def test_install_unreachable_device(opts, kw):
    dev = FakeDevice(**kw)
    lines = await install_to_device("10.0.0.9", base_url=BASE, device_token=TOKEN, opts=opts, client=dev)
    assert lines[-1].startswith("Error: could not reach the X3 at 10.0.0.9") and dev.closed
    assert dev.files == {}


async def test_install_refuses_165(opts):
    dev = FakeDevice(version="1.6.5")
    lines = await install_to_device("x3", base_url=BASE, device_token=TOKEN, opts=opts, client=dev)
    assert lines[-1].startswith("Error: CrossPoint 1.6.5 has no plugin support") and dev.files == {}


async def test_install_inbox_failure_is_a_warning(opts):
    dev = FakeDevice(mkdir_ok=False)
    lines = await install_to_device("x3", base_url=BASE, device_token=TOKEN, opts=opts, client=dev)
    assert any(l.startswith("Warning: could not create /Inbox") for l in lines) and lines[-1].startswith("Done")


async def test_install_input_errors(opts):
    assert (await install_to_device("", base_url=BASE, device_token=TOKEN, opts=opts))[0].startswith("Error")
    lines = await install_to_device("x3", base_url="not a url", device_token=TOKEN, opts=opts, client=FakeDevice())
    assert lines[0].startswith("Error")


# A CrossPoint 2.x File Transfer server, emulated closely enough for DeviceClient: /api/status,
# /mkdir (creates parents, refuses dot leaves), /api/files (hides dot names), /api/plugin-fs
# (403 when Plugins is off), /upload (never overwrites), /delete.


def _fake_crosspoint(sd: dict, *, plugins_on: bool):
    import posixpath

    dirs = sd.setdefault("dirs", {"/", "/.crosspoint"})
    files = sd.setdefault("files", {})

    def norm(p):
        return posixpath.normpath("/" + (p or "/").strip("/")).replace("//", "/")

    async def status(request):
        return web.json_response({"version": "2.0.0-rc+3f387a9", "device": "X3", "pluginApi": 1, "mode": "STA"})

    async def mkdir(request):
        form = await request.post()
        name, parent = form.get("name", ""), norm(form.get("path", "/"))
        if name.startswith("."):
            return web.Response(status=403, text="Cannot create protected item")
        path = norm(parent + "/" + name)
        if path in dirs or path in files:
            return web.Response(status=400, text="Folder already exists")
        while path != "/":
            dirs.add(path)
            path = posixpath.dirname(path)
        return web.Response(text=f"Folder created: {name}")

    async def list_files(request):
        folder = norm(request.query.get("path", "/"))
        out = []
        for d in dirs:
            if d != folder and posixpath.dirname(d) == folder and not posixpath.basename(d).startswith("."):
                out.append({"name": posixpath.basename(d), "size": 0, "isDirectory": True, "isEpub": False})
        for f, data in files.items():
            if posixpath.dirname(f) == folder and not posixpath.basename(f).startswith("."):
                out.append({"name": posixpath.basename(f), "size": len(data), "isDirectory": False,
                            "isEpub": f.endswith(".epub")})
        return web.json_response(out)

    async def read_part(request):
        reader = await request.multipart()
        part = await reader.next()
        if part is None:
            return None, None
        return part.filename, await part.read()

    async def plugin_fs(request):
        if not plugins_on:
            return web.Response(status=403, text="plugin system disabled")
        _, data = await read_part(request)
        if data is None:
            return web.Response(status=400, text="missing file part")
        path = norm(request.query["path"])
        d = posixpath.dirname(path)
        while d != "/":
            dirs.add(d)
            d = posixpath.dirname(d)
        files[path] = bytes(data)
        return web.json_response({"ok": True, "bytes": len(data)})

    async def upload(request):
        folder = norm(request.query.get("path", "/"))
        name, data = await read_part(request)
        if folder not in dirs:
            return web.Response(status=400, text="Failed to create file on SD card")
        path = norm(folder + "/" + name)
        if any(k.casefold() == path.casefold() for k in files):
            return web.Response(status=400, text=f"File already exists: {name}")
        files[path] = bytes(data)
        return web.Response(text=f"File uploaded successfully: {name}")

    async def delete(request):
        form = await request.post()
        files.pop(norm(form.get("path", "")), None)
        return web.Response(text="All items deleted successfully")

    app = web.Application()
    app.router.add_get("/api/status", status)
    app.router.add_post("/mkdir", mkdir)
    app.router.add_get("/api/files", list_files)
    app.router.add_post("/api/plugin-fs", plugin_fs)
    app.router.add_post("/upload", upload)
    app.router.add_post("/delete", delete)
    return app


@pytest.mark.parametrize("plugins_on", [True, False])
async def test_install_with_real_device_client(aiohttp_server, opts, plugins_on):
    pytest.importorskip("sendtox3.device_client")
    sd: dict = {}
    server = await aiohttp_server(_fake_crosspoint(sd, plugins_on=plugins_on))
    host = f"127.0.0.1:{server.port}"
    for attempt in range(2):  # a second install must replace the files, not add "device (2).json"
        opts.auto_delivery = attempt == 0
        lines = await install_to_device(host, base_url=BASE, device_token=TOKEN, opts=opts)
        assert lines[-1].startswith("Done"), lines
        assert ("Plugins is off" in lines[-1]) == (not plugins_on)
        dj = json.loads(sd["files"][f"{PLUGIN_DIR}/device.json"])
        assert ("events" in dj) == (attempt == 0)
    assert sorted(k for k in sd["files"]) == sorted(f"{PLUGIN_DIR}/{n}" for n in ("README.md", "device.json",
                                                                                 "manifest.json"))
    assert "/Inbox" in sd["dirs"]


# ---------------------------------------------------------------- server address


async def test_server_host_option(opts):
    opts.server_host = "192.168.1.50"
    assert await resolve_base_url(opts, supervisor_token="") == ("http://192.168.1.50:8765", "options")
    opts.server_host = "192.168.1.50:9000"
    assert (await resolve_base_url(opts))[0] == "http://192.168.1.50:9000"
    opts.server_host = "http://pi.lan:8765/"
    assert (await resolve_base_url(opts))[0] == "http://pi.lan:8765"


def _supervisor_app(*, interfaces, network, token="sup-token", fail_info=False):
    async def network_info(request):
        assert request.headers["Authorization"] == f"Bearer {token}"
        return web.json_response({"result": "ok", "data": {"interfaces": interfaces}})

    async def addon_info(request):
        if fail_info:
            return web.json_response({"result": "error"}, status=403)
        return web.json_response({"result": "ok", "data": {"network": network}})

    app = web.Application()
    app.router.add_get("/network/info", network_info)
    app.router.add_get("/addons/self/info", addon_info)
    return app


IFACES = [
    {"interface": "wlan0", "enabled": True, "connected": True, "primary": False,
     "ipv4": {"address": ["192.168.1.77/24"]}},
    {"interface": "end0", "enabled": True, "connected": True, "primary": True,
     "ipv4": {"address": ["192.168.1.50/24"]}},
    {"interface": "eth1", "enabled": False, "connected": False, "primary": False, "ipv4": {"address": ["10.9.9.9/8"]}},
]


async def test_supervisor_detection(aiohttp_server, opts):
    opts.server_host = ""
    server = await aiohttp_server(_supervisor_app(interfaces=IFACES, network={"8765/tcp": 18765}))
    url = str(server.make_url("")).rstrip("/")
    assert await resolve_base_url(opts, supervisor_url=url, supervisor_token="sup-token") == \
        ("http://192.168.1.50:18765", "supervisor")


async def test_supervisor_port_disabled_or_unknown_uses_container_port(aiohttp_server, opts):
    opts.server_host = ""
    for app in (_supervisor_app(interfaces=IFACES, network={"8765/tcp": None}),
                _supervisor_app(interfaces=IFACES, network={}, fail_info=True)):
        server = await aiohttp_server(app)
        url = str(server.make_url("")).rstrip("/")
        assert (await resolve_base_url(opts, supervisor_url=url, supervisor_token="sup-token"))[0] == \
            "http://192.168.1.50:8765"


async def test_supervisor_failure_falls_back_to_local_ip(aiohttp_server, opts, monkeypatch):
    opts.server_host = ""
    monkeypatch.setattr(plugin_bundle, "local_ipv4", lambda: "10.0.0.5")
    url = "http://127.0.0.1:9"  # nothing listens there
    assert await resolve_base_url(opts, supervisor_url=url, supervisor_token="t", timeout=0.5) == \
        ("http://10.0.0.5:8765", "local address")
    assert await resolve_base_url(opts, supervisor_token="") == ("http://10.0.0.5:8765", "local address")
    monkeypatch.setattr(plugin_bundle, "local_ipv4", lambda: "")
    assert await resolve_base_url(opts, supervisor_token="") == ("", "")
    assert await plugin_bundle.detect_base_url(opts, supervisor_token="") == ""


def test_local_ipv4_is_not_loopback():
    ip = plugin_bundle.local_ipv4()
    assert ip == "" or not ip.startswith("127.")


def test_looks_container_internal():
    assert looks_container_internal("http://172.30.33.4:8765")
    assert looks_container_internal("http://172.17.0.2:8765")
    assert not looks_container_internal("http://192.168.1.50:8765")
    assert not looks_container_internal("http://pi.lan:8765")
