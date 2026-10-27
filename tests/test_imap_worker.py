"""workers/imap.py against an in-process fake IMAP connection (no network).

The fake mimics imaplib's return shapes (``(typ, data)``, FETCH literals as tuples,
untagged responses dict, ``idle()`` context manager) and a server-side mailbox with
UIDs, flags, UIDVALIDITY, MOVE/COPY/EXPUNGE, so the worker's bookkeeping can be tested
precisely. The real-server test is tests/test_imap_greenmail.py.
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
import re
import threading
import time
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
from sendtox3.workers.imap import ImapWorker, encode_mailbox, parse_fetch, parse_search

PASSWORD = "s3cret-app-pass"
AR_PASS = "mx.example.test; dkim=pass header.d=example.org; dmarc=pass header.from=example.org"


# ============================================================================ fake server


@dataclass
class FakeMsg:
    raw: bytes
    flags: set[str] = field(default_factory=set)
    internaldate: float = field(default_factory=time.time)


@dataclass
class FakeFolder:
    uidvalidity: int
    next_uid: int = 1
    msgs: dict[int, FakeMsg] = field(default_factory=dict)
    generation: int = 0


class FakeServer:
    def __init__(
        self, caps: tuple[str, ...] = ("IMAP4REV1", "IDLE", "MOVE", "UIDPLUS"), *, uid_after_literal: bool = False
    ):
        self.cond = threading.Condition()
        self.caps = caps
        self.uid_after_literal = uid_after_literal
        self.folders: dict[str, FakeFolder] = {"INBOX": FakeFolder(uidvalidity=1000)}
        self.password = PASSWORD
        self.conns: list[FakeConn] = []
        self.connects = 0
        self.fail_connects = 0
        self.idles = 0
        self.full_fetches: list[int] = []
        self.commands: list[str] = []

    def deliver(self, raw: bytes, folder: str = "INBOX", *, seen: bool = False, date: float | None = None) -> int:
        with self.cond:
            f = self.folders[folder]
            uid = f.next_uid
            f.next_uid += 1
            f.msgs[uid] = FakeMsg(raw, {"\\Seen"} if seen else set(), time.time() if date is None else date)
            f.generation += 1
            self.cond.notify_all()
            return uid

    def kill_connections(self) -> None:
        with self.cond:
            for c in self.conns:
                c.closed = True
            self.cond.notify_all()

    def reset_uidvalidity(self, folder: str = "INBOX") -> None:
        with self.cond:
            old = self.folders[folder]
            new = FakeFolder(uidvalidity=old.uidvalidity + 1)
            for m in old.msgs.values():
                new.msgs[new.next_uid] = m
                new.next_uid += 1
            self.folders[folder] = new
            self.cond.notify_all()

    def connect(self, _opts: Options) -> FakeConn:
        with self.cond:
            self.connects += 1
            if self.fail_connects:
                self.fail_connects -= 1
                raise ConnectionRefusedError(61, "Connection refused")
            c = FakeConn(self)
            self.conns.append(c)
            return c

    def flags(self, uid: int, folder: str = "INBOX") -> set[str]:
        return self.folders[folder].msgs[uid].flags


def _unquote(s: Any) -> str:
    s = s.decode() if isinstance(s, bytes) else str(s)
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return s


def _uidset(spec: str, existing: list[int]) -> list[int]:
    out: set[int] = set()
    top = max(existing) if existing else 0
    for part in spec.split(","):
        if ":" in part:
            a, b = part.split(":")
            lo = int(a) if a != "*" else top
            hi = int(b) if b != "*" else top
            lo, hi = min(lo, hi), max(lo, hi)  # RFC 3501: n:* with n > top means top:n
            out.update(u for u in existing if lo <= u <= hi)
        elif part == "*":
            if top:
                out.add(top)
        else:
            out.add(int(part))
    return sorted(u for u in out if u in existing)


class FakeConn:
    def __init__(self, server: FakeServer):
        self.server = server
        self.capabilities = server.caps
        self.untagged_responses: dict[str, list[bytes]] = {}
        self.folder: str | None = None
        self.closed = False
        self.sock = None
        self.logged_in = False

    # -- helpers
    def _check(self) -> None:
        if self.closed:
            raise imaplib.IMAP4.abort("socket error: EOF")

    @property
    def _f(self) -> FakeFolder:
        return self.server.folders[self.folder]

    # -- commands
    def login(self, user: str, password: str):
        self._check()
        if password != self.server.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.logged_in = True
        return "OK", [b"LOGIN completed"]

    def select(self, box: str):
        self._check()
        name = _unquote(box)
        with self.server.cond:
            if name not in self.server.folders:
                return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
            self.folder = name
            f = self._f
            self.untagged_responses = {
                "EXISTS": [str(len(f.msgs)).encode()],
                "UIDVALIDITY": [str(f.uidvalidity).encode()],
                "UIDNEXT": [str(f.next_uid).encode()],
            }
            return "OK", [str(len(f.msgs)).encode()]

    def response(self, code: str):
        return code, self.untagged_responses.pop(code, [None])

    def status(self, box: str, names: str):
        f = self.server.folders[_unquote(box)]
        return "OK", [f'"{_unquote(box)}" (UIDVALIDITY {f.uidvalidity} UIDNEXT {f.next_uid})'.encode()]

    def list(self, directory: str = '""', pattern: str = "*"):
        self._check()
        name = _unquote(pattern)
        if name in self.server.folders:
            return "OK", [f'() "/" "{name}"'.encode()]
        return "OK", [None]

    def create(self, box: str):
        self._check()
        with self.server.cond:
            self.server.folders.setdefault(_unquote(box), FakeFolder(uidvalidity=5000))
        return "OK", [b"CREATE completed"]

    def noop(self):
        self._check()
        return "OK", [b"NOOP completed"]

    def expunge(self):
        self._check()
        with self.server.cond:
            for uid in [u for u, m in self._f.msgs.items() if "\\Deleted" in m.flags]:
                del self._f.msgs[uid]
        return "OK", [None]

    def logout(self):
        self.closed = True
        return "BYE", [b"logging out"]

    def shutdown(self):
        with self.server.cond:
            self.closed = True
            self.server.cond.notify_all()

    def idle(self, duration: float | None = None):
        return FakeIdler(self, duration)

    def uid(self, command: str, *args: str):
        self._check()
        command = command.upper()
        self.server.commands.append(command)
        with self.server.cond:
            f = self._f
            existing = sorted(f.msgs)
            if command == "SEARCH":
                return "OK", [" ".join(map(str, self._search(f, existing, list(args)))).encode()]
            if command == "FETCH":
                return "OK", self._fetch(f, _uidset(args[0], existing), args[1])
            if command == "STORE":
                spec, op, flags = args
                names = set(re.findall(r"\\\w+", flags))
                for u in _uidset(spec, existing):
                    if op.startswith("+"):
                        f.msgs[u].flags |= names
                    else:
                        f.msgs[u].flags -= names
                return "OK", [None]
            if command in ("MOVE", "COPY"):
                spec, box = args
                dest = self.server.folders.get(_unquote(box))
                if dest is None:
                    return "NO", [b"[TRYCREATE] no such mailbox"]
                for u in _uidset(spec, existing):
                    m = f.msgs[u]
                    dest.msgs[dest.next_uid] = FakeMsg(m.raw, set(m.flags))
                    dest.next_uid += 1
                    if command == "MOVE":
                        del f.msgs[u]
                return "OK", [None]
            if command == "EXPUNGE":
                for u in _uidset(args[0], existing):
                    if "\\Deleted" in f.msgs[u].flags:
                        del f.msgs[u]
                return "OK", [None]
        raise imaplib.IMAP4.error(f"unknown UID command {command}")

    @staticmethod
    def _search(f: FakeFolder, existing: list[int], args: list[str]) -> list[int]:
        uids = set(existing)
        i = 0
        while i < len(args):
            a = args[i].upper()
            if a == "UNSEEN":
                uids = {u for u in uids if "\\Seen" not in f.msgs[u].flags}
            elif a == "UNDELETED":
                uids = {u for u in uids if "\\Deleted" not in f.msgs[u].flags}
            elif a == "UID":
                i += 1
                uids &= set(_uidset(args[i], existing))
            elif a == "SINCE":  # RFC 3501: internal date (ignoring time and timezone) on or after
                i += 1
                since = time.strftime("%Y-%m-%d", time.strptime(args[i], "%d-%b-%Y"))
                uids = {u for u in uids if time.strftime("%Y-%m-%d", time.gmtime(f.msgs[u].internaldate)) >= since}
            i += 1
        return sorted(uids)

    def _fetch(self, f: FakeFolder, uids: list[int], parts: str) -> list[Any]:
        out: list[Any] = []
        seq_of = {u: n for n, u in enumerate(sorted(f.msgs), start=1)}
        for u in uids:
            m = f.msgs[u]
            seq = seq_of[u]
            if "BODY.PEEK[HEADER]" in parts:
                head = m.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                if self.server.uid_after_literal:
                    out.append((b"%d (RFC822.SIZE %d BODY[HEADER] {%d}" % (seq, len(m.raw), len(head)), head))
                    out.append(b" UID %d)" % u)
                else:
                    out.append((b"%d (UID %d RFC822.SIZE %d BODY[HEADER] {%d}" % (seq, u, len(m.raw), len(head)), head))
                    out.append(b")")
            elif "BODY.PEEK[]" in parts:
                self.server.full_fetches.append(u)
                out.append((b"%d (UID %d BODY[] {%d}" % (seq, u, len(m.raw)), m.raw))
                out.append(b")")
            else:
                out.append(b"%d (UID %d)" % (seq, u))
        return out


class FakeIdler:
    def __init__(self, conn: FakeConn, duration: float | None):
        self.conn = conn
        self.duration = duration

    def __enter__(self):
        self.conn._check()
        self.conn.server.idles += 1
        self.gen = self.conn._f.generation
        self.deadline = time.monotonic() + (self.duration or 3600)
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        server = self.conn.server
        with server.cond:
            while True:
                if self.conn.closed:
                    raise imaplib.IMAP4.abort("socket error: EOF")
                f = self.conn._f
                if f.generation != self.gen:
                    self.gen = f.generation
                    return "EXISTS", [str(len(f.msgs)).encode()]
                left = self.deadline - time.monotonic()
                if left <= 0:
                    raise StopIteration
                server.cond.wait(min(left, 0.05))


# ============================================================================ app fakes


class FakeProcessor:
    def __init__(self) -> None:
        self.batches: list[list[IngestRequest]] = []
        self.fail_times = 0

    async def process_many(self, reqs: list[IngestRequest]):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("processor exploded")
        self.batches.append(list(reqs))
        return [SimpleNamespace(title=r.title or r.url, id=f"item{len(self.batches)}") for r in reqs]

    @property
    def requests(self) -> list[IngestRequest]:
        return [r for b in self.batches for r in b]


@dataclass
class State:
    opts: Options
    store: Store
    processor: FakeProcessor
    status: dict[str, Any] = field(default_factory=dict)


def mail(
    subject: str = "Hello",
    body: str = "A note with enough words to be a real note for the reading queue on the device.",
    *,
    frm: str = "Qiong Example <qiong@example.org>",
    to: str = "x3reader+k7q2@example.test",
    ar: str | None = AR_PASS,
) -> bytes:
    m = EmailMessage()
    if ar:
        m["Authentication-Results"] = ar
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    m["Message-ID"] = f"<{subject.replace(' ', '.')}.{time.monotonic_ns()}@example.org>"
    m.set_content(body + "\n")
    return m.as_bytes(policy=SMTP)


@pytest.fixture
def iopts(opts: Options) -> Options:
    return replace(
        opts,
        imap_enabled=True,
        imap_host="imap.example.test",
        imap_port=993,
        imap_user="x3reader@example.test",
        imap_password=PASSWORD,
        imap_folder="INBOX",
        imap_processed_folder="Processed",
        imap_plus_tag="k7q2",
        allowed_senders=["@example.org"],
        require_dmarc=True,
    )


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def state(iopts, store) -> State:
    return State(opts=iopts, store=store, processor=FakeProcessor())


def start(state: State, server: FakeServer, **kw: Any) -> asyncio.Task:
    kw.setdefault("connect", server.connect)
    kw.setdefault("idle_seconds", 2)
    kw.setdefault("poll_seconds", 0.05)
    kw.setdefault("backoff_min", 0.05)
    kw.setdefault("backoff_max", 0.2)
    return asyncio.create_task(imapw.run(state, **kw))


async def until(pred, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return
        except KeyError:  # status["imap"] appears once the task has started
            pass
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


# ============================================================================ tests


async def test_first_run_processes_unread_once_flags_and_moves(state, server):
    old_read = server.deliver(mail("Old read"), seen=True)
    first = server.deliver(mail("First"))
    second = server.deliver(mail("Second"))
    task = start(state, server)
    await until(lambda: len(state.processor.batches) == 2, what="two messages processed")
    await until(lambda: state.status["imap"]["state"] == "idle", what="idle")
    try:
        assert [r.title for r in state.processor.requests] == ["First", "Second"]
        assert all(
            r.source == "email" and r.sender == "Qiong Example <qiong@example.org>" for r in state.processor.requests
        )
        inbox = server.folders["INBOX"].msgs
        assert set(inbox) == {old_read}  # processed mail moved out
        assert len(server.folders["Processed"].msgs) == 2
        assert all("\\Seen" in m.flags for m in server.folders["Processed"].msgs.values())
        st = state.status["imap"]
        assert st["connected"] and st["mode"] == "idle"
        assert st["messages_processed"] == 2 and st["items_created"] == 2
        assert st["last_error"] == ""
        key = "imap:x3reader@example.test@imap.example.test:993/INBOX"
        assert state.store.kv_get(key + ":uidvalidity") == "1000"
        assert int(state.store.kv_get(key + ":last_uid")) >= second
        assert first in server.full_fetches and old_read not in server.full_fetches
    finally:
        await stop(task)
    assert state.status["imap"]["state"] == "stopped"


async def test_idle_wakes_on_new_mail_and_already_read_new_mail_is_processed(state, server):
    task = start(state, server)
    await until(lambda: server.idles >= 1, what="first IDLE")
    try:
        server.deliver(mail("While idle"))
        await until(lambda: len(state.processor.batches) == 1, timeout=1.5, what="IDLE pickup")
        # A phone opened the next one before the Pi saw it: still processed (UID-based).
        server.deliver(mail("Opened on phone"), seen=True)
        await until(lambda: len(state.processor.batches) == 2, timeout=1.5, what="seen-but-new pickup")
        assert [r.title for r in state.processor.requests] == ["While idle", "Opened on phone"]
    finally:
        await stop(task)


async def test_restart_does_not_reprocess(state, server, iopts, store):
    server.deliver(mail("Once"))
    task = start(state, server)
    await until(lambda: len(state.processor.batches) == 1)
    await stop(task)
    # Same store, new worker; the message was moved, but even if it was not, the
    # high-water mark prevents a second pass.
    state2 = State(opts=replace(iopts, imap_processed_folder=""), store=store, processor=FakeProcessor())
    server.deliver(mail("Twice"))
    task = start(state2, server)
    await until(lambda: len(state2.processor.batches) == 1)
    await until(lambda: state2.status["imap"]["state"] == "idle")
    await stop(task)
    assert [r.title for r in state2.processor.requests] == ["Twice"]


async def test_no_processed_folder_leaves_mail_flagged_in_place(state, server):
    state.opts = replace(state.opts, imap_processed_folder="")
    uid = server.deliver(mail("Stay"))
    task = start(state, server)
    await until(lambda: len(state.processor.batches) == 1)
    await until(lambda: state.status["imap"]["state"] == "idle")
    await stop(task)
    assert "\\Seen" in server.flags(uid)
    assert "Processed" not in server.folders


async def test_rejections(state, server, caplog):
    caplog.set_level(logging.DEBUG, logger="sendtox3")
    spoof = server.deliver(mail("Spoof", ar="mx.example.test; dmarc=fail header.from=example.org"))
    stranger = server.deliver(
        mail("Stranger", frm="Someone <someone@else.test>", ar=AR_PASS.replace("example.org", "else.test"))
    )
    not_ours = server.deliver(mail("Not ours", to="x3reader@example.test"))
    good = server.deliver(mail("Good"))
    task = start(state, server)
    await until(lambda: len(state.processor.batches) == 1)
    await until(lambda: state.status["imap"]["state"] == "idle")
    await stop(task)
    assert [r.title for r in state.processor.requests] == ["Good"]
    inbox = server.folders["INBOX"].msgs
    # Refused mail is never touched: ours (reported) and someone else's in a shared inbox alike.
    assert inbox[spoof].flags == set() and inbox[stranger].flags == set() and inbox[not_ours].flags == set()
    assert good not in inbox  # moved
    st = state.status["imap"]
    assert st["messages_rejected"] == 2 and "allowed_senders" in st["last_reject_reason"]
    assert set(server.full_fetches) == {good}  # rejected on headers alone, bodies never downloaded
    assert "DMARC failed" in caplog.text


async def test_oversized_message_rejected_without_download(state, server):
    big = mail("Huge", body="x" * 200)
    uid = server.deliver(big)
    # Pretend the server reports a giant RFC822.SIZE.
    orig = FakeConn._fetch

    def fake_fetch(self, f, uids, parts):
        out = orig(self, f, uids, parts)
        return [
            (re.sub(rb"RFC822\.SIZE \d+", b"RFC822.SIZE 99999999", o[0]), o[1]) if isinstance(o, tuple) else o
            for o in out
        ]

    FakeConn._fetch = fake_fetch
    try:
        task = start(state, server)
        await until(lambda: state.status["imap"]["messages_rejected"] == 1)
        await stop(task)
    finally:
        FakeConn._fetch = orig
    assert uid not in server.full_fetches and state.processor.batches == []
    assert "MB" in state.status["imap"]["last_reject_reason"]


async def test_uid_after_literal_fetch_format(state):
    server = FakeServer(uid_after_literal=True)
    server.deliver(mail("Gmail style"))
    task = start(state, server)
    await until(lambda: len(state.processor.batches) == 1)
    await stop(task)


async def test_poll_mode_without_idle(state):
    server = FakeServer(caps=("IMAP4REV1", "UIDPLUS", "MOVE"))
    task = start(state, server, poll_seconds=0.05)
    await until(lambda: state.status["imap"]["state"] == "polling")
    try:
        assert state.status["imap"]["mode"] == "poll"
        server.deliver(mail("Polled"))
        await until(lambda: len(state.processor.batches) == 1, what="poll pickup")
        assert server.idles == 0
    finally:
        await stop(task)


async def test_copy_delete_expunge_without_move(state):
    server = FakeServer(caps=("IMAP4REV1", "IDLE", "UIDPLUS"))
    server.deliver(mail("Copied"))
    task = start(state, server)
    await until(lambda: len(server.folders.get("Processed", SimpleNamespace(msgs={})).msgs) == 1)
    await stop(task)
    assert server.folders["INBOX"].msgs == {}
    assert "MOVE" not in server.commands and {"COPY", "EXPUNGE"} <= set(server.commands)


async def test_reconnects_after_connection_drop_with_backoff(state, server):
    task = start(state, server)
    await until(lambda: server.idles >= 1)
    try:
        server.fail_connects = 2
        server.kill_connections()
        await until(lambda: state.status["imap"]["state"] == "backoff", what="backoff")
        assert "connection" in state.status["imap"]["last_error"]
        await until(lambda: server.connects >= 4 and state.status["imap"]["state"] == "idle", what="reconnected")
        server.deliver(mail("After reconnect"))
        await until(lambda: len(state.processor.batches) == 1)
        assert state.status["imap"]["connected"]
    finally:
        await stop(task)


async def test_login_failure_is_reported_without_password(state, server, caplog):
    caplog.set_level(logging.DEBUG)
    server.password = "different"
    task = start(state, server, backoff_max=0.1)
    await until(lambda: server.connects >= 2, what="retry after auth failure")
    await stop(task)
    st = state.status["imap"]
    assert "login failed" in st["last_error"] and "Invalid credentials" in st["last_error"]
    assert PASSWORD not in caplog.text and PASSWORD not in repr(st)


async def test_processor_failure_retries_then_succeeds(state, server):
    state.processor.fail_times = 2
    server.deliver(mail("Flaky"))
    server.deliver(mail("After flaky"))
    task = start(state, server, idle_seconds=1)
    try:
        await until(lambda: len(state.processor.batches) == 2, timeout=8, what="retry success")
        assert [r.title for r in state.processor.requests] == ["Flaky", "After flaky"]  # order kept
    finally:
        await stop(task)


async def test_processor_failing_forever_gives_up_after_max_attempts(state, server):
    state.processor.fail_times = 99
    uid = server.deliver(mail("Poison"))
    task = start(state, server, idle_seconds=1)
    try:
        await until(lambda: "\\Seen" in server.flags(uid), timeout=10, what="give up")
        assert state.processor.fail_times == 99 - imapw.MAX_ATTEMPTS
    finally:
        await stop(task)
    assert "processing failed" in state.status["imap"]["last_error"]


async def test_uidvalidity_change_rescans_unread_only(state, server):
    state.opts = replace(state.opts, imap_processed_folder="")
    server.deliver(mail("Before reset"))
    task = start(state, server)
    await until(lambda: len(state.processor.batches) == 1)
    await until(lambda: state.status["imap"]["state"] == "idle")
    server.reset_uidvalidity()
    server.deliver(mail("After reset"))
    server.kill_connections()
    await until(lambda: len(state.processor.batches) == 2, what="processed after reset")
    await until(lambda: state.status["imap"]["state"] == "idle")
    await stop(task)
    assert [r.title for r in state.processor.requests] == ["Before reset", "After reset"]


async def test_mail_arriving_during_processing_is_not_left_waiting(state, server):
    class SlowProcessor(FakeProcessor):
        async def process_many(self, reqs):
            if not self.batches:
                server.deliver(mail("Arrived meanwhile"))
            return await super().process_many(reqs)

    state.processor = SlowProcessor()
    server.deliver(mail("First"))
    task = start(state, server, idle_seconds=30)
    try:
        await until(
            lambda: len(state.processor.batches) == 2, timeout=2, what="second without waiting for IDLE timeout"
        )
    finally:
        await stop(task)


async def test_cancel_during_idle_is_prompt(state, server):
    task = start(state, server, idle_seconds=1500)
    await until(lambda: server.idles >= 1)
    t0 = time.monotonic()
    await stop(task)
    assert time.monotonic() - t0 < 2
    assert state.status["imap"]["state"] == "stopped"


async def test_missing_folder_is_a_config_error(state, server):
    state.opts = replace(state.opts, imap_folder="Send to X3")
    task = start(state, server)
    await until(lambda: state.status["imap"]["state"] == "backoff")
    await stop(task)
    assert "cannot open folder 'Send to X3'" in state.status["imap"]["last_error"]


async def test_disabled_and_unconfigured_return_immediately(state, server):
    state.opts = replace(state.opts, imap_enabled=False)
    await asyncio.wait_for(imapw.run(state, connect=server.connect), 1)
    assert state.status["imap"]["state"] == "disabled"
    state.opts = replace(state.opts, imap_enabled=True, imap_password="")
    await asyncio.wait_for(imapw.run(state, connect=server.connect), 1)
    assert state.status["imap"]["state"] == "not_configured"
    assert "imap_password" in state.status["imap"]["last_error"]
    assert state.status["imap"]["enabled"] is False  # the app supervisor must not restart it
    assert server.connects == 0


async def test_state_without_status_dict_still_works(iopts, store, server):
    class Bare:
        def __init__(self):
            self.opts, self.store, self.processor = replace(iopts, imap_enabled=False), store, FakeProcessor()

    st = Bare()
    await imapw.run(st, connect=server.connect)
    assert st.status["imap"]["state"] == "disabled"


# ============================================================================ helpers


def test_encode_mailbox():
    assert encode_mailbox("INBOX") == '"INBOX"'
    assert encode_mailbox("Send to X3/Done") == '"Send to X3/Done"'
    assert encode_mailbox("Gelesen/Ärger") == '"Gelesen/&AMQ-rger"'
    assert encode_mailbox("R&D") == '"R&-D"'
    assert encode_mailbox("&AMQ-rger") == '"&AMQ-rger"'  # already modified UTF-7
    assert encode_mailbox('a"b\\c') == '"a\\"b\\\\c"'
    assert encode_mailbox("台北/日本語") == '"&U,BTFw-/&ZeVnLIqe-"'  # the RFC 3501 5.1.3 example


def test_parse_fetch_shapes():
    data = [
        (b"1 (UID 7 RFC822.SIZE 120 BODY[HEADER] {5}", b"H: 1\r"),
        b")",
        (b"2 (RFC822.SIZE 99 BODY[HEADER] {3}", b"abc"),
        b" UID 9 FLAGS (\\Seen))",
        b"3 (UID 7 FLAGS (\\Seen))",  # unsolicited flag update must not wipe the header
        (b"4 (UID 11 BODY[] {4}", b"body"),
        b")",
    ]
    recs = parse_fetch(data)
    assert recs[7]["header"] == b"H: 1\r" and recs[7]["size"] == 120 and recs[7]["flags"] == ["\\Seen"]
    assert recs[9] == {"size": 99, "header": b"abc", "uid": 9, "flags": ["\\Seen"]}
    assert recs[11]["body"] == b"body"
    assert parse_fetch([]) == {} and parse_fetch([None]) == {}


def test_parse_search():
    assert parse_search([b"3 1 2"]) == [1, 2, 3]
    assert parse_search([b""]) == [] and parse_search([None]) == []


async def test_real_imaplib_errors_are_survived(state):
    """With the real imaplib path (no fake), an unreachable host becomes backoff, not a crash."""
    state.opts = replace(state.opts, imap_host="127.0.0.1", imap_port=1)  # nothing listens on port 1
    task = asyncio.create_task(imapw.run(state, backoff_min=0.05, backoff_max=0.1, timeout=2))
    await until(lambda: state.status["imap"]["state"] == "backoff")
    await stop(task)
    assert "refused" in state.status["imap"]["last_error"] or "connection" in state.status["imap"]["last_error"]


def test_worker_rejects_plaintext_port_143_without_starttls(state, monkeypatch):
    """Port 143 needs STARTTLS; a server that lacks it must never receive the password."""
    import socket as _s

    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    received: list[bytes] = []

    def serve():
        c, _ = srv.accept()
        c.sendall(b"* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN] fake ready\r\n")
        c.settimeout(3)
        data = b""
        try:
            while chunk := c.recv(1024):
                data += chunk
        except OSError:
            pass
        received.append(data)
        c.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    real_imap4 = imapw.imaplib.IMAP4

    class ToEphemeralPort(real_imap4):  # the worker asks for :143, we redirect to the test port
        def __init__(self, host, _port, timeout=None):
            super().__init__(host, port, timeout=timeout)

    monkeypatch.setattr(imapw.imaplib, "IMAP4", ToEphemeralPort)
    st = SimpleNamespace(
        opts=replace(state.opts, imap_host="127.0.0.1", imap_port=143), store=state.store, processor=None, status={}
    )
    w = ImapWorker(st, timeout=3)
    try:
        with pytest.raises(imapw.ImapConfigError, match="STARTTLS"):
            w._open()
    finally:
        t.join(5)
        srv.close()
    assert received and PASSWORD.encode() not in received[0] and b"LOGIN" not in received[0].upper()
