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


def pytest_collection_modifyitems(config, items):
    skip_net = pytest.mark.skip(reason="set SENDTOX3_NETWORK_TESTS=1 to run")
    skip_docker = pytest.mark.skip(reason="set SENDTOX3_DOCKER_TESTS=1 to run")
    for item in items:
        if "network" in item.keywords and os.environ.get("SENDTOX3_NETWORK_TESTS") != "1":
            item.add_marker(skip_net)
        if "docker" in item.keywords and os.environ.get("SENDTOX3_DOCKER_TESTS") != "1":
            item.add_marker(skip_docker)
