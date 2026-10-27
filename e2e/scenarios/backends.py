"""Pluggable servers for the e2e scenarios.

``mock``    e2e/mock_backend.py (standard library stand-in)
``local``   the real app: ``python -m sendtox3`` with a temp ``SENDTOX3_DATA_DIR`` and an
            ``options.json`` written here (port, tokens, LAN push target), bound to
            127.0.0.1 via ``SENDTOX3_BIND``
``docker``  the app image: ``docker run`` with the same options, published on
            127.0.0.1 only

Every backend is seeded the same way, through ``POST /api/v1/ingest`` with the ingest
token, and serves the device API on ``base_url``. The device never talks to the backend
directly: scenarios put a :class:`~e2e.scenarios.wiretap.WireTap` in between and give
the device the tap's address (``public_url``), which is also what the backend must
advertise in absolute links (OPDS paging, bundle URLs).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[2]
APP_DIR = REPO / "send_to_x3"

INGEST_TOKEN = "e2e-ingest-token-0123456789abcdefgh"
DEVICE_TOKEN = "e2e-device-token-0123456789abcdefgh"
ADMIN_TOKEN = "e2e-admin-token-0123456789abcdefghi"


class BackendError(RuntimeError):
    pass


@dataclass
class BackendConfig:
    port: int  # loopback port the backend listens on
    data_dir: Path
    public_url: str  # what the device uses (the tap)
    device_host: str = ""  # LAN push target host:port ("" = off)
    push_poll_seconds: int = 1
    delivery_format: str = "epub"  # catalog download + LAN push format ("epub" | "txt")
    empty_deliver_status: int = 404
    log_path: Optional[Path] = None
    python: str = sys.executable
    docker_image: str = "send_to_x3:e2e"
    extra_options: dict[str, Any] = field(default_factory=dict)


class Backend:
    """Base class: lifecycle, readiness and the HTTP calls every backend shares."""

    kind = "base"

    def __init__(self, cfg: BackendConfig) -> None:
        self.cfg = cfg
        self.ingest_token = INGEST_TOKEN
        self.device_token = DEVICE_TOKEN
        self.proc: Optional[subprocess.Popen] = None
        self.stdout_path = cfg.data_dir / f"{self.kind}-backend.log"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.port}"

    # -- lifecycle ------------------------------------------------------------------------

    def command(self) -> tuple[list[str], dict[str, str], Optional[Path]]:
        raise NotImplementedError

    def start(self, timeout: float = 30.0) -> "Backend":
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        argv, env, cwd = self.command()
        self._log_f = self.stdout_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(argv, env=env, cwd=cwd, stdout=self._log_f, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                self.stop()
                raise BackendError(f"{self.kind} backend exited with {self.proc.returncode}; see {self.stdout_path}")
            try:
                if self.http("GET", "/healthz", auth=None, timeout=1)[0] == 200:
                    return self
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.1)
        self.stop()
        raise BackendError(f"{self.kind} backend not ready after {timeout:.0f}s; see {self.stdout_path}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if getattr(self, "_log_f", None) and not self._log_f.closed:
            self._log_f.close()

    def __enter__(self) -> "Backend":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- HTTP -----------------------------------------------------------------------------

    def http(self, method: str, path: str, *, body: Optional[bytes] = None, ctype: str = "",
             auth: Optional[str] = "device", timeout: float = 30) -> tuple[int, bytes, dict[str, str]]:
        headers = {}
        if auth == "device":
            headers["Authorization"] = f"Bearer {self.device_token}"
        elif auth == "ingest":
            headers["Authorization"] = f"Bearer {self.ingest_token}"
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}

    def seed(self, *, title: str = "", text: str = "", url: str = "") -> dict:
        """Ingest one item; returns the API's JSON (``id``, ``title``, ``status``)."""
        doc = {k: v for k, v in (("title", title), ("text", text), ("url", url)) if v}
        status, body, _ = self.http("POST", "/api/v1/ingest", body=json.dumps(doc).encode(),
                                    ctype="application/json", auth="ingest", timeout=60)
        if status != 200:
            raise BackendError(f"ingest failed: HTTP {status} {body[:200]!r}")
        out = json.loads(body)
        if not out.get("ok") or not out.get("id"):
            raise BackendError(f"ingest returned {out}")
        return out

    def item_bytes(self, item_id: str, fmt: str) -> bytes:
        status, body, _ = self.http("GET", f"/device/v1/items/{item_id}.{fmt}")
        if status != 200:
            raise BackendError(f"GET item {item_id}.{fmt}: HTTP {status}")
        return body

    def state(self) -> Optional[dict]:
        """Backend-side view (mock only); None when the backend has no such endpoint."""
        return None

    def bundle_zip(self, *, delivery_format: Optional[str] = None, auto_delivery: bool = True,
                   source: str = "auto") -> bytes:
        """The SD plugin bundle pointing at ``public_url``.

        ``source="app"`` uses the app's own generator (``sendtox3.plugin_bundle``),
        ``"spec"`` the §10 template from :mod:`e2e.sim.sd`; ``"auto"`` prefers the app.
        """
        from e2e.sim import sd as sdmod

        delivery_format = delivery_format or self.cfg.delivery_format
        if source in ("auto", "app"):
            try:
                if str(APP_DIR) not in sys.path:
                    sys.path.insert(0, str(APP_DIR))
                from sendtox3.config import Options  # type: ignore[import-not-found]
                from sendtox3.plugin_bundle import build_bundle_zip  # type: ignore[import-not-found]
            except Exception:  # noqa: BLE001 - any import problem means "not available"
                if source == "app":
                    raise
            else:
                opts = Options(delivery_format=delivery_format, auto_delivery=auto_delivery)
                return build_bundle_zip(base_url=self.cfg.public_url, device_token=self.device_token, opts=opts)
        return sdmod.spec_bundle_zip(self.cfg.public_url, self.device_token, delivery_format=delivery_format,
                                     auto_delivery=auto_delivery)


