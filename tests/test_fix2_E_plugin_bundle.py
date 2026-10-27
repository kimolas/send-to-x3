"""Round-2 regression tests, group E (plugin_bundle.py): R2-E2 (a full-URL server_host is
checked like the host-only form: no port 0, no user part) and R2-E3 (X3 menu wording)."""

from __future__ import annotations

import pytest

from sendtox3 import plugin_bundle
from sendtox3.ingest import Processor
from sendtox3.plugin_bundle import (INVALID_SERVER_HOST, install_to_device, normalize_base_url, readme_bytes,
                                    resolve_base_url)
from sendtox3.web.app import create_app
from sendtox3.web.common import STATE_KEY

# ---------------------------------------------------------------- R2-E2

BAD_FULL_URLS = [
    "http://h:0",
    "http://192.168.1.50:0/",
    "http://[fe80::1]:0",
    "http://h:",
    "http://user@h:80",
    "http://user:secret@192.168.1.50:8765",
    "https://user@x3.example/prefix",
    "http://@h:80",
]


@pytest.mark.parametrize("bad", [u for u in BAD_FULL_URLS if not u.rstrip("/").endswith(":0")])
def test_normalize_base_url_rejects_user_parts_and_empty_ports(bad):
    with pytest.raises(ValueError):
        normalize_base_url(bad)


@pytest.mark.parametrize("bad", BAD_FULL_URLS)
async def test_full_url_server_host_is_checked_like_the_host_only_form(opts, bad):
    opts.server_host = bad
    assert await resolve_base_url(opts, supervisor_token="") == ("", INVALID_SERVER_HOST)


@pytest.mark.parametrize("bad", ["h:0", "user@h:80", "h:"])
async def test_host_only_server_host_rejects_the_same(opts, bad):
    opts.server_host = bad
    assert await resolve_base_url(opts, supervisor_token="") == ("", INVALID_SERVER_HOST)


@pytest.mark.parametrize("good", ["http://h:1", "http://h:65535", "http://192.168.1.50:8765/prefix",
                                  "https://x3.example", "http://[fe80::1]:80"])
async def test_full_url_server_host_still_accepts_usable_values(opts, good):
    opts.server_host = good
    assert await resolve_base_url(opts, supervisor_token="") == (good, "options")


def test_create_app_marks_a_server_host_with_a_user_part_invalid(opts, store):
    opts.server_host = "http://user@192.168.1.50:8765"
    state = create_app(opts, store, Processor(store, opts))[STATE_KEY]
    assert state.base_url == "" and state.base_url_source == INVALID_SERVER_HOST


# ---------------------------------------------------------------- R2-E3


def test_readme_says_file_transfer_join_a_network(opts):
    opts.auto_delivery = True
    text = readme_bytes(base_url="http://192.168.1.50:8765", opts=opts).decode()
    assert "File Transfer > Join a Network" in text and "Join Network" not in text
    assert "Home > Plugins > Send to X3" in text and "Home, Plugins" not in text


class _Unreachable:
    async def status(self):
        return None

    async def close(self):
        pass


async def test_install_messages_say_file_transfer_join_a_network(opts):
    kw = dict(base_url="http://192.168.1.50:8765", device_token="device-test-token", opts=opts)
    lines = await install_to_device("192.168.1.60", client=_Unreachable(), **kw)
    assert lines[-1].startswith("Error: could not reach the X3")
    assert "File Transfer > Join a Network" in lines[-1] and "Join Network" not in lines[-1]
    lines = await install_to_device("", **kw)
    assert "File Transfer > Join a Network" in lines[-1]


def test_no_message_in_plugin_bundle_says_join_network():
    import inspect

    src = inspect.getsource(plugin_bundle)
    assert "Join Network" not in src and "Home, Plugins" not in src
