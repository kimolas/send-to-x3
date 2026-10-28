"""Unit tests for the firmware e2e harness (e2e/sim, e2e/wire, e2e/scenarios, mock backend).

Nothing here needs a built simulator or the internet. The opt-in tests at the bottom
run real scenarios when ``SENDTOX3_E2E_SIM=1`` and the simulators are built
(``e2e/sim/build_sim.sh 2.0.0rc``).
"""

from __future__ import annotations

import base64
import io
import json
import os
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from e2e import mock_backend  # noqa: E402
from e2e.scenarios import REGISTRY  # noqa: E402
from e2e.scenarios.backends import BackendConfig, MockBackend  # noqa: E402
from e2e.scenarios.base import Context, Result, Scenario, SkipScenario, epub_info, execute, valid_epub  # noqa: E402
from e2e.scenarios.wiretap import WireTap, parse_requests, parse_responses  # noqa: E402
from e2e.sim import images, runner, sd  # noqa: E402
from e2e.sim.firmware import FIRMWARES, HOME_RECENT_ROWS, firmware  # noqa: E402
from e2e.sim.nav import Nav, Waits  # noqa: E402
from e2e.sim.runner import InputScript  # noqa: E402
from e2e.wire import wire  # noqa: E402

FW2 = firmware("2.0.0rc")
FW1 = firmware("1.6.5")
DEV = firmware("develop")
FORK = firmware("fork")


# ---------------------------------------------------------------------------------------
# firmware profiles + navigation
# ---------------------------------------------------------------------------------------


def test_home_menu_order_matches_firmware():
    assert FW2.home_items(plugins_enabled=True, has_opds_servers=False) == [
        "browse_files", "library", "plugins", "file_transfer", "settings"]
    # 2.x without the plugin switch: the slot shows the OPDS browser only with a server.
    assert FW2.home_items(plugins_enabled=False, has_opds_servers=False) == [
        "browse_files", "library", "file_transfer", "settings"]
    assert FW2.home_items(plugins_enabled=False, has_opds_servers=True)[2] == "opds"
    assert FW1.home_items(plugins_enabled=True, has_opds_servers=True) == [
        "browse_files", "library", "opds", "file_transfer", "settings"]
    assert FW1.home_items(plugins_enabled=True, has_opds_servers=False)[2] == "file_transfer"


def test_plugin_picker_rows():
    assert FW2.plugin_picker_rows(has_opds_servers=True, hub_row=False, plugin_titles=["b", "Send to X3", "a"]) == [
        "OPDS Browser", "a", "b", "Send to X3"]
    with pytest.raises(ValueError):
        FW1.plugin_picker_rows(has_opds_servers=False, hub_row=False, plugin_titles=[])


def test_unknown_firmware():
    with pytest.raises(KeyError, match="known"):
        firmware("9.9.9")
    assert set(FIRMWARES) >= {"2.0.0rc", "1.6.5"}
    assert not FW2.cold_sleep_delivery and FIRMWARES["3f387a9"].cold_sleep_delivery


def test_develop_and_fork_profiles():
    """Every plugin build sends limit=9 with the app's device.json; only develop and the
    fork fill in {offset}; only the fork opens downloads and relaunches on restart."""
    plugin_builds = [f for f in FIRMWARES.values() if f.plugins]
    assert {f.ref for f in plugin_builds} == {"2.0.0rc", "3f387a9", "develop", "fork"}
    assert all(f.catalog_limit == 9 for f in plugin_builds)
    assert FW2.catalog_query() == "page=1&limit=9&offset={offset}"
    assert firmware("3f387a9").catalog_query(2) == "page=2&limit=9&offset={offset}"
    assert DEV.catalog_query() == "page=1&limit=9&offset=0" and DEV.catalog_query(3) == "page=3&limit=9&offset=16"
    assert FORK.catalog_query(2) == DEV.catalog_query(2)
    assert [f.ref for f in FIRMWARES.values() if f.opens_after_download] == ["fork"]
    assert [f.ref for f in FIRMWARES.values() if f.sim_restart] == ["fork"]
    assert FORK.base == "develop" and DEV.cold_sleep_delivery and FORK.cold_sleep_delivery
    assert FORK.version == DEV.version == "2.0.0" and FORK.epub_reader_activity == "EpubReader"


