"""Review fixes, group C: the email acceptance gate (extract/email.py accept_email)."""

from __future__ import annotations

from dataclasses import replace
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from sendtox3.config import Options
from sendtox3.extract import email as em
from sendtox3.extract.email import accept_email, parse_email

GMAIL_PASS = "mx.google.com; dkim=pass header.i=@{d}; dmarc=pass header.from={d}"


def build(frm: str, *, to: str = "x3reader@gmail.com", ar: str | None = None) -> bytes:
    m = EmailMessage()
    domain = frm.rpartition("@")[2].rstrip(">")
    m["Authentication-Results"] = ar if ar is not None else GMAIL_PASS.format(d=domain)
    m["From"] = frm
    m["To"] = to
    m["Subject"] = "Hello"
    m.set_content("A note with enough words to be a real note for the e-reader queue.\n")
    return m.as_bytes(policy=SMTP)


@pytest.fixture
def open_opts(opts: Options) -> Options:
    """IMAP turned on with only user and password set (every filter left at its default)."""
    return replace(opts, imap_enabled=True, imap_host="imap.gmail.com", imap_user="x3reader@gmail.com",
                   imap_password="app-password-1234")


# ---------------------------------------------------------- finding 16: fail closed


def test_default_settings_do_not_accept_any_dmarc_passing_stranger(open_opts):
    assert open_opts.allowed_senders == [] and open_opts.imap_plus_tag == ""
    assert not em.has_sender_filter(open_opts)
    for frm in ("Stranger <stranger@gmail.com>", "Me <x3reader@gmail.com>"):
        ok, reason = accept_email(parse_email(build(frm)), open_opts)
        assert not ok
        assert "allowed_senders and imap_plus_tag are both empty" in reason and "'*'" in reason


def test_star_opts_in_to_any_sender(open_opts):
    # In a dedicated folder: in an untagged INBOX '*' is refused (test_fix2_C_wildcard.py).
    o = replace(open_opts, allowed_senders=["*"], imap_folder="Send to X3")
    assert em.has_sender_filter(o)
    assert accept_email(parse_email(build("Stranger <stranger@gmail.com>")), o)[0]


def test_secret_plus_tag_alone_still_gates_without_a_sender_list(open_opts):
    o = replace(open_opts, imap_plus_tag="k7q2")
    assert em.has_sender_filter(o)
    tagged = parse_email(build("Friend <friend@gmail.com>", to="x3reader+k7q2@gmail.com"))
    assert em.sender_allowed(tagged, o) and accept_email(tagged, o)[0]
    assert not accept_email(parse_email(build("Friend <friend@gmail.com>")), o)[0]


def test_sender_list_alone_still_gates(open_opts):
    o = replace(open_opts, allowed_senders=["me@example.org"])
    assert accept_email(parse_email(build("Me <me@example.org>")), o)[0]
    ok, reason = accept_email(parse_email(build("Stranger <stranger@gmail.com>")), o)
    assert not ok and "is not in allowed_senders" in reason


# ------------------------------------------- finding 42: whose Authentication-Results


@pytest.mark.parametrize(
    ("host", "good"),
    [
        ("outlook.office365.com", "spf=pass smtp.mailfrom=allowed.com; dkim=pass header.d=allowed.com;"
                                  "dmarc=pass action=none header.from=allowed.com;compauth=pass reason=100"),
        ("imap.fastmail.com", "mx3.messagingengine.com; dkim=pass header.d=allowed.com; "
                              "dmarc=pass header.from=allowed.com"),
        ("imap.mail.yahoo.com", "atlas-production.v2.mail.yahoo.com; dkim=pass header.i=@allowed.com; "
                                "dmarc=pass(p=REJECT) header.from=allowed.com"),
    ],
)
def test_known_providers_reject_a_topmost_header_from_another_server(opts, host, good):
    o = replace(opts, imap_host=host, imap_user="me@allowed.com", allowed_senders=["me@allowed.com"],
                require_dmarc=True)
    forged = "x; dmarc=pass header.from=allowed.com"
    ok, reason = accept_email(parse_email(build("Me <me@allowed.com>", ar=forged)), o)
    assert not ok and "topmost Authentication-Results is from 'x'" in reason
    ok, reason = accept_email(parse_email(build("Me <me@allowed.com>", ar=good)), o)
    assert ok, reason


def test_microsoft_rejects_a_header_that_carries_an_authserv_id(opts):
    o = replace(opts, imap_host="outlook.office365.com", allowed_senders=["*"], imap_folder="X3", require_dmarc=True)
    ok, reason = accept_email(parse_email(build("Me <me@allowed.com>", ar="mx.google.com; dmarc=pass")), o)
    assert not ok and "without authserv-id" in reason
