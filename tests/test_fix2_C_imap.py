"""Review fixes round 2, group C (R2-C3): which failures count as a processing attempt.

A lost connection, a shutdown or a StorageError leaves the attempt count as it was; any
other failure (a NO to the FETCH, MemoryError while parsing, a bug) counts, so a message
that keeps failing is given up (marked read) after MAX_ATTEMPTS instead of being retried
forever. Uses the fake IMAP server of test_imap_worker.
"""

from __future__ import annotations

import asyncio
import imaplib
import threading

import pytest

from sendtox3.ingest import StorageError
from sendtox3.workers import imap as imapw

from test_imap_worker import FakeConn, FakeProcessor, FakeServer, mail, start, stop, until
from test_imap_worker import iopts, server, state  # noqa: F401  (fixtures used below)

KEY = "imap:x3reader@example.test@imap.example.test:993/INBOX"
POLL_CAPS = ("IMAP4REV1", "MOVE", "UIDPLUS")  # no IDLE: retries come every poll (fast in tests)


class FetchTroubleServer(FakeServer):
    """Answers the full FETCH of ``bad_uid`` with ``reply`` (``"NO"``, or ``"abort"`` to
    drop the connection) ``times`` times."""

    def __init__(self, reply: str, times: int = 10**6):
        super().__init__(caps=POLL_CAPS)
        self.reply, self.times, self.bad_uid, self.troubles = reply, times, 0, 0

    def connect(self, opts):
        conn = super().connect(opts)
        server = self

        class Conn(FakeConn):
            def uid(self, command, *args):
                if (command.upper() == "FETCH" and "BODY.PEEK[]" in args[1] and args[0] == str(server.bad_uid)
                        and server.troubles < server.times):
                    server.troubles += 1
                    if server.reply == "abort":
                        raise imaplib.IMAP4.abort("socket error: EOF")
                    return "NO", [b"Some messages could not be FETCHed (Failure)"]
                return super().uid(command, *args)

        conn.__class__ = Conn
        return conn


def attempts(store) -> str:
    return store.kv_get(KEY + ":attempts") or ""


# ------------------------------------------------- failures of the message's own count


async def test_a_message_that_crashes_the_parser_is_given_up_after_max_attempts(state, store, monkeypatch):  # noqa: F811
    server = FakeServer(caps=POLL_CAPS)
    real = imapw.parse_email
    parses = 0

    def parse(raw, **kw):
        nonlocal parses
        if not kw.get("headers_only") and b"Subject: Poison" in raw:
            parses += 1
            raise MemoryError("out of memory")
        return real(raw, **kw)

    monkeypatch.setattr(imapw, "parse_email", parse)
    poison = server.deliver(mail("Poison"))
    later = server.deliver(mail("Later"))
    task = start(state, server, poll_seconds=0.05)
    try:
        await until(lambda: "\\Seen" in server.flags(poison), what="poison message given up")
        await until(lambda: later not in server.folders["INBOX"].msgs, what="later mail processed and moved")
    finally:
        await stop(task)
    assert parses == imapw.MAX_ATTEMPTS  # not retried forever
    assert [r.title for r in state.processor.requests] == ["Later"]
    assert "parsing failed" in state.status["imap"]["last_error"] or "gave up" in state.status["imap"]["last_error"]
    assert attempts(store) == ""


async def test_a_message_the_server_refuses_to_fetch_is_given_up_after_max_attempts(state, store):  # noqa: F811
    server = FetchTroubleServer("NO")
    server.bad_uid = poison = server.deliver(mail("Unfetchable"))
    server.deliver(mail("Later"))
    task = start(state, server, poll_seconds=0.05)
    try:
        await until(lambda: "\\Seen" in server.flags(poison), what="unfetchable message given up")
        await until(lambda: len(state.processor.batches) == 1, what="later mail processed")
    finally:
        await stop(task)
    assert server.troubles == imapw.MAX_ATTEMPTS
    assert [r.title for r in state.processor.requests] == ["Later"]


async def test_an_unexpected_processing_error_counts(state, store):  # noqa: F811
    server = FakeServer(caps=POLL_CAPS)

    class Broken(FakeProcessor):
        calls = 0

        async def process_many(self, reqs):
            Broken.calls += 1
            raise MemoryError("out of memory")

    state.processor = Broken()
    uid = server.deliver(mail("Poison"))
    task = start(state, server, poll_seconds=0.05)
    try:
        await until(lambda: "\\Seen" in server.flags(uid), what="given up")
    finally:
        await stop(task)
    assert Broken.calls == imapw.MAX_ATTEMPTS
    assert "processing failed" in state.status["imap"]["last_error"]