def test_build_sim_pins_match_the_firmware_dir():
    """build_sim.sh's develop pin is the fork's base (firmware/UPSTREAM), both on the newer
    simulator; the fork's simulator patches are the ones in e2e/sim/patches."""
    script = (REPO / "e2e" / "sim" / "build_sim.sh").read_text()
    upstream = (REPO / "firmware" / "UPSTREAM").read_text()
    commit = next(ln.split("=", 1)[1].strip() for ln in upstream.splitlines() if ln.startswith("COMMIT="))
    assert f'DEVELOP_SHA="{commit}"' in script
    assert 'develop|fork|8f4d035*) SIM_REF="5b380779ddea5f1549f649eab0480e69e30a77b6"' in script
    assert '2.0.0rc) SIM_REF="1777fe2f1da1405cf5ecf191b831734e8862a92f"' in script
    assert sorted(p.name for p in (REPO / "e2e" / "sim" / "patches" / "simulator").glob("*.patch")) == [
        "0001-real-software-restart.patch"]
    out = subprocess.run(["bash", str(REPO / "e2e" / "sim" / "build_sim.sh"), "--help"], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0 and "--fw-patch" in out.stdout and "--sim-patch" in out.stdout
    bad = subprocess.run(["bash", str(REPO / "e2e" / "sim" / "build_sim.sh"), "--fw-patch", "/no/such.patch",
                          "fork"], capture_output=True, text=True, timeout=30)
    assert bad.returncode != 0 and "no such patch file" in bad.stderr


def test_simulator_restart_patch_matches_the_runner():
    """The patch speaks the runner's protocol: the opt-in variable, the after-restart
    schedules, the relaunch and restore lines; and the listening sockets close on exec."""
    patch = (REPO / "e2e" / "sim" / "patches" / "simulator" / "0001-real-software-restart.patch").read_text()
    for needle in ('"CROSSPOINT_SIM_RESTART"', '"CROSSPOINT_SIM_INPUT_SCRIPT_AFTER_RESTART"',
                   '"CROSSPOINT_SIM_SCREENSHOTS_AFTER_RESTART"', "[SIM] software restart: relaunching (rtc %zu bytes)",
                   "[SIM] software restart: boot %s, restored %zu/%zu rtc bytes"):
        assert needle in patch
    assert runner._RELAUNCH_RE.match("[SIM] software restart: relaunching (rtc 4124 bytes)")
    files = [ln.split(" b/")[1] for ln in patch.splitlines() if ln.startswith("diff --git ")]
    assert {"src/WebServer.cpp", "src/WebSocketsServer.cpp", "src/CrossPointWebServer.cpp", "src/Arduino.h",
            "src/SimulatorLifecycle.cpp", "src/esp_system.h", "src/simulator_main.cpp"} <= set(files)
    assert patch.count("F_SETFD, FD_CLOEXEC") == 3


def test_input_script_schedule():
    s = InputScript(start_ms=1000, gap_ms=400)
    s.press("down").press("ENTER", wait_ms=2000).shot("a").hold("POWER", 1500).quit(wait_ms=100)
    spec = s.input_spec().split(";")
    assert spec[0] == "1000:DOWN" and spec[1] == "1400:ENTER"
    assert spec[2] == "3450:POWER:1500"  # 1400 + 2000 wait, then the 50 ms screenshot slot
    assert spec[-1].endswith(":QUIT")
    assert s.shots[0].at_ms == 3400
    assert s.screenshot_spec(Path("/x")) == "3400:/x/a.bmp"
    with pytest.raises(RuntimeError):
        s.press("UP")
    with pytest.raises(ValueError):
        InputScript().press("JUMP")
    with pytest.raises(ValueError):
        InputScript().shot("bad name")
    t = InputScript()
    t.shot("x")
    with pytest.raises(ValueError):
        t.shot("x")


def test_input_script_hold_never_overlaps_next_press():
    s = InputScript(start_ms=0, gap_ms=200)
    s.hold("POWER", 1000)
    s.press("ENTER")
    assert s.input_spec() == "0:POWER:1000;1120:ENTER"


def test_waits_scaling(monkeypatch):
    assert Waits().scaled(2).download == 2 * Waits().download
    monkeypatch.setenv("SENDTOX3_E2E_SLOWDOWN", "1.5")
    assert Waits.from_env().key == int(Waits().key * 1.5)
    assert InputScript().gap_ms == int(450 * 1.5)
    with pytest.raises(ValueError):
        Waits().scaled(0)


def test_nav_home_and_plugin(tmp_path):
    s = InputScript(start_ms=0, gap_ms=200)
    nav = Nav(FW2, s, waits=Waits())
    nav.open_plugin("Send to X3", plugin_titles=["Send to X3"])
    keys = [x.split(":")[1] for x in s.input_spec().split(";")]
    assert keys == ["DOWN", "DOWN", "ENTER", "ENTER"]  # Home row 2 = Plugins; picker row 0
    s2 = InputScript(start_ms=0, gap_ms=200)
    Nav(FW1, s2, waits=Waits()).file_transfer_join()
    assert [x.split(":")[1] for x in s2.input_spec().split(";")] == ["DOWN", "DOWN", "ENTER", "ENTER"]
    s3 = InputScript(start_ms=0, gap_ms=200)
    Nav(FW2, s3, recent_books=2, waits=Waits()).home("settings")
    assert [x.split(":")[1] for x in s3.input_spec().split(";")].count("DOWN") == 6
    with pytest.raises(ValueError):
        Nav(FW1, InputScript()).home("plugins")
    with pytest.raises(ValueError):
        Nav(FW1, InputScript()).open_opds()


def test_nav_on_the_fork_never_dismisses():
    """On the fork Confirm opens the book, so there is no "Download complete" to dismiss,
    and the fetch waits for the book to open."""
    w = Waits()
    s = InputScript(start_ms=0, gap_ms=200)
    Nav(FORK, s, waits=w).catalog_fetch(0)
    s.press("ENTER")
    assert s.input_spec() == f"0:ENTER;{w.download + w.open_book}:ENTER"
    with pytest.raises(ValueError, match="no \"Download complete\""):
        Nav(FORK, InputScript(), waits=w).dismiss()
    s2 = InputScript(start_ms=0, gap_ms=200)
    Nav(DEV, s2, waits=w).catalog_fetch(0).dismiss()
    assert s2.input_spec() == f"0:ENTER;{w.download}:ENTER"
    # After a book was opened, Home lists one recent book above its menu.
    s3 = InputScript(start_ms=0, gap_ms=200)
    Nav(FORK, s3, recent_books=HOME_RECENT_ROWS, waits=w).home("plugins")
    assert [x.split(":")[1] for x in s3.input_spec().split(";")] == ["DOWN"] * 3 + ["ENTER"]
    # A device.json without download.open (S1 on the fork): the stock "Download complete".
    s4 = InputScript(start_ms=0, gap_ms=200)
    Nav(FORK, s4, catalog_opens=False, waits=w).catalog_fetch(0).dismiss()
    assert s4.input_spec() == s2.input_spec()
    assert not Nav(DEV, InputScript(), catalog_opens=True).catalog_opens  # develop never reads the key


def test_nav_open_file_uses_card_order(tmp_path):
    card = sd.build_sd(tmp_path / "sd", FW2, files={
        "/Inbox/b10.txt": "x", "/Inbox/b9.txt": "x", "/Inbox/A.epub": "x", "/Inbox/notes.pdf": "x",
        "/Books/z.epub": "x", "/.hidden/x.epub": "x"})
    assert sd.browser_listing(card.root, "/", FW2) == ["Books/", "Inbox/"]
    assert sd.browser_listing(card.root, "/Inbox", FW2) == ["A.epub", "b9.txt", "b10.txt"]
    s = InputScript(start_ms=0, gap_ms=200)
    Nav(FW2, s, waits=Waits()).open_file(card.root, "/Inbox/b10.txt")
    keys = [x.split(":")[1] for x in s.input_spec().split(";")]
    # Home row 0 (Browse Files), root row 1 (Inbox/), Inbox row 2.
    assert keys == ["ENTER", "DOWN", "ENTER", "DOWN", "DOWN", "ENTER"]
    with pytest.raises(FileNotFoundError):
        Nav(FW2, InputScript()).open_file(card.root, "/Inbox/notes.pdf")


@pytest.mark.parametrize("a,b,less", [
    ("a2", "a10", True), ("a10", "a2", False), ("A", "a", False), ("a", "B", True),
    ("x007", "x7", False), ("x7", "x007", False), ("ab", "abc", True), ("", "a", True),
    ("Inbox/", "inbox2/", True), ("café", "cafe", False),
])
def test_natural_less(a, b, less):
    assert sd.natural_less(a, b) is less


# ---------------------------------------------------------------------------------------
# SD card builder
# ---------------------------------------------------------------------------------------


def test_spec_device_json_contract():
    """The browse URL pages on every firmware (``{offset}`` for develop, the literal text
    elsewhere), downloads ask to open (the fork) and nothing else changed."""
    for auto in (False, True):
        dj = sd.spec_device_json("http://h:1", "tok", auto_delivery=auto)
        assert dj["browse"]["url"] == "http://h:1/device/v1/catalog?page={page}&limit={limit}&offset={offset}"
        assert dj["browse"]["page_size"] == sd.PAGE_SIZE == mock_backend.PAGE_SIZE == 8
        assert dj["download"]["open"] is True
        assert dj["download"]["url"] == "http://h:1/device/v1/items/{id}.epub" + ("" if auto else "?confirm=1")
        assert list(dj["download"]) == ["url", "headers", "dest_dir", "filename", "open"]
        assert list(dj["events"]) == (["sleep.enter"] if auto else ["book.downloaded"])


def test_spec_bundle_matches_the_app_by_default():
    """The §10 template the harness falls back to is the app's default plugin: download
    report and ``?confirm=1``, no sleep delivery; the same browse URL, ``download.open``
    and page size."""
    from sendtox3 import delivery
    from sendtox3.config import Options
    from sendtox3.plugin_bundle import build_device_json

    for auto in (False, True):
        assert sd.spec_device_json("http://h:1", "tok", auto_delivery=auto) == build_device_json(
            base_url="http://h:1", device_token="tok", opts=Options(auto_delivery=auto))
    assert getattr(delivery, "PAGE_SIZE", None) == sd.PAGE_SIZE


def test_build_sd_formats(tmp_path):
    bundle = sd.spec_bundle_zip("http://127.0.0.1:9", "tok", auto_delivery=True)
    card = sd.build_sd(tmp_path / "sd", FW2, bundle=bundle, opds_servers=[
        sd.OpdsServer("S", "http://127.0.0.1:9/opds", "x3", "tok")], settings={"fontSize": 16})
    st = card.settings()
    assert st["pluginsEnabled"] == 1 and st["pluginHubPromptHidden"] == 1
    assert st["opdsDownloadFolder"] == "/Inbox" and st["fontSize"] == 16
    wifi = json.loads(card.path("/.crosspoint/wifi.json").read_text())
    assert wifi["lastConnectedSsid"] == wifi["credentials"][0]["ssid"] == "Simulator WiFi (fake)"
    assert wifi["credentials"][0]["password"] == ""
    opds = json.loads(card.path("/.crosspoint/opds.json").read_text())
    assert opds == {"servers": [{"name": "S", "url": "http://127.0.0.1:9/opds", "username": "x3", "password": "tok"}]}
    assert card.plugin_dir == "/.crosspoint/plugins/sendtox3"
    dj = json.loads(card.path(card.plugin_dir + "/device.json").read_text())
    assert dj["download"]["url"] == "http://127.0.0.1:9/device/v1/items/{id}.epub"
    assert dj["events"]["sleep.enter"]["download"]["dest"] == "/Inbox/{event.ts}-{event.id}.epub"
    assert card.path("/Inbox").is_dir() and card.inbox_files() == {}
    assert card.outbox() == []
    card.path(card.plugin_dir + "/events.jsonl").write_text('{"e":"sleep.enter","id":"a-1"}\nnot json\n')
    assert card.outbox() == [{"e": "sleep.enter", "id": "a-1"}, {"_unparsed": "not json"}]


def test_build_sd_edits_device_json(tmp_path):
    """``device_json`` edits the installed manifest: S1 drops download.open on the fork."""
    bundle = sd.spec_bundle_zip("http://127.0.0.1:9", "tok")
    card = sd.build_sd(tmp_path / "sd", FORK, bundle=bundle, device_json=sd.drop_download_open)
    dj = json.loads(card.path(card.plugin_dir + "/device.json").read_text())
    assert "open" not in dj["download"]
    assert dj == {**sd.spec_device_json("http://127.0.0.1:9", "tok"),
                  "download": {k: v for k, v in sd.spec_device_json("http://127.0.0.1:9", "tok")["download"].items()
                               if k != "open"}}
    sd.drop_download_open({})  # nothing to drop
    with pytest.raises(ValueError, match="needs a plugin"):
        sd.build_sd(tmp_path / "sd2", FORK, device_json=sd.drop_download_open)


def test_build_sd_165_has_no_plugin_keys(tmp_path):
    card = sd.build_sd(tmp_path / "sd", FW1, wifi=False, inbox=None)
    assert "pluginsEnabled" not in card.settings()
    assert not card.path("/.crosspoint/wifi.json").exists() and not card.path("/Inbox").exists()
    with pytest.raises(ValueError):
        sd.build_sd(tmp_path / "sd2", FW1, bundle=sd.spec_bundle_zip("http://h", "t"))


def test_build_sd_is_clean_and_validates(tmp_path):
    root = tmp_path / "sd"
    sd.build_sd(root, FW2, files={"/old.txt": "x"})
    sd.build_sd(root, FW2)
    assert not (root / "old.txt").exists()
    with pytest.raises(ValueError):
        sd.build_sd(tmp_path / "x", FW2, opds_download_folder="Inbox")
    with pytest.raises(ValueError):
        sd.build_sd(tmp_path / "y", FW2, opds_servers=[sd.OpdsServer(str(i), "u") for i in range(9)])


@pytest.mark.parametrize("name", ["../evil", "/abs/file", "a/../../b"])
def test_extract_bundle_rejects_unsafe_entries(tmp_path, name):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        z.writestr(name, b"x")
    with pytest.raises(ValueError):
        sd.extract_bundle(tmp_path, bio.getvalue())


def test_sd_path_guard(tmp_path):
    assert sd.sd_path(tmp_path, "/a/./b") == tmp_path / "a" / "b"
    for bad in ("relative", "/a/../../x"):
        with pytest.raises(ValueError):
            sd.sd_path(tmp_path, bad)


def test_snapshot_and_diff(tmp_path):
    sd.write_file(tmp_path, "/a.txt", "1")
    sd.write_file(tmp_path, "/.h/b.txt", "2")
    before = sd.snapshot(tmp_path)
    assert set(before) == {"/a.txt", "/.h/b.txt"}
    assert set(sd.snapshot(tmp_path, include_hidden=False)) == {"/a.txt"}
    sd.write_file(tmp_path, "/a.txt", "changed")
    sd.write_file(tmp_path, "/c.txt", "3")
    (tmp_path / ".h" / "b.txt").unlink()
    d = sd.diff(before, sd.snapshot(tmp_path))
    assert set(d.added) == {"/c.txt"} and set(d.changed) == {"/a.txt"} and set(d.removed) == {"/.h/b.txt"}
    assert d and not sd.diff(before, before)


# ---------------------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------------------


def _bmp(width, height, pixel, *, bpp=24, top_down=False, bitfields=False):
    step = bpp // 8
    stride = (width * step + 3) & ~3
    rows = []
    for y in range(height):
        row = b"".join(pixel(x, y) for x in range(width))
        rows.append(row + b"\0" * (stride - len(row)))
    if not top_down:
        rows.reverse()
    data = b"".join(rows)
    header_size = 40
    extra = struct.pack("<III", 0xFF0000, 0xFF00, 0xFF) if bitfields else b""
    offset = 14 + header_size + len(extra)
    info = struct.pack("<IiiHHIIiiII", header_size, width, -height if top_down else height, 1, bpp,
                       3 if bitfields else 0, len(data), 2835, 2835, 0, 0)
    return b"BM" + struct.pack("<IHHI", offset + len(data), 0, 0, offset) + info + extra + data


@pytest.mark.parametrize("bpp,top_down,bitfields", [(24, False, False), (32, True, True), (32, False, False)])
def test_bmp_decode_and_png(tmp_path, bpp, top_down, bitfields):
    # Left half black, right half white; a red pixel at (0, 0) to check orientation.
    def px(x, y):
        if (x, y) == (0, 0):
            return b"\x00\x00\xff" + (b"\xff" if bpp == 32 else b"")
        v = b"\x00\x00\x00" if x < 2 else b"\xff\xff\xff"
        return v + (b"\xff" if bpp == 32 else b"")

    p = tmp_path / "s.bmp"
    p.write_bytes(_bmp(4, 3, px, bpp=bpp, top_down=top_down, bitfields=bitfields))
    g = images.read_bmp(p)
    assert (g.width, g.height) == (4, 3)
    assert g.pixels[0] == (299 * 255) // 1000  # red luma at the top-left
    assert g.pixels[1] == 0 and g.pixels[3] == 255
    assert abs(g.ink_ratio() - 6 / 12) < 1e-9
    assert g.region(2, 0, 4, 3).ink_ratio() == 0.0
    png = images.bmp_to_png(p)
    raw = png.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n" and b"IHDR" in raw and raw.endswith(b"IEND\xaeB`\x82")
    idat = raw[raw.index(b"IDAT") + 4: raw.index(b"IEND") - 8]
    assert zlib.decompress(idat)[:5] == b"\x00" + g.pixels[:4]
    other = images.Gray(4, 3, bytes(255 - v for v in g.pixels))
    assert images.difference(g, g) == 0.0 and images.difference(g, other) > 0.5
    assert images.difference(g, images.Gray(1, 1, b"\0")) == 1.0


def test_bmp_rejects_garbage(tmp_path):
    p = tmp_path / "x.bmp"
    p.write_bytes(b"PK not a bmp" * 10)
    with pytest.raises(ValueError):
        images.read_bmp(p)


# ---------------------------------------------------------------------------------------
# wire tap
# ---------------------------------------------------------------------------------------


def test_parse_requests_and_responses():
    up = (b"GET /a HTTP/1.1\r\nHost: h\r\n\r\n"
          b"POST /b HTTP/1.1\r\nContent-Length: 3\r\n\r\nxyz"
          b"HEAD /c HTTP/1.1\r\n\r\n")
    reqs = parse_requests(up)
    assert [r.start_line for r in reqs] == ["GET /a HTTP/1.1", "POST /b HTTP/1.1", "HEAD /c HTTP/1.1"]
    assert reqs[1].body == b"xyz" and reqs[0].header("host") == "h"
    down = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"
            b"HTTP/1.1 404 Not Found\r\nContent-Length: 2\r\n\r\nno"
            b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n")
    resps = parse_responses(down, ["GET", "POST", "HEAD"], closed=True)
    assert resps[0].body == b"abcde" and resps[0].complete
    assert resps[1].body == b"no"
    assert resps[2].body == b"" and resps[2].complete  # HEAD has no body
    close_delim = parse_responses(b"HTTP/1.0 200 OK\r\n\r\nbody", ["GET"], closed=True)
    assert close_delim[0].body == b"body" and close_delim[0].complete
    assert not parse_responses(b"HTTP/1.1 200 OK\r\n\r\nbo", ["GET"], closed=False)[0].complete
    short = parse_responses(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nabc", ["GET"], closed=True)
    assert not short[0].complete


class _Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = f"you asked {self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: D401
        pass


def test_wiretap_relays_and_records():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with WireTap(srv.server_address[1]) as tap:
            with urllib.request.urlopen(tap.url + "/hello?x=1", timeout=5) as r:
                assert r.read() == b"you asked /hello?x=1"
            ex = tap.wait_for("/hello")
            assert ex and ex[0].status == 200 and ex[0].method == "GET" and ex[0].path == "/hello"
            assert ex[0].response.body == b"you asked /hello?x=1"
            assert ex[0].summary()["response_body_bytes"] == len(b"you asked /hello?x=1")
            tap.clear()
            assert tap.exchanges() == []
    finally:
        srv.shutdown()


def test_wiretap_upstream_down():
    port = runner.free_port()
    with WireTap(port) as tap:
        with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(tap.url + "/x", timeout=3)


# ---------------------------------------------------------------------------------------
# mock backend
# ---------------------------------------------------------------------------------------


@pytest.fixture
def mock(tmp_path):
    log = tmp_path / "req.jsonl"
    srv, t, _ = mock_backend.serve(ingest_token="ing", device_token="dev", log_path=str(log))
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, srv, log
    srv.shutdown()


def _req(url, *, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _ingest(base, **doc):
    st, body, _ = _req(base + "/api/v1/ingest", method="POST", data=json.dumps(doc).encode(),
                       headers={"Authorization": "Bearer ing", "Content-Type": "application/json"})
    assert st == 200, body
    return json.loads(body)


def test_mock_ingest_auth_and_titles(mock):
    base, srv, log = mock
    st, _, _ = _req(base + "/api/v1/ingest", method="POST", data=b"{}", headers={"Content-Type": "application/json"})
    assert st == 401
    st, _, _ = _req(base + "/api/v1/ingest?token=ing", method="POST", data=b"   ", headers={"Content-Type": "text/plain"})
    assert st == 422
    a = _ingest(base, title="Wait... what? a/b: {x} ~1", text="t")
    assert a["title"] == "Wait… what a-b - x - 1"
    b = _ingest(base, title="Wait... what? a/b: {x} ~1", text="t")
    assert b["title"] == a["title"] + " (2)"
    u = _ingest(base, text="https://example.com/p")
    assert u["title"] == "example.com"
    st, _, _ = _req(base + "/api/v1/ingest?token=ing", method="POST", data=b"text=hello+there",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert st == 200
    for ctype, data in (("application/json", b"[1, 2]"), ("application/json", b"{nope"),
                        ("multipart/form-data; boundary=x", b"--x--")):
        st, _, _ = _req(base + "/api/v1/ingest?token=ing", method="POST", data=data, headers={"Content-Type": ctype})
        assert st == 422, ctype
    long = _ingest(base, title="word " * 40, text="x")
    assert len(long["title"].encode()) <= 90
    assert len(log.read_text().splitlines()) >= 9


def test_mock_catalog_paging_and_downloads(mock):
    base, srv, _ = mock
    ids = [_ingest(base, title=f"Item {i}", text=f"Body {i}")["id"] for i in range(10)]
    auth = {"Authorization": "Bearer dev"}
    assert _req(base + "/device/v1/catalog?page=1&limit=9")[0] == 401
    st, body, hdr = _req(base + "/device/v1/catalog?page=1&limit=9", headers=auth)
    rows = json.loads(body)["items"]
    # "Get 10 new" first, then newest first; page_size + 1 rows
    assert st == 200 and len(rows) == 9 and rows[0]["title"].startswith("Get 10 new · ") and rows[1]["id"] == ids[-1]
    assert hdr["Content-Length"] == str(len(body)) and hdr["Connection"] == "close"
    st, body, _ = _req(base + "/device/v1/catalog?page=2&limit=9", headers=auth)
    rows2 = json.loads(body)["items"]
    assert [r["id"] for r in rows2] == [ids[2], ids[1], ids[0]]  # offset (P-1)*(L-1) = 8
    assert rows[1]["subtitle"].startswith("New · 2 words")
    st, epub, hdr = _req(base + f"/device/v1/items/{ids[0]}.epub", headers=auth)
    assert st == 200 and hdr["Content-Type"] == "application/epub+zip" and valid_epub(epub)
    st, txt, hdr = _req(base + f"/device/v1/items/{ids[0]}.txt", headers=auth)
    assert st == 200 and txt.decode().startswith("Item 0\n") and txt.endswith(b"\n") and b"\r" not in txt
    assert _req(base + "/device/v1/items/aaaaaaaaaa.epub", headers=auth)[0] == 404
    _, body, _ = _req(base + "/device/v1/catalog?page=2&limit=9", headers=auth)
    assert json.loads(body)["items"][2]["subtitle"].startswith("Read on X3")


def test_mock_catalog_paging_guard(mock):
    """offset when it is a number (develop); else (page-1)*limit for limit == 8 (develop with
    an old device.json); else (page-1)*(limit-1) (2.0.0rc/3f387a9 send the literal {offset},
    which the simulator's curl turns into "offset"). Every case lists the same rows."""
    base, srv, _ = mock
    ids = [_ingest(base, title=f"Item {i}", text=f"Body {i}")["id"] for i in range(20)]
    auth = {"Authorization": "Bearer dev"}
    rows = ["get"] + ids[::-1]  # the "Get 20 new" row, then newest first

    def page(query):
        st, body, _ = _req(base + "/device/v1/catalog?" + query, headers=auth)
        assert st == 200
        got = [r["id"] for r in json.loads(body)["items"]]
        return ["get" if i.startswith("all-") else i for i in got]

    for p in (1, 2, 3):
        want = rows[(p - 1) * 8:(p - 1) * 8 + 9]
        assert page(f"page={p}&limit=9&offset={(p - 1) * 8}") == want  # develop, the fork
        assert page(f"page={p}&limit=9&offset=%7Boffset%7D") == want  # 2.0.0rc, 3f387a9
        assert page(f"page={p}&limit=9&offset=offset") == want  # the same through curl
        assert page(f"page={p}&limit=9") == want  # a 0.2.0 device.json
        assert page(f"page={p}&limit=8") == rows[(p - 1) * 8:p * 8]  # develop with a 0.2.0 device.json
    assert page("page=1&limit=9&offset=-1") == rows[:9]  # not all digits: ignored
    assert page("page=3&limit=9&offset=20") == rows[20:]


def test_mock_fetch_row(mock):
    base, srv, _ = mock
    auth = {"Authorization": "Bearer dev"}
    rows = json.loads(_req(base + "/device/v1/catalog?page=1&limit=9", headers=auth)[1])["items"]
    assert rows == [{"id": "none", "title": "Nothing new right now",
                     "subtitle": "Share something to Send to X3, then open this list again."}]
    assert _req(base + "/device/v1/items/none.epub", headers=auth)[0] == 404
    ids = [_ingest(base, title=f"F{i}", text="x")["id"] for i in range(2)]
    top = json.loads(_req(base + "/device/v1/catalog?page=1&limit=9", headers=auth)[1])["items"][0]
    assert top["title"].startswith("Get 2 new · ") and top["id"].startswith("all-")
    assert top["subtitle"] == "Everything new in one book · 2 words: F1; F0"  # as the app words it: newest first
    st, d1, _ = _req(base + f"/device/v1/items/{top['id']}.epub", headers=auth)
    assert st == 200 and epub_info(d1)["chapters"] == 2
    assert _req(base + f"/device/v1/items/{top['id']}.epub", headers=auth)[1] == d1  # repeats are identical
    state = json.loads(_req(base + "/mock/state")[1])
    assert all("fetch" in i["delivered"] for i in state["items"] if i["id"] in ids)
    rows = json.loads(_req(base + "/device/v1/catalog?page=1&limit=9", headers=auth)[1])["items"]
    assert rows[0]["id"] == "none"


def test_mock_deliver_digest_idempotent_and_404(mock):
    base, srv, _ = mock
    auth = {"Authorization": "Bearer dev"}
    st, _, hdr = _req(base + "/device/v1/deliver?ev=aaaa0000-1&ts=10", headers=auth)
    assert st == 404 and hdr["Content-Length"] != "0"
    for i in range(3):
        _ingest(base, title=f"P{i}", text="x")
    st, d1, _ = _req(base + "/device/v1/deliver?ev=aaaa0000-2&ts=11", headers=auth)
    assert st == 200 and epub_info(d1)["chapters"] == 3 and epub_info(d1)["title"].startswith("Inbox: 3 items")
    st, d2, _ = _req(base + "/device/v1/deliver?ev=aaaa0000-2&ts=11", headers=auth)
    assert st == 200 and d2 == d1  # a retried (ev, ts) gets byte-identical output
    assert _req(base + "/device/v1/deliver?ev=aaaa0000-3&ts=12", headers=auth)[0] == 404
    assert _req(base + "/device/v1/deliver?ev=x", headers=auth)[0] == 400
    one = _ingest(base, title="Single", text="x")
    st, d3, _ = _req(base + "/device/v1/deliver?ev=aaaa0000-4&ts=13", headers=auth)
    assert st == 200 and epub_info(d3)["title"] == one["title"] and epub_info(d3)["chapters"] == 1


def test_mock_empty_deliver_status_is_configurable():
    srv, _, _ = mock_backend.serve(ingest_token="i", device_token="d", empty_deliver_status=204)
    try:
        st, body, _ = _req(f"http://127.0.0.1:{srv.server_address[1]}/device/v1/deliver?ev=a-1&ts=1",
                           headers={"Authorization": "Bearer d"})
        assert st == 204 and body == b""
    finally:
        srv.shutdown()


def test_mock_opds(mock):
    base, srv, _ = mock
    for i in range(52):
        _ingest(base, title=f"Book {i}", text="x")
    assert _req(base + "/opds")[0] == 401
    basic = {"Authorization": "Basic " + base64.b64encode(b"x3:dev").decode()}
    assert _req(base + "/opds", headers={"Authorization": "Basic " + base64.b64encode(b"x3:no").decode()})[0] == 401
    st, feed, hdr = _req(base + "/opds", headers=basic)
    text = feed.decode()
    # "Get 52 new" + 50 books
    assert st == 200 and text.count("<entry>") == 51 and 'rel="next"' in text and 'rel="previous"' not in text
    assert "<entry><title>Get 52 new · " in text
    assert 'type="application/epub+zip"' in text and 'href="/opds/items/' in text
    assert f'href="{base}/opds?page=2"' in text  # absolute paging links
    import xml.etree.ElementTree as ET

    ET.fromstring(feed)
    st, feed2, _ = _req(base + "/opds?page=2", headers=basic)
    assert feed2.decode().count("<entry>") == 2 and 'rel="previous"' in feed2.decode()
    item_id = text.split('href="/opds/items/')[2].split(".epub")[0]
    st, epub, _ = _req(base + f"/opds/items/{item_id}.epub", headers=basic)
    assert st == 200 and valid_epub(epub)
    fetch_id = text.split('href="/opds/items/')[1].split(".epub")[0]
    st, epub, _ = _req(base + f"/opds/items/{fetch_id}.epub", headers=basic)
    assert fetch_id.startswith("all-") and st == 200 and epub_info(epub)["chapters"] == 52  # as listed


def test_mock_epub_structure():
    st = mock_backend.State()
    item = st.add(title='A & B <c> "d"', paragraphs=["x & y", "z"], kind="text", byline="me")
    with zipfile.ZipFile(io.BytesIO(item.epub)) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
        assert not infos[0].extra
        assert z.read("META-INF/container.xml").count(b'media-type="application/oebps-package+xml"') == 1
        chapter = z.read("OEBPS/text/item-001.xhtml").decode()
        assert "&amp;" in chapter and "&nbsp;" not in chapter and "<pre" not in chapter and "<img" not in chapter
        nav = z.read("OEBPS/nav.xhtml").decode()
        assert 'epub:type="toc"' in nav
        opf = z.read("OEBPS/content.opf").decode()
        assert "<dc:language>en</dc:language>" in opf and 'idref="nav"' not in opf


def test_mock_lan_push_against_fake_device(tmp_path):
    files: dict[str, bytes] = {"notes.txt": b"old"}
    calls: list[str] = []

    class Device(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status, body):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            calls.append("GET " + self.path)
            if self.path.startswith("/api/status"):
                return self._send(200, b'{"version":"1.6.5"}')
            return self._send(200, json.dumps([{"name": n, "size": len(b)} for n, b in files.items()]).encode())

        def do_POST(self):  # noqa: N802
            calls.append("POST " + self.path)
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/mkdir":
                return self._send(400, b"Folder already exists")
            name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            if name.lower() in {n.lower() for n in files}:
                return self._send(400, f"File already exists: {name}".encode())
            data = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            files[name] = data
            return self._send(200, f"File uploaded successfully: {name}".encode())

        def log_message(self, *a):  # noqa: D401
            pass

    dev = ThreadingHTTPServer(("127.0.0.1", 0), Device)
    threading.Thread(target=dev.serve_forever, daemon=True).start()
    srv, _, pusher = mock_backend.serve(ingest_token="i", device_token="d",
                                        device_host=f"127.0.0.1:{dev.server_address[1]}", push_poll=0.2)
    try:
        item = srv.state.add(title="notes", paragraphs=["new"], kind="text")
        for _ in range(100):
            if "push" in item.delivered:
                break
            threading.Event().wait(0.05)
        assert "push" in item.delivered
        assert files["notes (2).txt"] == item.txt and files["notes.txt"] == b"old"
        assert any(c.startswith("POST /upload?path=/Inbox") for c in calls)
    finally:
        pusher.stop_event.set()
        srv.shutdown()
        dev.shutdown()


def test_mock_rejects_public_bind():
    with pytest.raises(ValueError):
        mock_backend.serve(host="0.0.0.0", ingest_token="i", device_token="d")


# ---------------------------------------------------------------------------------------
# backends + scenarios registry + orchestrator CLI
# ---------------------------------------------------------------------------------------


def test_mock_backend_process(tmp_path):
    cfg = BackendConfig(port=runner.free_port(), data_dir=tmp_path / "b", public_url="http://127.0.0.1:1")
    with MockBackend(cfg) as b:
        item = b.seed(title="Proc", text="hello")
        assert valid_epub(b.item_bytes(item["id"], "epub"))
        assert b.state()["items"][0]["id"] == item["id"]
        for source in ("spec", "app"):
            z = zipfile.ZipFile(io.BytesIO(b.bundle_zip(source=source)))
            dj = json.loads(z.read(".crosspoint/plugins/sendtox3/device.json"))
            assert dj["browse"]["url"].startswith("http://127.0.0.1:1/device/v1/catalog")
            assert dj["browse"]["headers"]["Authorization"] == f"Bearer {b.device_token}"
    assert b.proc.poll() is not None


def test_registry_and_cli():
    assert {"S1", "S2", "S3", "S4", "S5", "S6", "W1"} <= set(REGISTRY)
    assert set(REGISTRY["S3"].firmwares) >= {"2.0.0rc", "1.6.5"}
    assert all(set(sc.firmwares) <= set(FIRMWARES) for sc in REGISTRY.values())
    assert REGISTRY["S6"].firmwares == ("fork",)
    # S1 runs the fork's stock path (a device.json without download.open); S6 its open path.
    assert {"develop", "fork"} <= set(REGISTRY["S1"].firmwares)
    assert {"develop", "fork"} <= set(REGISTRY["W1"].firmwares)
    out = subprocess.run([sys.executable, str(REPO / "e2e" / "run_e2e.py"), "--list"], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0 and "S1" in out.stdout and "W1" in out.stdout
    bad = subprocess.run([sys.executable, str(REPO / "e2e" / "run_e2e.py"), "-s", "S9"], capture_output=True,
                         text=True, timeout=60)
    assert bad.returncode != 0 and "unknown scenario" in bad.stderr


def test_run_e2e_firmware_selection():
    sys.path.insert(0, str(REPO / "e2e"))
    try:
        import run_e2e
    finally:
        sys.path.remove(str(REPO / "e2e"))
    known = list(FIRMWARES)
    assert run_e2e.select("releases", known, "firmware") == ["2.0.0rc", "1.6.5"]
    assert run_e2e.select("releases,fork", known, "firmware") == ["2.0.0rc", "1.6.5", "fork"]
    assert run_e2e.select("develop, 2.0.0rc,releases", known, "firmware") == ["develop", "2.0.0rc", "1.6.5"]
    assert run_e2e.select("all", known, "firmware") == known
    with pytest.raises(SystemExit, match="unknown firmware: 9.9"):
        run_e2e.select("fork,9.9", known, "firmware")


def test_run_e2e_skips_missing_simulator(tmp_path):
    env = dict(os.environ, SENDTOX3_E2E_WORK=str(tmp_path / "empty-work"))
    out = subprocess.run([sys.executable, str(REPO / "e2e" / "run_e2e.py"), "-s", "S5", "-f", "1.6.5", "--out",
                          str(tmp_path / "o"), "--json"], capture_output=True, text=True, timeout=120, env=env)
    report = json.loads(out.stdout)
    assert out.returncode == 0 and report["results"][0]["status"] == "skip"
    assert "build_sim.sh 1.6.5" in report["results"][0]["error"]
    assert report["results"][0]["build"] == {}  # no build-info.json either


def _fake_build(work: Path, ref: str, **info) -> Path:
    """A work dir with ``bin/<ref>/program`` and the build-info.json build_sim.sh writes."""
    out = work / "bin" / ref
    out.mkdir(parents=True)
    (out / "program").write_text("#!/bin/sh\nexit 0\n")
    (out / "program").chmod(0o755)
    (out / "build-info.json").write_text(json.dumps({"name": ref, "firmware_ref": ref, **info}))
    return out


def _blobs(folder: Path) -> list[dict]:
    return [{"file": p.name, "blob": runner._blob(p)} for p in sorted(folder.glob("*.patch"))]


def test_build_provenance(tmp_path, monkeypatch):
    """A fork simulator counts as current only when it was built from firmware/UPSTREAM
    and exactly the patches in firmware/patches and e2e/sim/patches/simulator."""
    monkeypatch.setenv("SENDTOX3_E2E_WORK", str(tmp_path))
    fw_patches = _blobs(REPO / "firmware" / "patches")
    sim_patches = _blobs(REPO / "e2e" / "sim" / "patches" / "simulator")
    want = runner.expected_build("fork")
    assert fw_patches and want["firmware_patches"] == [p["blob"] for p in fw_patches]
    assert want["firmware_patches"][0] == subprocess.run(
        ["git", "hash-object", str(sorted((REPO / "firmware" / "patches").glob("*.patch"))[0])],
        capture_output=True, text=True, check=True).stdout.strip()
    commit = want["firmware_commit"]
    assert len(commit) == 40 and runner.expected_build("develop") == {
        "firmware_patches": [], "simulator_patches": [], "firmware_commit": commit}
    assert runner.expected_build("2.0.0rc") == {"firmware_patches": [], "simulator_patches": []}

    assert runner.stale_build("fork") == ["no build-info.json"]
    _fake_build(tmp_path, "fork", firmware_commit=commit, firmware_patches=fw_patches,
                simulator_patches=sim_patches)
    assert runner.stale_build("fork") == []
    runner.check_build("fork")
    assert runner.build_info("fork")["firmware_commit"] == commit

    old = [{**fw_patches[0], "blob": "0" * 40}] + fw_patches[1:]
    (tmp_path / "bin" / "fork" / "build-info.json").write_text(json.dumps(
        {"firmware_commit": "1" * 40, "firmware_patches": old, "simulator_patches": []}))
    why = runner.stale_build("fork")
    assert len(why) == 3 and "expected " + commit[:12] in why[0] and "firmware/patches (built 00000000" in why[1]
    assert "e2e/sim/patches/simulator (built none" in why[2]
    with pytest.raises(runner.StaleBuild, match="build_sim.sh fork, or pass --build"):
        runner.check_build("fork")

    # Context.program: missing -> skip, stale -> an error result, never a silent pass.
    ctx = Context(fw=FORK, backend_kind="mock", out=tmp_path / "o", result=Result("X", "fork", "mock"),
                  python=sys.executable)
    res = execute(Scenario("X", "x", lambda c: c.check("ran", bool(c.program)), ("fork",)), ctx)
    assert res.status == "error" and "StaleBuild" in res.error and not res.checks
    ctx2 = Context(fw=DEV, backend_kind="mock", out=tmp_path / "o2", result=Result("X", "develop", "mock"),
                   python=sys.executable)
    with pytest.raises(SkipScenario, match="build_sim.sh develop"):
        ctx2.program


def test_run_e2e_build_always_runs_the_stamped_scripts(monkeypatch, tmp_path):
    """--build runs build_sim.sh for every firmware a selected scenario runs on (it is a
    no-op when up to date), and build_wire.sh where W1 runs, even if a binary exists."""
    sys.path.insert(0, str(REPO / "e2e"))
    try:
        import run_e2e
    finally:
        sys.path.remove(str(REPO / "e2e"))
    monkeypatch.setenv("SENDTOX3_E2E_WORK", str(tmp_path))
    _fake_build(tmp_path, "fork")
    calls: list[list[str]] = []
    monkeypatch.setattr(run_e2e.subprocess, "run", lambda argv, check: calls.append(argv[1:]))
    run_e2e.ensure_built(["S6", "W1"], ["1.6.5", "develop", "fork"])
    sim, wire_ = str(REPO / "e2e" / "sim" / "build_sim.sh"), str(REPO / "e2e" / "wire" / "build_wire.sh")
    assert calls == [[sim, "develop"], [wire_, "develop"], [sim, "fork"], [wire_, "fork"]]
    calls.clear()
    run_e2e.ensure_built(["S5"], ["3f387a9", "1.6.5"])  # S5 does not run on 3f387a9
    assert calls == [[sim, "1.6.5"]]


def test_build_wire_uses_the_recorded_simulator_dir(tmp_path):
    """build_sim.sh records the simulator worktree (fork's is the patched one), and
    build_wire.sh compiles against it instead of assuming the unpatched simulator/<sha12>."""
    script = (REPO / "e2e" / "sim" / "build_sim.sh").read_text()
    assert '"simulator_dir": sim_dir' in script and '"$SIM_SHA" "$SIM_DIR"' in script
    work = tmp_path / "work"
    (work / "fw" / "fork" / "src" / "util").mkdir(parents=True)
    (work / "fw" / "fork" / "src" / "util" / "PluginEvents.cpp").write_text("")
    patched = work / "simulator" / "5b380779ddea-e8af7369"
    out = _fake_build(work, "fork", firmware_version="2.0.0", simulator_commit="5b380779ddea" + "0" * 28,
                      simulator_dir=str(patched))
    env = dict(os.environ, SENDTOX3_E2E_WORK=str(work))
    run = subprocess.run(["bash", str(REPO / "e2e" / "wire" / "build_wire.sh"), "fork"], capture_output=True,
                         text=True, timeout=30, env=env)
    assert run.returncode != 0 and f"simulator sources missing at {patched}/src" in run.stderr
    (patched / "src").mkdir(parents=True)  # now only ArduinoJson is missing
    run = subprocess.run(["bash", str(REPO / "e2e" / "wire" / "build_wire.sh"), "fork"], capture_output=True,
                         text=True, timeout=30, env=env)
    assert run.returncode != 0 and "ArduinoJson missing" in run.stderr
    # An older build-info.json without simulator_dir: the unpatched worktree.
    info = json.loads((out / "build-info.json").read_text())
    del info["simulator_dir"]
    (out / "build-info.json").write_text(json.dumps(info))
    run = subprocess.run(["bash", str(REPO / "e2e" / "wire" / "build_wire.sh"), "fork"], capture_output=True,
                         text=True, timeout=30, env=env)
    assert f"simulator sources missing at {work}/simulator/5b380779ddea/src" in run.stderr


def test_wire_result_parsing(tmp_path):
    fake = tmp_path / "wire_harness"
    sent = base64.b64encode(b"GET /x HTTP/1.1\r\nHost: h\r\nAuthorization: Bearer t\r\n\r\n").decode()
    fake.write_text("#!/bin/sh\n"
                    "echo 'noise'\n"
                    "echo '@{\"type\":\"browse\",\"status\":200,\"rows\":1}'\n"
                    "echo '@{\"type\":\"row\",\"index\":0,\"id\":\"abc\",\"title\":\"T\"}'\n"
                    f"echo '@{{\"type\":\"wire\",\"connects\":1,\"sent_b64\":\"{sent}\"}}'\n"
                    "echo 'log line' >&2\nexit 0\n")
    fake.chmod(0o755)
    card = wire.prepare_sd(tmp_path / "sd", base_url="http://127.0.0.1:1", token="t")
    assert wire.plugin_dir(card.root) == "/.crosspoint/plugins/sendtox3"
    res = wire.run(fake, card.root, ["catalog", "/x"])
    assert res.returncode == 0 and res.browse["status"] == 200 and res.rows[0]["id"] == "abc"
    assert res.connects == 1 and res.requests()[0].header("Authorization") == "Bearer t"
    assert "log line" in res.stderr and "/.crosspoint/plugins/sendtox3/device.json" in res.files
    assert res.to_json()["requests"][0]["start_line"] == "GET /x HTTP/1.1"
    with pytest.raises(ValueError):
        wire.prepare_sd(tmp_path / "sd2")


def test_find_program_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SENDTOX3_E2E_WORK", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="build_sim.sh 2.0.0rc"):
        runner.find_program("2.0.0rc")
    with pytest.raises(FileNotFoundError, match="build_wire.sh"):
        wire.find_binary("2.0.0rc")


def test_free_port_pair():
    p = runner.free_port(pair=True)
    assert 1024 <= p < 65535


def test_run_sim_with_fake_program(tmp_path):
    """The runner's plumbing (env, serial capture, watches, early stop) without SDL."""
    prog = tmp_path / "program"
    prog.write_text("#!/bin/sh\n"
                    "echo \"[1] [DBG] [ACT] Entering activity: Home\"\n"
                    "echo \"SD=$CROSSPOINT_SIM_SD PORT=$CROSSPOINT_SIM_HTTP_PORT\"\n"
                    "echo \"[2] [ERR] [X] boom\"\n"
                    "echo \"[3] [DBG] [WEB] Web server started on port 80\"\n"
                    "sleep 30\n")
    prog.chmod(0o755)
    fired: list[str] = []
    s = InputScript(start_ms=0)
    s.press("ENTER").quit()
    res = runner.run_sim(prog, tmp_path / "sd", s, tmp_path / "out", http_port=12345,
                         watches=[runner.Watch("Web server started", fired.append)],
                         stop_when=lambda: bool(fired), stop_grace_ms=100, timeout_s=20)
    assert res.stopped_early and not res.timed_out and res.seconds < 15
    assert res.activities() == ["Home"] and res.entered("Home")
    assert res.errors() == ["[2] [ERR] [X] boom"] and res.errors(ignore=["boom"]) == []
    assert "PORT=12345" in res.log and str((tmp_path / "sd").resolve()) in res.log
    assert fired and res.env["CROSSPOINT_SIM_INPUT_SCRIPT"].startswith("0:ENTER")
    assert res.summary()["activities"] == ["Home"]
    with pytest.raises(ValueError):
        runner.run_sim(prog, tmp_path / "sd", InputScript(), tmp_path / "o2",
                       env={"CROSSPOINT_SIM_HTTP_MOCK_ROOT": "/tmp"})


_RESTARTING_PROGRAM = """#!/bin/sh
# Stands in for a simulator with the restart patch: boot 1 restarts into the reader.
if [ -z "$CROSSPOINT_SIM_BOOT" ]; then
  echo "[1] [DBG] [ACT] Entering activity: Home"
  echo "[2] [DBG] [ACT] Entering activity: PluginCatalog"
  echo "[3] [DBG] [MAIN] Silent restart (target=reader)"
  [ "$CROSSPOINT_SIM_RESTART" = exec ] || { echo "[4] [DBG] [ACT] Entering activity: EpubReader"; exit 0; }
  echo "[SIM] software restart: relaunching (rtc 8 bytes)"
  CROSSPOINT_SIM_BOOT=1 CROSSPOINT_SIM_INPUT_SCRIPT="$CROSSPOINT_SIM_INPUT_SCRIPT_AFTER_RESTART" \
    CROSSPOINT_SIM_SCREENSHOTS="$CROSSPOINT_SIM_SCREENSHOTS_AFTER_RESTART" exec "$0"
fi
echo "[SIM] software restart: boot 1, restored 8/8 rtc bytes"
echo "[1] [DBG] [ACT] Entering activity: EpubReader"
echo "INPUT=$CROSSPOINT_SIM_INPUT_SCRIPT"
shot="${CROSSPOINT_SIM_SCREENSHOTS#*:}"
printf 'BM' > "$shot"
"""


def test_run_sim_restart_splits_boots(tmp_path):
    prog = tmp_path / "program"
    prog.write_text(_RESTARTING_PROGRAM)
    prog.chmod(0o755)
    s = InputScript(start_ms=0)
    s.press("ENTER").shot("catalog").quit()
    after = InputScript(start_ms=100)
    after.press("DOWN").shot("reader").quit()
    res = runner.run_sim(prog, tmp_path / "sd", s, tmp_path / "out", restart="exec", after_restart=after,
                         png=False, timeout_s=20)
    assert res.returncode == 0 and not res.timed_out
    assert res.env["CROSSPOINT_SIM_RESTART"] == "exec"
    assert res.env["CROSSPOINT_SIM_INPUT_SCRIPT_AFTER_RESTART"] == after.input_spec()
    assert after.input_spec().startswith("100:DOWN;")
    assert res.env["CROSSPOINT_SIM_SCREENSHOTS_AFTER_RESTART"].endswith("/shots/reader.bmp")
    assert len(res.boots()) == 2 and len(res.relaunched_at) == 1
    assert res.activities() == ["Home", "PluginCatalog", "EpubReader"]
    assert res.activities(boot=0) == ["Home", "PluginCatalog"] and res.activities(boot=1) == ["EpubReader"]
    assert res.activities(boot=-1) == ["EpubReader"] and res.activities(boot=5) == []
    assert res.entered("EpubReader", boot=1) and not res.entered("EpubReader", boot=0)
    assert res.silent_restarts() == ["reader"] and res.silent_restarts(boot=1) == []
    assert f"INPUT={after.input_spec()}" in res.boots()[1]  # the relaunch runs the after-restart schedule
    assert res.shots["reader"].taken and not res.shots["catalog"].taken
    assert res.summary()["boots"] == 2

    # Without restart="exec" the restart continues in the same process (upstream).
    plain = runner.run_sim(prog, tmp_path / "sd", s, tmp_path / "out2", png=False, timeout_s=20)
    assert len(plain.boots()) == 1 and plain.activities() == ["Home", "PluginCatalog", "EpubReader"]
    assert "CROSSPOINT_SIM_RESTART" not in plain.env and plain.relaunched_at == []


def test_run_sim_restart_arguments(tmp_path):
    prog = tmp_path / "program"
    prog.write_text("#!/bin/sh\nexit 0\n")
    prog.chmod(0o755)
    with pytest.raises(ValueError, match="needs restart"):
        runner.run_sim(prog, tmp_path / "sd", InputScript(), tmp_path / "o", after_restart=InputScript())
    with pytest.raises(ValueError, match="restart must be"):
        runner.run_sim(prog, tmp_path / "sd", InputScript(), tmp_path / "o", restart="reboot")
    a, b = InputScript(), InputScript()
    a.shot("x")
    b.shot("x")
    with pytest.raises(ValueError, match="unique"):
        runner.run_sim(prog, tmp_path / "sd", a, tmp_path / "o", restart="exec", after_restart=b)


def test_run_sim_timeout_kills(tmp_path):
    prog = tmp_path / "program"
    prog.write_text("#!/bin/sh\nsleep 60\n")
    prog.chmod(0o755)
    res = runner.run_sim(prog, tmp_path / "sd", InputScript(start_ms=0), tmp_path / "out", timeout_s=1)
    assert res.timed_out and res.returncode is not None


# ---------------------------------------------------------------------------------------
# Opt-in: real simulator runs
# ---------------------------------------------------------------------------------------

needs_sim = pytest.mark.skipif(os.environ.get("SENDTOX3_E2E_SIM") != "1",
                               reason="set SENDTOX3_E2E_SIM=1 (and build the simulators) to run")


@needs_sim
@pytest.mark.parametrize("scenario,ref", [("S1", "2.0.0rc"), ("S3", "1.6.5"), ("W1", "2.0.0rc"), ("S6", "fork")])
def test_firmware_scenario(tmp_path, scenario, ref):
    out = subprocess.run([sys.executable, str(REPO / "e2e" / "run_e2e.py"), "-s", scenario, "-f", ref,
                          "--out", str(tmp_path), "--json"], capture_output=True, text=True, timeout=600)
    report = json.loads(out.stdout)
    res = report["results"][0]
    assert res["status"] == "pass", json.dumps(res, indent=2)[:4000]


@pytest.mark.docker
@needs_sim
def test_firmware_scenario_docker_backend(tmp_path):
    """S1 + S3 against the app image (build it first: docker build -t send_to_x3:e2e send_to_x3)."""
    out = subprocess.run([sys.executable, str(REPO / "e2e" / "run_e2e.py"), "-s", "S1,S3", "-f", "2.0.0rc",
                          "--backend", "docker", "--out", str(tmp_path), "--json"], capture_output=True, text=True,
                         timeout=900)
    report = json.loads(out.stdout)
    assert [r["status"] for r in report["results"]] == ["pass", "pass"], json.dumps(report, indent=2)[:4000]
