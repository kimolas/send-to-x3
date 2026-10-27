"""Review fixes, group C: workers/imap.py (personal INBOX, poison messages, storage
errors, parsing off the event loop). Uses the fake IMAP server of test_imap_worker."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace

import pytest

from sendtox3.ingest import StorageError
from sendtox3.workers import imap as imapw

from test_imap_worker import AR_PASS, FakeFolder, FakeProcessor, FakeServer, State, mail, start, stop, until
from test_imap_worker import iopts, server, state  # noqa: F401  (fixtures used below)

KEY = "imap:x3reader@example.test@imap.example.test:993/INBOX"


def other(i: int) -> bytes:
    """Ordinary mail from someone else, DMARC-authenticated for their own domain."""
    return mail(f"Personal {i}", frm=f"Friend <f{i}@else.test>", to="x3reader@example.test",
                ar=AR_PASS.replace("example.org", "else.test"))


@pytest.fixture
def inbox(iopts, store):  # noqa: F811
    """The defaults most people start from: INBOX, no plus-tag, no processed folder."""
    opts = replace(iopts, imap_plus_tag="", imap_processed_folder="", allowed_senders=["@example.org"])
    return State(opts=opts, store=store, processor=FakeProcessor())


# ----------------------------------------------- finding 3: the user's own INBOX


async def test_untagged_inbox_leaves_other_mail_unread(inbox, server, store):  # noqa: F811
    others = [server.deliver(other(i)) for i in range(5)]
    ours = server.deliver(mail("For the X3", to="x3reader@example.test"))
    task = start(inbox, server)
    try:
        await until(lambda: len(inbox.processor.batches) == 1, what="our message processed")
        await until(lambda: inbox.status["imap"]["state"] == "idle")
        later = server.deliver(other(9))  # arrives while idling
        await until(lambda: int(store.kv_get(KEY + ":last_uid") or 0) >= later, what="later mail looked at")
    finally:
        await stop(task)
    assert [r.title for r in inbox.processor.requests] == ["For the X3"]
    assert "\\Seen" in server.flags(ours)
    assert all(server.flags(u) == set() for u in [*others, later])  # someone else's mail: untouched
    assert inbox.status["imap"]["messages_rejected"] == 0
    assert "INBOX" in inbox.status["imap"]["warning"] and "imap_plus_tag" in inbox.status["imap"]["warning"]


async def test_untagged_inbox_first_check_skips_the_old_unread_backlog(inbox, server, store):  # noqa: F811
    old = server.deliver(mail("Unread for weeks", to="x3reader@example.test"), date=time.time() - 20 * 86_400)
    new = server.deliver(mail("Sent just now", to="x3reader@example.test"))
    task = start(inbox, server)
    try:
        await until(lambda: len(inbox.processor.batches) == 1)
        await until(lambda: inbox.status["imap"]["state"] == "idle")
    finally:
        await stop(task)
    assert [r.title for r in inbox.processor.requests] == ["Sent just now"]
    assert server.flags(old) == set() and "\\Seen" in server.flags(new)
    assert int(store.kv_get(KEY + ":last_uid")) >= new  # and it is not looked at again later


async def test_untagged_inbox_reports_a_rejected_message_from_an_allowed_sender_without_flagging_it(
    inbox, server  # noqa: F811
):
    spoof = server.deliver(
        mail("Spoof", to="x3reader@example.test", ar="mx.example.test; dmarc=fail header.from=example.org")
    )
    task = start(inbox, server)
    try:
        await until(lambda: inbox.status["imap"]["messages_rejected"] == 1)
    finally:
        await stop(task)
    assert server.flags(spoof) == set()
    assert "DMARC failed" in inbox.status["imap"]["last_reject_reason"]


async def test_dedicated_folder_without_tag_reports_rejected_mail_and_leaves_it_unread(inbox, server):  # noqa: F811
    server.folders["Send to X3"] = FakeFolder(uidvalidity=77)
    inbox.opts = replace(inbox.opts, imap_folder="Send to X3")
    stranger = server.deliver(other(1), folder="Send to X3")
    task = start(inbox, server)
    try:
        await until(lambda: inbox.status["imap"]["messages_rejected"] == 1)
    finally:
        await stop(task)
    assert server.flags(stranger, "Send to X3") == set()
    assert "allowed_senders" in inbox.status["imap"]["last_reject_reason"]
    assert "INBOX" not in inbox.status["imap"]["warning"]


# ------------------------------------------------- finding 16: fail closed by default


async def test_worker_without_sender_filter_does_not_read_any_mail(inbox, server, store):  # noqa: F811
    inbox.opts = replace(inbox.opts, allowed_senders=[], imap_plus_tag="")
    uid = server.deliver(other(1))
    await asyncio.wait_for(imapw.run(inbox, connect=server.connect), 2)  # returns at once
    st = inbox.status["imap"]
    assert server.connects == 0 and server.flags(uid) == set()
    assert st["state"] == "not_configured" and st["enabled"] is False  # no restart loop
    assert "allowed_senders" in st["last_error"] and "imap_plus_tag" in st["last_error"] and st["warning"]
    assert store.kv_get(KEY + ":last_uid") is None  # nothing skipped over: read once a filter is set


# ------------------------------------------- finding 5: poison messages across restarts


async def test_attempt_is_recorded_before_the_message_is_processed(state, server, store):  # noqa: F811
    seen: list[str | None] = []

    class Recording(FakeProcessor):
        async def process_many(self, reqs):
            seen.append(store.kv_get(KEY + ":attempts"))
            return await super().process_many(reqs)

    state.processor = Recording()
    uid = server.deliver(mail("Recorded"))
    task = start(state, server)
    try:
        await until(lambda: len(state.processor.batches) == 1)
        await until(lambda: state.status["imap"]["state"] == "idle")
    finally:
        await stop(task)
    assert seen == [f"1000:{uid}:1"]  # a crash during processing would leave this behind
    assert not store.kv_get(KEY + ":attempts")  # cleared once done


async def test_attempt_also_covers_parsing_the_message(state, server, store, monkeypatch):  # noqa: F811
    during_parse: list[str | None] = []
    real = imapw.parse_email

    def spy(raw, **kw):
        if not kw.get("headers_only"):
            during_parse.append(store.kv_get(KEY + ":attempts"))
        return real(raw, **kw)

    monkeypatch.setattr(imapw, "parse_email", spy)
    uid = server.deliver(mail("Parsed"))
    task = start(state, server)
    try:
        await until(lambda: len(state.processor.batches) == 1)
    finally:
        await stop(task)
    assert during_parse == [f"1000:{uid}:1"]  # a message that hangs the parser is counted too


async def test_message_that_kept_crashing_the_app_is_given_up_after_restarts(state, server, store):  # noqa: F811
    uid = server.deliver(mail("Poison"))
    # What three runs that died (watchdog restart) while processing it leave behind.
    store.kv_set(KEY + ":attempts", f"1000:{uid}:{imapw.MAX_ATTEMPTS}")
    task = start(state, server)
    try:
        await until(lambda: uid not in server.folders["INBOX"].msgs or "\\Seen" in server.flags(uid),
                    what="poison message given up")
        await until(lambda: state.status["imap"]["state"] == "idle")
    finally:
        await stop(task)
    assert state.processor.batches == []  # not processed a fourth time
    assert "gave up" in state.status["imap"]["last_error"]


async def test_attempts_of_another_message_do_not_count(state, server, store):  # noqa: F811
    uid = server.deliver(mail("Fine"))
    store.kv_set(KEY + ":attempts", f"1000:{uid + 7}:{imapw.MAX_ATTEMPTS}")
    task = start(state, server)
    try:
        await until(lambda: len(state.processor.batches) == 1)
    finally:
        await stop(task)


# ------------------------------------------------------ shared decision: StorageError


async def test_storage_error_keeps_the_message_unread_and_retries_without_giving_up(state, store):  # noqa: F811
    server = FakeServer(caps=("IMAP4REV1", "MOVE", "UIDPLUS"))  # poll mode: quick retries

    class DiskFull(FakeProcessor):
        failures = imapw.MAX_ATTEMPTS + 2

        async def process_many(self, reqs):
            if self.failures:
                self.failures -= 1
                assert "\\Seen" not in server.flags(uid)
                raise StorageError("database or disk is full")
            return await super().process_many(reqs)

    state.processor = DiskFull()
    uid = server.deliver(mail("Saved later"))
    task = start(state, server, poll_seconds=0.05)
    try:
        await until(lambda: state.processor.failures == 0, what="storage failures")
        assert "could not save" in state.status["imap"]["last_error"]
        await until(lambda: len(state.processor.batches) == 1, what="processed once storage works")
        await until(lambda: len(server.folders["Processed"].msgs) == 1, what="moved after success")
    finally:
        await stop(task)
    assert [r.title for r in state.processor.requests] == ["Saved later"]
    assert not store.kv_get(KEY + ":attempts")


# ----------------------------------------------------- finding 37: off the event loop


async def test_messages_are_parsed_off_the_event_loop(state, server, monkeypatch):  # noqa: F811
    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []
    real = imapw.parse_email

    def spy(raw, **kw):
        threads.append(threading.current_thread())
        return real(raw, **kw)

    monkeypatch.setattr(imapw, "parse_email", spy)
    server.deliver(mail("Parsed in a thread"))
    task = start(state, server)
    try:
        await until(lambda: len(state.processor.batches) == 1)
    finally:
        await stop(task)
    assert len(threads) == 2 and all(t is not loop_thread for t in threads)  # headers, then the message
