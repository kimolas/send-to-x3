"""Build the app image and run it like Home Assistant would (needs docker).

Skipped unless SENDTOX3_DOCKER_TESTS=1. Builds ``send_to_x3/`` for the native
platform (override with SENDTOX3_DOCKER_PLATFORM, e.g. ``linux/amd64`` to test
amd64 under emulation on Apple Silicon), then:

* checks the runtime imports, tesseract (with English data), labels and Python version;
* runs the image the way the Supervisor does (``/init`` + CMD, ``/data`` with an
  ``options.json``) and, once ``sendtox3/__main__.py`` exists, waits for
  ``GET /healthz`` on a loopback-only published port;
* checks that the CMD's exit code becomes the container's (so a crash stops the
  container and the Supervisor watchdog can restart it), and that no bytecode is
  written at runtime.

Run::

    SENDTOX3_DOCKER_TESTS=1 python -m pytest -q tests/test_packaging_docker.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "send_to_x3"
PLATFORM = os.environ.get("SENDTOX3_DOCKER_PLATFORM", "")
TAG = f"local/send-to-x3:pytest-{(PLATFORM or 'native').replace('/', '-')}"
IMPORTS = "import sendtox3, trafilatura, lxml, aiohttp, pypdfium2, segno, jinja2, httpx, charset_normalizer"


def _docker(*args: str, timeout: float = 600, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


def _run(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    platform = ["--platform", PLATFORM] if PLATFORM else []
    return _docker("run", "--rm", *platform, *args, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def image() -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found")
    if _docker("info", timeout=30, check=False).returncode != 0:
        pytest.skip("docker daemon not reachable")
    cmd = ["buildx", "build", "--load", "-t", TAG, "--build-arg", "BUILD_VERSION=pytest"]
    if PLATFORM:
        cmd += ["--platform", PLATFORM]
    result = _docker(*cmd, str(APP), timeout=1800, check=False)
    assert result.returncode == 0, result.stderr[-4000:]
    return TAG


@pytest.fixture
def data_volume(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    options = {"log_level": "debug", "ha_share_enabled": False, "imap_enabled": False}
    (d / "options.json").write_text(json.dumps(options), encoding="utf-8")
    return d


def test_runtime_dependencies(image: str) -> None:
    probe = (
        f"{IMPORTS}; import sys, sqlite3; "
        "assert sys.version_info[:2] == (3, 14), sys.version; "
        "assert sqlite3.sqlite_version_info >= (3, 35), sqlite3.sqlite_version; "
        "print('ok', sendtox3.__version__)"
    )
    r = _run(image, "python3", "-c", probe)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ok " in r.stdout
    r = _run(image, "tesseract", "--version")
    assert r.returncode == 0 and "tesseract" in (r.stdout + r.stderr).lower()
    r = _run(image, "tesseract", "--list-langs")
    assert r.returncode == 0 and "eng" in (r.stdout + r.stderr).split()


def test_options_json_is_read_from_data(image: str, data_volume: Path) -> None:
    probe = "from sendtox3.config import load_options as l; o = l(); print(o.log_level, o.data_dir, o.port)"
    r = _run("-v", f"{data_volume}:/data", image, "python3", "-c", probe)
    assert r.returncode == 0, r.stderr
    assert "debug /data 8765" in r.stdout


def test_labels_and_entrypoint(image: str) -> None:
    info = json.loads(_docker("image", "inspect", image).stdout)[0]["Config"]
    assert info["Labels"]["io.hass.type"] == "app"
    assert info["Labels"]["io.hass.version"] == "pytest"
    assert info["Entrypoint"] == ["/init"], "s6-overlay must stay PID 1"
    assert info["Cmd"] == ["/run.sh"]


def test_bytecode_is_precompiled_and_never_written(image: str) -> None:
    script = (
        "count() { find /opt/sendtox3 -name '*.pyc' | wc -l; }; "
        f"before=$(count); python3 -c '{IMPORTS}' && after=$(count); echo \"$before $after\""
    )
    r = _run(image, "sh", "-c", script)
    assert r.returncode == 0, r.stderr
    before, after = map(int, r.stdout.strip().splitlines()[-1].split())
    assert before > 0, "compileall should have run at build time"
    assert after == before, "PYTHONDONTWRITEBYTECODE: nothing is written at runtime"


def test_cmd_exit_code_becomes_container_exit_code(image: str) -> None:
    """s6-overlay's /init returns the CMD's exit code, so a crash stops the container
    and the Supervisor watchdog can restart it."""
    assert _run(image, "sh", "-c", "exit 3").returncode == 3


def test_run_sh_starts_python_module(image: str, data_volume: Path) -> None:
    if (APP / "sendtox3" / "__main__.py").exists():
        pytest.skip("covered by test_server_answers_healthz")
    r = _run("-v", f"{data_volume}:/data", image)
    out = r.stdout + r.stderr
    assert "Starting Send to X3 (standalone, no Supervisor)" in out
    assert r.returncode != 0 and "No module named sendtox3.__main__" in out


def test_server_answers_healthz(image: str, data_volume: Path) -> None:
    if not (APP / "sendtox3" / "__main__.py").exists():
        pytest.skip("sendtox3/__main__.py is not written yet")
    name = f"sendtox3-pytest-{uuid.uuid4().hex[:8]}"
    platform = ["--platform", PLATFORM] if PLATFORM else []
    _docker("run", "-d", "--name", name, *platform, "-p", "127.0.0.1::8765", "-v", f"{data_volume}:/data", image)
    try:
        port = _docker("port", name, "8765/tcp").stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        deadline = time.monotonic() + 90
        last = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as resp:
                    assert resp.status == 200
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError) as err:
                last = repr(err)
                state = _docker("inspect", "-f", "{{.State.Running}}", name, check=False).stdout.strip()
                assert state == "true", "container exited:\n" + _docker("logs", name, check=False).stderr[-4000:]
                time.sleep(1)
        else:
            pytest.fail(f"/healthz did not answer: {last}\n" + _docker("logs", name, check=False).stderr[-4000:])
        logs = _docker("logs", name, check=False)
        assert "Starting Send to X3 (standalone, no Supervisor)" in logs.stdout + logs.stderr
        assert (data_volume / "sendtox3.db").exists(), "the store lives in /data"
    finally:
        _docker("rm", "-f", name, check=False)


def test_image_size_budget(image: str) -> None:
    """Keep the image small for SD-card hosts (base image is ~200 MB unpacked)."""
    size = int(_docker("image", "inspect", image, "--format", "{{.Size}}").stdout.strip())
    print(f"{image}: {size / 1e6:.1f} MB")
    assert size < 450e6
