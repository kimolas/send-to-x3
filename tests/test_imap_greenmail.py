"""workers/imap.py against a real IMAP server: GreenMail standalone in docker.

Run with ``SENDTOX3_DOCKER_TESTS=1``. Proves, over real IMAPS + IDLE:
new mail is picked up within seconds (IDLE, not the 60 s poll), processed exactly once,
flagged \\Seen and moved to the processed folder; rejected mail is left unread and not
processed; and the worker survives a server restart (reconnect with backoff, new
UIDVALIDITY) and keeps delivering.

Ports are bound to 127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
import imaplib
import shutil
import smtplib
import socket
import ssl
import subprocess
import time
import uuid
from dataclasses import dataclass, field, replace
from email.message import EmailMessage
from email.policy import SMTP
from types import SimpleNamespace
from typing import Any

import pytest

from sendtox3.config import Options
from sendtox3.models import IngestRequest
from sendtox3.store import Store
from sendtox3.workers import imap as imapw

pytestmark = pytest.mark.docker

IMAGE = "greenmail/standalone:2.1.14"
USER = "x3@example.test"
PASSWORD = "gm-secret-pass"
AR_PASS = "mx.example.test; dkim=pass header.d=example.org; dmarc=pass header.from=example.org"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _insecure_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # GreenMail's self-signed test certificate
    return ctx


@dataclass
class GreenMail:
    name: str
    imaps: int
    smtp: int

    def client(self) -> imaplib.IMAP4_SSL:
        c = imaplib.IMAP4_SSL("127.0.0.1", self.imaps, ssl_context=_insecure_ctx(), timeout=10)
        c.login(USER, PASSWORD)
        return c

    def wait_ready(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                c = self.client()
                c.logout()
                with smtplib.SMTP("127.0.0.1", self.smtp, timeout=5) as s:
                    s.noop()
                return
            except Exception as e:  # not up yet
                last = e
                time.sleep(0.5)
        logs = subprocess.run(["docker", "logs", "--tail", "50", self.name], capture_output=True, text=True).stdout
        raise RuntimeError(f"GreenMail did not start: {last}\n{logs}")

    def send(self, raw: bytes) -> None:
        with smtplib.SMTP("127.0.0.1", self.smtp, timeout=10) as s:
            s.sendmail("qiong@example.org", [USER], raw)

    def folder_state(self, folder: str) -> list[set[str]]:
        """Flags of every message in ``folder`` ([] if the folder does not exist)."""
        c = self.client()
        try:
            typ, _ = c.select(f'"{folder}"', readonly=True)
            if typ != "OK":
                return []
            typ, data = c.uid("FETCH", "1:*", "(FLAGS)")
            out = []
            for d in data or []:
                if isinstance(d, bytes) and b"FLAGS" in d:
                    flags = d.split(b"FLAGS (", 1)[1].split(b")", 1)[0].decode()
                    out.append(set(flags.split()))
            return out
        finally:
            c.logout()

    def restart(self) -> None:
        subprocess.run(["docker", "restart", "-t", "1", self.name], check=True, capture_output=True)
        self.wait_ready()


@pytest.fixture(scope="module")
def greenmail():
    if not shutil.which("docker"):
        pytest.skip("docker not installed")
    gm = GreenMail(name=f"sendtox3-greenmail-{uuid.uuid4().hex[:8]}", imaps=_free_port(), smtp=_free_port())
    opts = (
        "-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 "
        f"-Dgreenmail.users={USER.split('@')[0]}:{PASSWORD}@{USER.split('@')[1]} -Dgreenmail.users.login=email"
    )
    subprocess.run(
        [
            "docker", "run", "-d", "--name", gm.name, "-e", f"GREENMAIL_OPTS={opts}",
            "-p", f"127.0.0.1:{gm.imaps}:3993", "-p", f"127.0.0.1:{gm.smtp}:3025", IMAGE,
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    try:
        gm.wait_ready()
        yield gm
    finally:
        subprocess.run(["docker", "rm", "-f", gm.name], capture_output=True)


class RecordingProcessor:
    def __init__(self) -> None:
        self.batches: list[tuple[float, list[IngestRequest]]] = []

    async def process_many(self, reqs: list[IngestRequest]):
        self.batches.append((time.monotonic(), list(reqs)))
        return [SimpleNamespace(title=r.title or r.url) for r in reqs]

    @property
    def titles(self) -> list[str]:
        return [r.title for _, b in self.batches for r in b]


@dataclass
class State:
    opts: Options
    store: Store
    processor: RecordingProcessor
    status: dict[str, Any] = field(default_factory=dict)


def mail(subject: str, *, ar: str | None = AR_PASS, frm: str = "Qiong Example <qiong@example.org>") -> bytes:
    m = EmailMessage()
    if ar:
        m["Authentication-Results"] = ar  # GreenMail adds none; this plays the provider's role
    m["From"] = frm
    m["To"] = "x3+k7q2@example.test"
    m["Subject"] = f"[x3] {subject}"
    m["Message-ID"] = f"<{uuid.uuid4().hex}@example.org>"
    m.set_content(f"{subject}: a note long enough to be a real reading item on the device, sent over SMTP.\n")
    return m.as_bytes(policy=SMTP)


async def until(pred, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return
        except KeyError:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out after {timeout:g} s waiting for {what}")


async def test_greenmail_idle_once_seen_move_and_restart(greenmail: GreenMail, opts: Options, store: Store):
    o = replace(
        opts,
        imap_enabled=True,
        imap_host="127.0.0.1",
        imap_port=greenmail.imaps,
        imap_user=USER,
        imap_password=PASSWORD,
        imap_folder="INBOX",
        imap_processed_folder="Processed",
        imap_plus_tag="k7q2",
        allowed_senders=["@example.org"],
        require_dmarc=True,
    )
    state = State(opts=o, store=store, processor=RecordingProcessor())
    task = asyncio.create_task(
        imapw.run(state, ssl_context=_insecure_ctx(), idle_seconds=4, backoff_min=0.5, backoff_max=2, timeout=10)
    )
    try:
        await until(lambda: state.status["imap"]["state"] == "idle", 30, "IDLE")
        st = state.status["imap"]
        assert st["mode"] == "idle" and st["connected"]

        # 1. IDLE pickup: well under the 60 s poll interval.
        t0 = time.monotonic()
        await asyncio.to_thread(greenmail.send, mail("First"))
        await until(lambda: len(state.processor.batches) == 1, 10, "first message")
        latency = state.processor.batches[0][0] - t0
        assert latency < 5, f"IDLE pickup took {latency:.1f} s"
        assert state.processor.titles == ["First"]
        req = state.processor.batches[0][1][0]
        assert req.kind == "text" and req.source == "email" and req.sender == "Qiong Example <qiong@example.org>"

        # 2. Flagged \Seen and moved to the processed folder (created by the worker).
        await until(lambda: greenmail.folder_state("INBOX") == [], 10, "INBOX emptied")
        processed = await asyncio.to_thread(greenmail.folder_state, "Processed")
        assert len(processed) == 1 and "\\Seen" in processed[0]

        # 3. A spoofed message (no DMARC pass) is reported, left unread in place, not processed.
        await asyncio.to_thread(
            greenmail.send, mail("Spoofed", ar="mx.example.test; dmarc=fail header.from=example.org")
        )
        await until(lambda: state.status["imap"]["messages_rejected"] == 1, 10, "rejection")
        inbox = await asyncio.to_thread(greenmail.folder_state, "INBOX")
        assert len(inbox) == 1 and "\\Seen" not in inbox[0], inbox

        # 4. Processed exactly once, even across several IDLE re-issues (idle_seconds=4).
        await asyncio.sleep(9)
        assert state.processor.titles == ["First"]
        assert state.status["imap"]["messages_processed"] == 1

        # 5. Server restart: the worker reconnects with backoff and keeps delivering.
        since = state.status["imap"]["connected_since"]
        await asyncio.to_thread(greenmail.restart)
        await until(
            lambda: state.status["imap"]["state"] == "idle" and state.status["imap"]["connected_since"] != since,
            60,
            "reconnect after restart",
        )
        assert state.status["imap"]["last_error"] == ""
        await asyncio.to_thread(greenmail.send, mail("After restart"))
        await until(lambda: len(state.processor.batches) == 2, 15, "message after restart")
        assert state.processor.titles == ["First", "After restart"]
        await until(lambda: len(greenmail.folder_state("Processed")) == 1, 10, "moved after restart")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
    assert state.status["imap"]["state"] == "stopped"
