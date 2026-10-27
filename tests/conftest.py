"""Shared pytest fixtures. Area-specific fixtures live in the area's own test files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sendtox3.config import Options
from sendtox3.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("SENDTOX3_DATA_DIR", str(d))
    return d


@pytest.fixture
def opts(data_dir) -> Options:
    return Options(
        server_host="127.0.0.1",
        ingest_token="ingest-test-token",
        device_token="device-test-token",
        admin_token="admin-test-token",
        imap_enabled=False,
        ha_share_enabled=False,
        allow_private_urls=True,  # tests fetch from 127.0.0.1 fixture servers
    )


@pytest.fixture
def store(data_dir) -> Store:
    s = Store(data_dir)
    yield s
    s.close()


@pytest.fixture
async def serve_like_main():
    """Serve an app the way ``__main__`` does (an ``AppRunner`` with aiohttp's default
    ``handler_cancellation=False``) and return a client session for it. aiohttp's test
    server always cancels a handler whose client went away, which would stop a handler
    that is still waiting for the client's TCP acknowledgement of a download."""
    from aiohttp import ClientSession, CookieJar, web

    started = []

    async def start(app):
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        # unsafe=True: keep cookies for an IP address, as aiohttp's test client does.
        session = ClientSession(base_url=f"http://127.0.0.1:{port}", cookie_jar=CookieJar(unsafe=True))
        started.append((runner, session))
        return session

    yield start
    for runner, session in started:
        await session.close()
        await runner.cleanup()


def pytest_collection_modifyitems(config, items):
    skip_net = pytest.mark.skip(reason="set SENDTOX3_NETWORK_TESTS=1 to run")
    skip_docker = pytest.mark.skip(reason="set SENDTOX3_DOCKER_TESTS=1 to run")
    for item in items:
        if "network" in item.keywords and os.environ.get("SENDTOX3_NETWORK_TESTS") != "1":
            item.add_marker(skip_net)
        if "docker" in item.keywords and os.environ.get("SENDTOX3_DOCKER_TESTS") != "1":
            item.add_marker(skip_docker)