# ------------------------------------------- failures that are not the message's fault


async def test_a_shutdown_while_processing_restores_the_attempt_count(state, server, store):  # noqa: F811
    started = asyncio.Event()

    class Hangs(FakeProcessor):
        async def process_many(self, reqs):
            started.set()
            await asyncio.Event().wait()  # until cancelled

    state.processor = Hangs()
    uid = server.deliver(mail("Interrupted"))
    store.kv_set(KEY + ":attempts", f"1000:{uid}:{imapw.MAX_ATTEMPTS - 1}")  # one attempt left
    task = start(state, server)
    await asyncio.wait_for(started.wait(), 5)
    assert attempts(store) == f"1000:{uid}:{imapw.MAX_ATTEMPTS}"  # counted while it runs ...
    await stop(task)
    assert attempts(store) == f"1000:{uid}:{imapw.MAX_ATTEMPTS - 1}"  # ... and put back on shutdown
    assert server.flags(uid) == set()

    state.processor = FakeProcessor()  # the next start of the app still processes it
    task = start(state, server)
    try:
        await until(lambda: len(state.processor.batches) == 1, what="processed after the restart")
    finally:
        await stop(task)
    assert attempts(store) == ""


async def test_a_shutdown_while_parsing_restores_the_attempt_count(state, server, store, monkeypatch):  # noqa: F811
    real = imapw.parse_email
    parsing, release = threading.Event(), threading.Event()

    def slow(raw, **kw):
        if not kw.get("headers_only"):
            parsing.set()
            release.wait(5)
        return real(raw, **kw)

    monkeypatch.setattr(imapw, "parse_email", slow)
    uid = server.deliver(mail("Slow"))
    task = start(state, server)
    try:
        await until(parsing.is_set, what="parsing started")
        assert attempts(store) == f"1000:{uid}:1"
        await stop(task)
    finally:
        release.set()
    assert attempts(store) == ""  # restored to "no attempt yet"
    assert server.flags(uid) == set()


async def test_a_lost_connection_during_the_fetch_does_not_count(state, store):  # noqa: F811
    server = FetchTroubleServer("abort", times=imapw.MAX_ATTEMPTS + 2)
    server.bad_uid = uid = server.deliver(mail("Flaky line"))
    task = start(state, server, poll_seconds=0.05)
    try:
        # Processed and moved, not given up (marked read and left in place).
        await until(lambda: uid not in server.folders["INBOX"].msgs, what="processed once the line holds")
    finally:
        await stop(task)
    assert server.troubles == imapw.MAX_ATTEMPTS + 2  # more drops than the limit, still not given up
    assert [r.title for r in state.processor.requests] == ["Flaky line"]


@pytest.mark.parametrize(
    ("exc", "counts"),
    [
        (imaplib.IMAP4.abort("socket error: EOF"), False),
        (ConnectionResetError(54, "reset"), False),
        (TimeoutError("timed out"), False),
        (EOFError(), False),
        (StorageError("disk full"), False),
        (asyncio.CancelledError(), False),
        (imaplib.IMAP4.error("FETCH failed: NO"), True),
        (MemoryError(), True),
        (RecursionError(), True),
        (ValueError("bug"), True),
    ],
)
def test_which_failures_count_as_an_attempt(exc, counts):
    assert imapw._not_the_messages_fault(exc) is not counts


async def test_unreadable_headers_are_left_alone_not_retried_forever(state, server, store, monkeypatch):  # noqa: F811
    real = imapw.parse_email

    def parse(raw, **kw):
        if kw.get("headers_only") and b"Subject: Odd" in raw:
            raise ValueError("header parser bug")
        return real(raw, **kw)

    monkeypatch.setattr(imapw, "parse_email", parse)
    odd = server.deliver(mail("Odd"))
    server.deliver(mail("Fine"))
    task = start(state, server)
    try:
        await until(lambda: len(state.processor.batches) == 1, what="the next message processed")
    finally:
        await stop(task)
    assert [r.title for r in state.processor.requests] == ["Fine"]
    assert server.flags(odd) == set()  # rejected like any mail that is not ours: left unread
    assert server.connects == 1  # no reconnect loop
