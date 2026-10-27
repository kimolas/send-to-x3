"""Regression tests for review findings 11, 28, 46, 49 and 52 (plugin bundle, server address)."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import zipfile

import pytest

from sendtox3 import plugin_bundle
from sendtox3.plugin_bundle import build_bundle_zip, normalize_base_url, readme_bytes, resolve_base_url

BASE = "http://192.168.1.50:8765"
TOKEN = "device-test-token"


# ---------------------------------------------------------------- finding 11


def test_readme_does_not_promise_delivery_at_every_sleep_on_2_0_0rc(opts):
    opts.auto_delivery = True
    text = readme_bytes(base_url=BASE, opts=opts).decode()
    assert "Every time the X3 goes to sleep" not in text
    assert "2.0.0rc" in text and "facaa87" in text
    assert "next time the X3 joins Wi-Fi" in text and "File Transfer" in text and "OPDS" in text
    opts.auto_delivery = False
    off = readme_bytes(base_url=BASE, opts=opts).decode()
    assert "is off" in off and "facaa87" not in off and "Get 3 new" in off


# ---------------------------------------------------------------- finding 28


def test_bundle_zip_entries_carry_unix_modes(opts):
    with zipfile.ZipFile(io.BytesIO(build_bundle_zip(base_url=BASE, device_token=TOKEN, opts=opts))) as z:
        infos = z.infolist()
    dirs = [i for i in infos if i.filename.endswith("/")]
    files = [i for i in infos if not i.filename.endswith("/")]
    assert {i.filename for i in dirs} >= {".crosspoint/", ".crosspoint/plugins/", ".crosspoint/plugins/sendtox3/",
                                          "Inbox/"}
    for i in dirs:
        assert i.external_attr >> 16 == 0o40755 and i.external_attr & 0x10, i.filename
    for i in files:
        assert i.external_attr >> 16 == 0o100644, i.filename
    assert "Inbox/.keep" in {i.filename for i in files}


@pytest.mark.skipif(shutil.which("unzip") is None, reason="Info-ZIP unzip not installed")
def test_bundle_extracted_with_unzip_is_readable(opts, tmp_path):
    zpath = tmp_path / "b.zip"
    zpath.write_bytes(build_bundle_zip(base_url=BASE, device_token=TOKEN, opts=opts))
    out = tmp_path / "out"
    subprocess.run(["unzip", "-o", "-q", str(zpath), "-d", str(out)], check=True)
    assert "sendtox3" in os.listdir(out / ".crosspoint" / "plugins")  # used to fail: Permission denied
    assert (out / ".crosspoint" / "plugins" / "sendtox3" / "device.json").read_bytes()
    assert os.listdir(out / "Inbox") == [".keep"]


# ---------------------------------------------------------------- finding 46


@pytest.mark.parametrize("bad", ["http://192.168.1.50:8765/opds?x=1", "http://192.168.1.50 :8765", "http://",
                                 "http://h:99999", "http://fe80::1"])
async def test_invalid_url_server_host_never_raises(opts, bad, caplog):
    opts.server_host = bad
    assert await resolve_base_url(opts, supervisor_token="") == ("", plugin_bundle.INVALID_SERVER_HOST)
    assert "server_host" in caplog.text and "invalid" in caplog.text


# ---------------------------------------------------------------- finding 49


@pytest.mark.parametrize("bad", ["192.168.1.50/x", "192.168.1.50 8765", "fe80::1", "pi.lan:0", "pi.lan:70000",
                                 "-pi.lan", "pi.lan:", "x3@pi.lan", "pi.lan:80/opds"])
async def test_host_only_server_host_is_validated(opts, bad):
    opts.server_host = bad
    assert await resolve_base_url(opts, supervisor_token="") == ("", plugin_bundle.INVALID_SERVER_HOST)


@pytest.mark.parametrize("value, expected", [
    ("192.168.1.50", "http://192.168.1.50:8765"),
    ("192.168.1.50:9000", "http://192.168.1.50:9000"),
    ("homeassistant.local", "http://homeassistant.local:8765"),
    ("[fe80::1]", "http://[fe80::1]:8765"),
    ("[fe80::1]:8000", "http://[fe80::1]:8000"),
    (" pi.lan/ ", "http://pi.lan:8765"),
])
async def test_valid_server_host_values(opts, value, expected):
    opts.server_host = value
    assert await resolve_base_url(opts, supervisor_token="") == (expected, "options")


def test_normalize_base_url_rejects_bad_ports():
    for bad in ("http://fe80::1", "http://h:99999", "http://h:abc"):
        with pytest.raises(ValueError):
            normalize_base_url(bad)
    assert normalize_base_url("http://[fe80::1]:80") == "http://[fe80::1]:80"


# ---------------------------------------------------------------- finding 52 (helper)


def test_device_token_problem():
    assert plugin_bundle.device_token_problem("Abc-123_x.~!") == ""
    for bad in ("my x3 secret", "a{b}", 'q"', "é", ""):
        assert "device token must be" in plugin_bundle.device_token_problem(bad)
