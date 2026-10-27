"""DeviceClient against the fake CrossPoint File Transfer server (1.6.5 and 2.0)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from fakes.crosspoint_server import FakeCrossPoint, core_parse_multipart
from sendtox3.device_client import (
    DeviceClient,
    DeviceRejected,
    DeviceUnreachable,
    build_multipart,
    check_filename,
    is_plugin_firmware,
    normalize_host,
    normalize_sd_path,
    numbered_name,
    parse_version,
    sanitize_filename,
    split_ext,
)

VERSIONS = ["1.6.5", "2.0"]


@pytest.fixture(params=VERSIONS)
async def device(request, tmp_path):
    async with FakeCrossPoint(tmp_path / "sd", version=request.param) as dev:
        yield dev


@pytest.fixture
async def client(device):
    async with DeviceClient(device.host, timeout=3, retries=2, backoff=0.01) as c:
        yield c


# ------------------------------------------------------------------ pure helpers


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1.6.5", (1, 6, 5)),
        ("2.0.0", (2, 0, 0)),
        ("2.0.0-rc+3f387a9", (2, 0, 0)),
        ("2.0.0-slim", (2, 0, 0)),
        ("1.1.0-dev-feat-kosync-xpath-05c6cf8", (1, 1, 0)),
        ("2.1", (2, 1, 0)),
        ("v3", (3, 0, 0)),
        ("", None),
        ("unknown", None),
        (None, None),
        (165, None),
    ],
)
def test_parse_version(raw, expected):
    assert parse_version(raw) == expected


def test_is_plugin_firmware():
    assert is_plugin_firmware({"version": "2.0.0-rc+abc", "pluginApi": 1})
    assert is_plugin_firmware({"version": "2.1.0"})
    assert not is_plugin_firmware({"version": "1.6.5"})
    assert not is_plugin_firmware(None)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("192.168.1.50", "192.168.1.50"),
        (" 192.168.1.50:80 ", "192.168.1.50:80"),
        ("http://192.168.1.50/", "192.168.1.50"),
        ("http://crosspoint.local:8080/files", "crosspoint.local:8080"),
        ("fe80::1", "[fe80::1]"),
        ("[fe80::1]:80", "[fe80::1]:80"),
    ],
)
def test_normalize_host(raw, expected):
    assert normalize_host(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "https://192.168.1.5"])
def test_normalize_host_rejects(bad):
    with pytest.raises(ValueError):
        normalize_host(bad)


@pytest.mark.parametrize(
    "raw, expected",
    [("", "/"), ("/", "/"), ("Inbox", "/Inbox"), ("/Inbox/", "/Inbox"), ("//a//b/../c", "/a/c"),
     ("/../..", "/"), ("/./x", "/x")],
)
def test_normalize_sd_path(raw, expected):
    assert normalize_sd_path(raw) == expected


def test_names():
    assert split_ext("Title.epub") == ("Title", ".epub")
    assert split_ext("Mr. Smith goes to town") == ("Mr. Smith goes to town", "")
    assert split_ext("README") == ("README", "")
    assert numbered_name("Title.epub", 1) == "Title.epub"
    assert numbered_name("Title.epub", 2) == "Title (2).epub"
    assert numbered_name("README", 3) == "README (3)"
    long = "é" * 200 + ".epub"  # 400 bytes of stem
    n = numbered_name(long, 12)
    assert len(n.encode()) <= 255 and n.endswith(" (12).epub")
    assert sanitize_filename('A/B: "c"? <d> | e*', "epub") == "A-B - 'c' d - e.epub"
    assert sanitize_filename("...hidden. ", "txt") == "hidden.txt"
    assert sanitize_filename("\x00\x01", "epub", fallback="Note x") == "Note x.epub"
    assert len(sanitize_filename("x" * 400, "epub").encode()) == 255


@pytest.mark.parametrize(
    "bad", ["", ".", "..", "a/b", "a\\b", 'a"b', "a:b", "a?b", "a*b", "a<b", "a|b", "a\nb", " lead", "trail ",
            "dot.", ".hidden", "x" * 256]
)
def test_check_filename_rejects(bad):
    with pytest.raises(ValueError):
        check_filename(bad)


def test_check_filename_accepts():
    check_filename("Café – “quotes” (2).epub")
    check_filename(".keep", allow_hidden=True)


def test_multipart_shape_matches_firmware_parser():
    body, ctype = build_multipart("Ünïcødé – title.epub", b"\r\n--x\r\nbinary\x00data")
    assert ctype.startswith("multipart/form-data; boundary=") and ";" not in ctype[len("multipart/form-data;"):]
    boundary = ctype.split("=", 1)[1]
    assert len(boundary) <= 70
    head = body.split(b"\r\n\r\n", 1)[0].decode()
    assert 'Content-Disposition: form-data; name="file"; filename="Ünïcødé – title.epub"' in head
    assert "filename*" not in head
    parts = core_parse_multipart(ctype, body, [])
    assert [(p.name, p.filename, p.data) for p in parts] == [
        ("file", "Ünïcødé – title.epub", b"\r\n--x\r\nbinary\x00data")
    ]
    with pytest.raises(ValueError):
        build_multipart('bad"name', b"x")


# ------------------------------------------------------------------------ status


async def test_status_and_version(device, client):
    st = await client.status()
    assert st is not None and st["device"] == "X3" and st["mode"] == "STA"
    if device.family == "2.0":
        assert st["pluginApi"] == 1 and st["firmwareFamily"] == "crosspoint"
        assert await client.firmware_version() == (2, 0, 0)
        st2 = await client.status(plugin="SendToX3")
        assert len(st2["deviceId"]) == 64
    else:
        assert "pluginApi" not in st
        assert await client.firmware_version() == (1, 6, 5)
    assert client.last_status is not None


async def test_status_none_when_offline_and_back(device, client):
    device.go_offline()
    t0 = time.monotonic()
    assert await client.status() is None
    assert time.monotonic() - t0 < 2
    await device.go_online()
    assert (await client.status())["device"] == "X3"


async def test_status_times_out_quickly(device, client):
    device.inject("GET", "/api/status", delay=1)
    t0 = time.monotonic()
    assert await client.status(timeout=0.3) is None
    assert time.monotonic() - t0 < 1.5


async def test_status_none_for_non_crosspoint(device, client):
    device.inject("GET", "/api/status", status=200, body="<html>router login</html>")
    assert await client.status() is None
    device.inject("GET", "/api/status", status=404, body="nope")
    assert await client.status() is None


async def test_unreachable_host_raises():
    async with DeviceClient("127.0.0.1:9", timeout=0.5, retries=1, backoff=0.01) as c:
        assert await c.status() is None
        with pytest.raises(DeviceUnreachable):
            await c.list_files("/")


# ----------------------------------------------------------------------- folders


async def test_mkdir_semantics(device, client):
    assert await client.mkdir("/", "Inbox") is True
    assert device.resolve("/Inbox").is_dir()
    assert await client.mkdir("/", "Inbox") is True  # 400 "Folder already exists" counts
    assert await client.mkdir("/", "inbox") is True  # FAT is case-insensitive
    assert await client.makedirs("/Deep/er/still") is True  # firmware creates parents
    assert device.resolve("/Deep/er/still").is_dir()
    assert await client.makedirs("/") is True
    device.write("/afile", b"x")
    assert await client.mkdir("/", "afile") is False  # "already exists" but it is a file
    with pytest.raises(ValueError):
        await client.mkdir("/", ".crosspoint")  # the device answers 403 for dot names
    with pytest.raises(ValueError):
        await client.mkdir("/", "XTCache")
    # A non-dot leaf below a dot folder is allowed by the firmware.
    assert await client.makedirs("/.crosspoint/plugins/sendtox3") is True
    assert device.resolve("/.crosspoint/plugins/sendtox3").is_dir()


async def test_mkdir_refusal_is_false(device, client):
    device.inject("POST", "/mkdir", status=500, body="Failed to create folder")
    assert await client.mkdir("/", "Inbox") is False


async def test_mkdir_retries_when_the_server_lost_the_form(device, client):
    device.inject("POST", "/mkdir", status=400, body="Missing folder name")
    assert await client.mkdir("/", "Inbox") is True
    assert len(device.requests_to("POST", "/mkdir")) == 2


async def test_verify_looks_twice_before_failing(device, client):
    """A glitchy first listing must not turn a stored upload into a failure."""
    await client.makedirs("/Inbox")
    device.inject("GET", "/api/files", status=200, body="[]", count=2)  # upload's own listing + first verify
    assert await client.upload("/Inbox", "a.epub", b"abc") == "a.epub"
    assert device.read("/Inbox/a.epub") == b"abc"


async def test_list_files(device, client):
    device.write("/Inbox/a.epub", b"12345")
    device.write("/Inbox/b.txt", b"1")
    device.write("/Inbox/.hidden", b"x")
    device.resolve("/Inbox/sub").mkdir()
    entries = {e.name: e for e in await client.list_files("/Inbox")}
    assert set(entries) == {"a.epub", "b.txt", "sub"}
    assert entries["a.epub"].size == 5 and entries["a.epub"].is_epub and not entries["a.epub"].is_dir
    assert entries["sub"].is_dir and not entries["b.txt"].is_epub
    assert await client.list_files("/missing") == []  # device: same as empty
    assert (await client.find("/inbox", "A.EPUB")).name == "a.epub"
    assert await client.find("/Inbox", "nope") is None


async def test_list_files_retries_after_reset(device, client):
    device.write("/Inbox/a.epub", b"x")
    device.inject("GET", "/api/files", reset=True)
    assert [e.name for e in await client.list_files("/Inbox")] == ["a.epub"]


async def test_list_files_bad_json(device, client):
    device.inject("GET", "/api/files", status=200, body="[{oops")
    with pytest.raises(DeviceRejected):
        await client.list_files("/")


# ------------------------------------------------------------------------ upload


async def test_upload_new_file_marks_library(device, client):
    await client.makedirs("/Inbox")
    name = await client.upload("/Inbox", "My Article.epub", b"EPUBDATA")
    assert name == "My Article.epub"
    assert device.read("/Inbox/My Article.epub") == b"EPUBDATA"
    assert device.library_dirty  # /upload marks the Library index stale on both versions
    assert len(device.requests_to("POST", "/upload")) == 1


async def test_upload_collisions_get_numbered(device, client):
    await client.makedirs("/Inbox")
    device.write("/Inbox/Note.epub", b"old")
    device.write("/Inbox/note (2).epub", b"old2")  # different case still collides on FAT
    name = await client.upload("/Inbox", "Note.epub", b"new")
    assert name == "Note (3).epub"
    assert device.read("/Inbox/Note.epub") == b"old"  # never overwritten
    assert device.read("/Inbox/Note (3).epub") == b"new"


async def test_upload_collision_not_in_listing(device, client):
    """'File already exists' for a name the listing did not show (e.g. a hidden-from-listing entry)."""
    await client.makedirs("/Inbox")
    device.inject("POST", "/upload", status=400, body="File already exists: Note.epub")
    assert await client.upload("/Inbox", "Note.epub", b"x") == "Note (2).epub"


async def test_upload_unicode_and_spaces(device, client):
    await client.makedirs("/Inbox")
    title = "Café – “Ünïcødé” + 100% & more #1.epub"
    assert await client.upload("/Inbox", title, b"x" * 3000) == title
    assert device.read("/Inbox/" + title) == b"x" * 3000


async def test_upload_large_binary(device, client):
    await client.makedirs("/Books")
    data = bytes(range(256)) * 4096  # 1 MiB with CR/LF/dashes inside
    assert await client.upload("/Books", "big.epub", data) == "big.epub"
    assert device.read("/Books/big.epub") == data


async def test_upload_missing_folder_is_rejected(device, client):
    with pytest.raises(DeviceRejected) as exc:
        await client.upload("/NoSuchFolder", "a.epub", b"x")
    assert "Failed to create file on SD card" in str(exc.value)


async def test_upload_invalid_name_never_sent(device, client):
    with pytest.raises(ValueError):
        await client.upload("/", "a:b.epub", b"x")
    assert device.requests_to("POST", "/upload") == []


async def test_upload_never_trusts_stale_success(device, client):
    """The firmware can answer 200 for a previous file; the listing decides."""
    await client.makedirs("/Inbox")
    device.inject("POST", "/upload", status=200, body="File uploaded successfully: Other.epub")
    with pytest.raises(DeviceRejected):
        await client.upload("/Inbox", "Mine.epub", b"x")
    assert not device.exists("/Inbox/Mine.epub")


async def test_stale_success_is_real_firmware_behaviour(device):
    """A request without a file part repeats the last success (documented quirk)."""
    import aiohttp

    async with DeviceClient(device.host) as c:
        await c.makedirs("/Inbox")
        await c.upload("/Inbox", "first.epub", b"1")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{device.url}/upload?path=/Inbox", data=b"") as r:
            assert r.status == 200 and (await r.text()) == "File uploaded successfully: first.epub"


async def test_upload_connection_lost_after_store_is_not_duplicated(device, client):
    await client.makedirs("/Inbox")
    device.inject("POST", "/upload", reset_after=True)  # stored, but the answer is lost
    assert await client.upload("/Inbox", "a.epub", b"abc") == "a.epub"
    assert device.listdir("/Inbox") == ["a.epub"]
    assert len(device.requests_to("POST", "/upload")) == 1


async def test_upload_connection_lost_before_store_is_retried(device, client):
    await client.makedirs("/Inbox")
    device.inject("POST", "/upload", reset=True)
    assert await client.upload("/Inbox", "a.epub", b"abc") == "a.epub"
    assert device.read("/Inbox/a.epub") == b"abc"
    assert len(device.requests_to("POST", "/upload")) == 2


async def test_upload_device_vanishes(device, client):
    await client.makedirs("/Inbox")
    device.vanish_after_uploads = 0
    with pytest.raises(DeviceUnreachable):
        await client.upload("/Inbox", "a.epub", b"abc")
    assert not device.online


async def test_upload_disk_full_cleans_partial_file(device, client):
    await client.makedirs("/Inbox")
    device.disk_full = True
    with pytest.raises(DeviceRejected) as exc:
        await client.upload("/Inbox", "a.epub", b"abcdef")
    assert "disk may be full" in str(exc.value)
    assert device.listdir("/Inbox") == []  # 1.6.5 left a truncated file; the client removed it


async def test_upload_size_mismatch_is_rejected(device, client):
    """The device claims success but the listing shows a truncated file: remove it, fail."""
    await client.makedirs("/Inbox")
    device.write("/Inbox/b.epub", b"short")  # what the card holds after a bad write
    device.inject("POST", "/upload", status=200, body="File uploaded successfully: b.epub")
    with pytest.raises(DeviceRejected) as exc:
        await client._upload_as("/Inbox", "b.epub", b"much longer data")
    assert "expected 16" in str(exc.value)
    assert not device.exists("/Inbox/b.epub")


# ------------------------------------------------------------------ delete/WebDAV


async def test_download(device, client):
    device.write("/Inbox/a.epub", b"\x00binary\r\n")
    assert await client.download("/Inbox/A.epub") == b"\x00binary\r\n"
    with pytest.raises(DeviceRejected) as exc:
        await client.download("/Inbox/missing.epub")
    assert exc.value.status == 404
    device.write("/.crosspoint/settings.json", b"{}")
    with pytest.raises(DeviceRejected) as exc:
        await client.download("/.crosspoint/.secret")
    assert exc.value.status == 403


async def test_delete(device, client):
    device.write("/Inbox/a.epub", b"x")
    assert await client.delete("/Inbox/a.epub") is True
    assert not device.exists("/Inbox/a.epub")
    assert await client.delete("/Inbox/a.epub") is True  # already gone
    device.write("/Inbox/full/x", b"x")
    assert await client.delete("/Inbox/full") is False  # folder not empty


async def test_webdav_put_overwrites_without_library_dirty(device, client):
    await client.makedirs("/plugins/demo")
    device.clear_library_dirty()
    assert await client.webdav_put("/plugins/demo/config.json", b"{}") is True
    assert await client.webdav_put("/plugins/demo/config.json", b'{"a":1}') is False  # 204 replaced
    assert device.read("/plugins/demo/config.json") == b'{"a":1}'
    assert not device.library_dirty
    with pytest.raises(ValueError):
        await client.webdav_put("/.crosspoint/x.json", b"{}")
    with pytest.raises(DeviceRejected):
        await client.webdav_put("/missing/parent.json", b"{}")


# ------------------------------------------------------------------------ plugins


async def test_plugins_enabled_probe(device, client):
    if device.family == "1.6.5":
        assert await client.plugins_enabled() is None
        assert await client.ensure_plugins_enabled() is False
        return
    assert await client.plugins_enabled() is False
    device.plugins_enabled = True
    assert await client.plugins_enabled() is True
    assert device.listdir("/.crosspoint") == []  # the probe wrote nothing


async def test_ensure_plugins_enabled_cannot_switch_remotely(tmp_path):
    async with FakeCrossPoint(tmp_path / "sd", version="2.0") as dev, DeviceClient(dev.host) as c:
        assert await c.ensure_plugins_enabled() is False
        assert dev.plugins_enabled is False
        assert "pluginsEnabled" not in await c.settings_values()  # webHidden on 2.x
        assert await c.set_settings({"pluginsEnabled": 1}) == 0  # ignored by the firmware
        dev.plugins_enabled = True
        assert await c.ensure_plugins_enabled() is True


PLUGIN_PATH = "/.crosspoint/plugins/sendtox3/device.json"


async def test_write_plugin_file_plugin_fs(tmp_path):
    async with FakeCrossPoint(tmp_path / "sd", version="2.0", plugins_enabled=True) as dev, \
            DeviceClient(dev.host) as c:
        assert await c.write_plugin_file("sendtox3", PLUGIN_PATH, b'{"v":1}') == "plugin-fs"
        assert await c.write_plugin_file("sendtox3", PLUGIN_PATH, b'{"v":2}') == "plugin-fs"  # atomic replace
        assert dev.read(PLUGIN_PATH) == b'{"v":2}'
        assert dev.requests_to("POST", "/upload") == []
        # Too big for plugin-fs (256 KiB): falls back to the file manager.
        big = b"x" * (256 * 1024 + 1)
        assert await c.write_plugin_file("sendtox3", "/.crosspoint/plugins/sendtox3/big.bin", big) == "upload"
        assert dev.read("/.crosspoint/plugins/sendtox3/big.bin") == big


@pytest.mark.parametrize("version, plugins", [("1.6.5", False), ("2.0", False)])
async def test_write_plugin_file_fallbacks(tmp_path, version, plugins):
    async with FakeCrossPoint(tmp_path / "sd", version=version, plugins_enabled=plugins) as dev, \
            DeviceClient(dev.host) as c:
        # Hidden plugin root: WebDAV refuses dot segments, so /delete + /upload is used.
        assert await c.write_plugin_file("sendtox3", PLUGIN_PATH, b'{"v":1}') == "upload"
        assert await c.write_plugin_file("sendtox3", PLUGIN_PATH, b'{"v":22}') == "upload"
        assert dev.read(PLUGIN_PATH) == b'{"v":22}'
        assert dev.listdir("/.crosspoint/plugins/sendtox3") == ["device.json"]
        # Visible plugin root: WebDAV PUT overwrites in place.
        assert await c.write_plugin_file("sendtox3", "/plugins/sendtox3/manifest.json", b"{}") == "webdav"
        assert await c.write_plugin_file("sendtox3", "/plugins/sendtox3/manifest.json", b'{"t":1}') == "webdav"
        assert dev.read("/plugins/sendtox3/manifest.json") == b'{"t":1}'


async def test_write_plugin_file_validation(client):
    for bad_path in ("relative/x.json", "/a/../b.json", "/.crosspoint/wifi.json", "/.CROSSPOINT/opds.json"):
        with pytest.raises(ValueError):
            await client.write_plugin_file("sendtox3", bad_path, b"{}")
    with pytest.raises(ValueError):
        await client.write_plugin_file("send/tox3", PLUGIN_PATH, b"{}")
    with pytest.raises(ValueError):
        await client.write_plugin_file("sendtox3", PLUGIN_PATH, b"")


async def test_plugin_fs_bad_path_is_an_error(tmp_path):
    async with FakeCrossPoint(tmp_path / "sd", version="2.0", plugins_enabled=True) as dev, \
            DeviceClient(dev.host) as c:
        dev.inject("POST", "/api/plugin-fs", status=400, body='{"error":"bad path"}')
        with pytest.raises(DeviceRejected):
            await c.write_plugin_file("sendtox3", PLUGIN_PATH, b"{}")


# ----------------------------------------------------------------------- settings


async def test_settings_roundtrip(device, client):
    settings = await client.get_settings()
    by_key = {s["key"]: s for s in settings}
    assert by_key["sleepTimeoutMinutes"]["type"] == "value" and by_key["sleepTimeoutMinutes"]["max"] == 31
    assert await client.set_settings({"sleepTimeoutMinutes": 15, "showHiddenFiles": True, "nope": 1}) == 2
    values = await client.settings_values()
    assert values["sleepTimeoutMinutes"] == 15 and values["showHiddenFiles"] == 1
    assert await client.set_settings({"sleepTimeoutMinutes": 99}) == 0  # out of range: ignored
    # The setting really drives the listing, like on the device.
    device.write("/.dotfile", b"x")
    assert ".dotfile" in [e.name for e in await client.list_files("/")]
    posted = device.requests_to("POST", "/api/settings")[0]
    assert posted.headers["Content-Type"] == "application/json"


# -------------------------------------------------------------------- transport


async def test_one_request_at_a_time_and_connection_close(device):
    async with DeviceClient(device.host) as a, DeviceClient(device.host) as b:
        await asyncio.gather(*(c.list_files("/") for c in (a, b) for _ in range(5)), a.status(), b.status())
    assert device.max_in_flight == 1
    assert all(r.headers.get("Connection", "").lower() == "close" for r in device.requests)


async def test_rejected_listing_status(device, client):
    device.inject("GET", "/api/files", status=500, body="boom")
    with pytest.raises(DeviceRejected) as exc:
        await client.list_files("/")
    assert exc.value.status == 500


async def test_query_encoding_round_trips(device, client):
    folder = "/A + B & C=D %20 é"
    assert await client.makedirs(folder) is True
    assert device.resolve(folder).is_dir()
    assert await client.upload(folder, "x+y.epub", b"1") == "x+y.epub"
    assert device.read(folder + "/x+y.epub") == b"1"
    q = device.requests_to("GET", "/api/files")[-1].query
    assert q["path"] == folder


def test_fake_records_json_compactly():
    # Guard for other tests: the fake's JSON looks like ArduinoJson's (no spaces).
    from fakes.crosspoint_server import _compact

    assert _compact({"a": 1, "b": [True]}) == '{"a":1,"b":[true]}'
    assert json.loads(_compact({"name": "é"})) == {"name": "é"}