class MockBackend(Backend):
    kind = "mock"

    def command(self) -> tuple[list[str], dict[str, str], Optional[Path]]:
        c = self.cfg
        argv = [c.python, "-I", str(REPO / "e2e" / "mock_backend.py"), "--port", str(c.port),
                "--ingest-token", self.ingest_token, "--device-token", self.device_token,
                "--empty-deliver-status", str(c.empty_deliver_status)]
        if c.log_path:
            argv += ["--log", str(c.log_path)]
        if c.device_host:
            argv += ["--device-host", c.device_host, "--push-poll", str(c.push_poll_seconds),
                     "--push-format", c.delivery_format]
        return argv, dict(os.environ), None

    def state(self) -> Optional[dict]:
        status, body, _ = self.http("GET", "/mock/state", auth=None)
        return json.loads(body) if status == 200 else None


def _app_options(cfg: BackendConfig, container_port: Optional[int] = None) -> dict[str, Any]:
    host_port = cfg.public_url.split("://", 1)[1].rstrip("/")
    opts: dict[str, Any] = {
        "log_level": "debug",
        "server_host": host_port,
        "port": container_port or cfg.port,
        "ingest_token": INGEST_TOKEN,
        "device_token": DEVICE_TOKEN,
        "admin_token": ADMIN_TOKEN,
        "imap_enabled": False,
        "ha_share_enabled": False,
        "device_host": cfg.device_host,
        "push_poll_seconds": cfg.push_poll_seconds,
        "delivery_format": cfg.delivery_format,
        "allow_private_urls": True,
    }
    opts.update(cfg.extra_options)
    return opts


class LocalBackend(Backend):
    """``python -m sendtox3`` from this checkout."""

    kind = "local"

    def command(self) -> tuple[list[str], dict[str, str], Optional[Path]]:
        c = self.cfg
        (c.data_dir / "options.json").write_text(json.dumps(_app_options(c), indent=2), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("SENDTOX3_")}
        env.update({"SENDTOX3_DATA_DIR": str(c.data_dir), "SENDTOX3_BIND": "127.0.0.1",
                    "PYTHONPATH": str(APP_DIR), "PYTHONUNBUFFERED": "1"})
        env.pop("SUPERVISOR_TOKEN", None)
        env.pop("HASSIO_TOKEN", None)
        return [c.python, "-m", "sendtox3"], env, APP_DIR


class DockerBackend(Backend):
    """The app image, published on 127.0.0.1 only. LAN push from inside the container
    reaches the simulator through ``host.docker.internal`` (Docker Desktop)."""

    kind = "docker"

    def command(self) -> tuple[list[str], dict[str, str], Optional[Path]]:
        if not shutil.which("docker"):
            raise BackendError("docker not found")
        c = self.cfg
        opts = _app_options(c, container_port=8765)
        if c.device_host.startswith("127.0.0.1:"):
            opts["device_host"] = "host.docker.internal:" + c.device_host.split(":", 1)[1]
        (c.data_dir / "options.json").write_text(json.dumps(opts, indent=2), encoding="utf-8")
        self.container = f"sendtox3-e2e-{os.getpid()}-{c.port}"
        argv = ["docker", "run", "--rm", "--name", self.container, "-p", f"127.0.0.1:{c.port}:8765",
                "-v", f"{c.data_dir}:/data", "--add-host", "host.docker.internal:host-gateway", c.docker_image]
        return argv, dict(os.environ), None

    def stop(self) -> None:
        if getattr(self, "container", None):
            subprocess.run(["docker", "stop", "-t", "5", self.container], capture_output=True, check=False)
        super().stop()


BACKENDS = {"mock": MockBackend, "local": LocalBackend, "docker": DockerBackend}


def make_backend(kind: str, cfg: BackendConfig) -> Backend:
    try:
        return BACKENDS[kind](cfg)
    except KeyError:
        raise ValueError(f"unknown backend {kind!r}; choose from {', '.join(BACKENDS)}") from None
