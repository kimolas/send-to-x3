"""Round-2 regression tests, group E (web layer): R2-E1 (172.16/12 only refused under
Home Assistant) and R2-E3 (X3 menu paths written as "Home > Plugins > Send to X3" and
"File Transfer > Join a Network")."""

from __future__ import annotations

import logging
import re

import pytest

from sendtox3 import __main__ as entry
from sendtox3 import plugin_bundle
from sendtox3.app_state import AppState
from sendtox3.ingest import Processor
from sendtox3.web import admin
from sendtox3.web.app import create_app
from sendtox3.web.common import STATE_KEY

from test_web_admin import login
from test_web_fakes import install_fakes

LAN_172 = "http://172.20.1.5:8765"  # a home LAN that really uses 172.16.0.0/12
DOCKER_HINT = "If this app runs in a Docker container"


@pytest.fixture
def no_supervisor(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)


@pytest.fixture
def app(opts, store, monkeypatch):
    install_fakes(monkeypatch)
    return create_app(opts, store, Processor(store, opts))


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


@pytest.fixture
def installs(monkeypatch):
    calls = []

    async def fake_install(host, **kw):
        calls.append((host, kw["base_url"]))
        return ["Done: the plugin is installed."]

    monkeypatch.setattr(plugin_bundle, "install_to_device", fake_install)
    return calls


# ---------------------------------------------------------------- R2-E1


async def test_plain_install_on_a_172_16_lan_gets_a_bundle(client, app, installs, no_supervisor):
    state = app[STATE_KEY]
    state.base_url, state.base_url_source = LAN_172, "local address"
    csrf = await login(client)
    resp = await client.get("/admin/plugin-bundle.zip")
    assert resp.status == 200, await resp.text()  # was 409 "an address inside Docker"
    assert (await client.get("/admin/setup/android.json")).status == 200
    html = await (await client.post("/admin/install-plugin",
                                    data={"csrf": csrf, "device_host": "172.20.1.60"})).text()
    assert "Plugin installed." in html and installs == [("172.20.1.60", LAN_172)]
    setup = await (await client.get("/admin/setup")).text()
    assert "cannot be installed yet" not in setup
    # Still a hint, since a plain-Docker container on a bridge network gets such an address.
    assert DOCKER_HINT in setup and "ignore this" in setup


async def test_a_172_16_guess_is_still_refused_under_home_assistant(client, app, installs, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    state = app[STATE_KEY]
    state.base_url, state.base_url_source = "http://172.30.33.5:8765", "local address"
    csrf = await login(client)
    assert (await client.get("/admin/plugin-bundle.zip")).status == 409
    assert (await client.get("/admin/setup/android.json")).status == 409
    html = await (await client.post("/admin/install-plugin",
                                    data={"csrf": csrf, "device_host": "192.168.1.60"})).text()
    assert "inside Docker" in html and installs == []
    setup = await (await client.get("/admin/setup")).text()
    assert "That looks like an address inside Docker" in setup and DOCKER_HINT not in setup


@pytest.mark.parametrize("supervisor, source, expected", [
    (False, "local address", "maybe"),
    (False, "options", ""),        # typed by the user outside Home Assistant: not second-guessed
    (True, "local address", "docker"),
    (True, "options", "docker"),   # warned, but not refused (explicit choice)
    (True, "supervisor", ""),      # the Supervisor reported it as the host's own LAN address
])
def test_internal_address_warning(opts, store, monkeypatch, supervisor, source, expected):
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    if supervisor:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    else:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    st = AppState(opts=opts, store=store, processor=None)
    st.base_url, st.base_url_source = LAN_172, source
    assert admin.internal_address_warning(st) == expected
    assert (admin.bundle_problem(st) != "") is (supervisor and source == "local address")
    st.base_url = "http://192.168.1.50:8765"
    assert admin.internal_address_warning(st) == "" and admin.bundle_problem(st) == ""


def test_startup_log_without_supervisor_does_not_claim_docker(opts, store, no_supervisor, caplog):
    caplog.set_level(logging.INFO, logger="sendtox3")
    st = AppState(opts=opts, store=store, processor=None)
    st.base_url, st.base_url_source = LAN_172, "local address"
    entry._log_startup(st, {}, "0.0.0.0", 8765)
    assert "ignore this if your LAN really uses it" in caplog.text
    caplog.clear()
    st.base_url_source = "options"
    entry._log_startup(st, {}, "0.0.0.0", 8765)
    assert "172.16.0.0/12" not in caplog.text and "inside Docker" not in caplog.text


def test_startup_log_under_supervisor_warns_about_docker(opts, store, monkeypatch, caplog):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    caplog.set_level(logging.INFO, logger="sendtox3")
    st = AppState(opts=opts, store=store, processor=None)
    st.base_url, st.base_url_source = "http://172.30.33.5:8765", "local address"
    entry._log_startup(st, {}, "0.0.0.0", 8765)
    assert "inside Docker that the X3 cannot reach" in caplog.text


# ---------------------------------------------------------------- R2-E3


@pytest.mark.parametrize("auto_delivery", [True, False])
async def test_setup_page_writes_x3_menu_paths_with_arrows(aiohttp_client, opts, store, monkeypatch, auto_delivery):
    install_fakes(monkeypatch)
    opts.auto_delivery = auto_delivery
    client = await aiohttp_client(create_app(opts, store, Processor(store, opts)))
    await login(client)
    html = await (await client.get("/admin/setup")).text()
    assert "Home, Plugins" not in html and "Settings, Connectivity" not in html
    assert "Home &gt; Plugins &gt; Send to X3" in html
    assert "Settings &gt; Connectivity &gt; Plugins" in html
    # The X3's menu item is "Join a Network" (STR_JOIN_NETWORK), reached from File Transfer.
    assert not re.search(r"Join Network", html)
    assert "File Transfer &gt; Join a Network" in html
    assert ("File Transfer &gt; Join a Network or KOReader sync" in html) is auto_delivery
