"""Review fixes round 2, group C (R2-C2): ``allowed_senders: ['*']`` is refused in the
user's own INBOX without a plus-tag, where it would turn every DMARC-passing message into
an item and mark it read. It is fine with ``imap_plus_tag`` or a dedicated ``imap_folder``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from sendtox3.config import Options
from sendtox3.extract import email as em
from sendtox3.extract.email import accept_email, parse_email
from sendtox3.workers import imap as imapw

from test_fix_C_accept import build
from test_imap_worker import AR_PASS, FakeFolder, FakeProcessor, State, mail, start, stop, until
from test_imap_worker import iopts, server  # noqa: F401  (fixtures used below)

KEY = "imap:x3reader@example.test@imap.example.test:993/INBOX"


@pytest.fixture
def star(opts: Options) -> Options:
    """IMAP on, ``allowed_senders: ['*']``, INBOX, no plus-tag."""
    return replace(opts, imap_enabled=True, imap_host="imap.gmail.com", imap_user="x3reader@gmail.com",
                   imap_password="app-password-1234", imap_folder="INBOX", imap_plus_tag="", allowed_senders=["*"])


def stranger() -> em.ParsedEmail:
    return parse_email(build("Stranger <stranger@gmail.com>"))


# ------------------------------------------------------------------ accept_email


@pytest.mark.parametrize("folder", ["INBOX", "inbox", " Inbox ", ""], ids=["INBOX", "lower", "spaces", "empty"])
@pytest.mark.parametrize("senders", [["*"], ["me@example.org", "*"], [" * "]], ids=["star", "star-and-list", "padded"])
def test_star_in_an_untagged_inbox_accepts_nothing(star, folder, senders):
    o = replace(star, imap_folder=folder, allowed_senders=senders)
    assert em.has_sender_filter(o)  # set, but unusable
    assert em.watches_personal_inbox(o)
    assert em.sender_filter_problem(o) == em.WILDCARD_IN_INBOX
    for p in (stranger(), parse_email(build("Me <me@example.org>"))):
        assert not em.sender_allowed(p, o)
        ok, reason = accept_email(p, o)
        assert not ok and em.WILDCARD_IN_INBOX in reason


def test_the_refusal_says_what_to_change(star):
    msg = em.sender_filter_problem(star)
    assert "'*'" in msg and "INBOX" in msg
    assert "imap_plus_tag" in msg and "imap_folder" in msg and "allowed_senders" in msg


def test_star_with_a_plus_tag_accepts_any_sender_on_the_tag(star):
    o = replace(star, imap_plus_tag="k7q2")
    assert em.sender_filter_problem(o) == ""
    assert accept_email(parse_email(build("Stranger <stranger@gmail.com>", to="x3reader+k7q2@gmail.com")), o)[0]
    assert not accept_email(stranger(), o)[0]  # the tag still gates


@pytest.mark.parametrize("folder", ["Send to X3", "INBOX/X3", "INBOX.x3", "[Gmail]/X3"])
def test_star_in_a_dedicated_folder_accepts_any_sender(star, folder):
    o = replace(star, imap_folder=folder)
    assert not em.watches_personal_inbox(o) and em.sender_filter_problem(o) == ""
    ok, reason = accept_email(stranger(), o)
    assert ok, reason


def test_a_sender_list_in_an_untagged_inbox_is_unaffected(star):
    o = replace(star, allowed_senders=["me@example.org"])
    assert em.sender_filter_problem(o) == ""
    assert accept_email(parse_email(build("Me <me@example.org>")), o)[0]
    ok, reason = accept_email(stranger(), o)
    assert not ok and "is not in allowed_senders" in reason


def test_no_filter_at_all_still_reports_the_missing_filter(star):
    o = replace(star, allowed_senders=[])
    assert em.sender_filter_problem(o) == em.NO_SENDER_FILTER


# ------------------------------------------------------------------- the worker


@pytest.fixture
def star_state(iopts, store):  # noqa: F811
    opts = replace(iopts, imap_plus_tag="", imap_processed_folder="", allowed_senders=["*"])
    return State(opts=opts, store=store, processor=FakeProcessor())


def anyone(i: int) -> bytes:
    return mail(f"From anyone {i}", frm=f"Friend <f{i}@else.test>", to="x3reader@example.test",
                ar=AR_PASS.replace("example.org", "else.test"))


async def test_worker_refuses_star_in_an_untagged_inbox(star_state, server, store):  # noqa: F811
    uid = server.deliver(anyone(1))
    await asyncio.wait_for(imapw.run(star_state, connect=server.connect), 2)  # returns at once
    st = star_state.status["imap"]
    assert server.connects == 0 and server.flags(uid) == set()  # not read, not flagged
    assert st["state"] == "not_configured" and st["enabled"] is False  # no restart loop
    assert st["reason"] == st["last_error"] == st["warning"] == em.WILDCARD_IN_INBOX
    assert star_state.processor.batches == []
    assert store.kv_get(KEY + ":last_uid") is None  # nothing skipped over: read once fixed


async def test_worker_accepts_star_with_a_plus_tag(star_state, server):  # noqa: F811
    star_state.opts = replace(star_state.opts, imap_plus_tag="k7q2")
    tagged = server.deliver(mail("Tagged", frm="Friend <f1@else.test>", to="x3reader+k7q2@example.test",
                                 ar=AR_PASS.replace("example.org", "else.test")))
    untagged = server.deliver(anyone(2))
    task = start(star_state, server)
    try:
        await until(lambda: len(star_state.processor.batches) == 1, what="tagged mail processed")
        await until(lambda: star_state.status["imap"]["state"] == "idle")
    finally:
        await stop(task)
    assert [r.title for r in star_state.processor.requests] == ["Tagged"]
    assert "\\Seen" in server.flags(tagged) and server.flags(untagged) == set()


async def test_worker_accepts_star_in_a_dedicated_folder(star_state, server):  # noqa: F811
    server.folders["Send to X3"] = FakeFolder(uidvalidity=77)
    star_state.opts = replace(star_state.opts, imap_folder="Send to X3")
    uid = server.deliver(anyone(3), folder="Send to X3")
    task = start(star_state, server)
    try:
        await until(lambda: "\\Seen" in server.flags(uid, "Send to X3"), what="mail in the folder processed")
    finally:
        await stop(task)
    assert [r.title for r in star_state.processor.requests] == ["From anyone 3"]
    assert star_state.status["imap"]["state"] != "not_configured"
