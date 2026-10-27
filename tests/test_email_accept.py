"""extract/email.py: accept_email and Authentication-Results handling (§9 security filter)."""

from __future__ import annotations

from dataclasses import replace
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

import pytest

from sendtox3.config import Options
from sendtox3.extract.email import (
    MAX_EMAIL_BYTES,
    accept_email,
    addressed_to_tag,
    domains_aligned,
    organizational_domain,
    parse_auth_results,
    parse_email,
)

EMAILS = Path(__file__).parent / "fixtures" / "emails"

GMAIL_PASS = (
    "mx.google.com; dkim=pass header.i=@example.org header.s=s1 header.b=AbC; "
    "spf=pass (google.com: domain of qiong@example.org designates 192.0.2.1 as permitted sender) "
    "smtp.mailfrom=qiong@example.org; dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.org"
)


@pytest.fixture
def gopts(opts: Options) -> Options:
    """A Gmail setup with every check on."""
    return replace(
        opts,
        imap_enabled=True,
        imap_host="imap.gmail.com",
        imap_user="x3reader@gmail.com",
        imap_password="app-password-1234",
        imap_plus_tag="k7q2",
        allowed_senders=["qiong@example.org", "@family.example"],
        require_dmarc=True,
    )


def build(
    *,
    frm: str = "Qiong Example <qiong@example.org>",
    to: str = "x3reader+k7q2@gmail.com",
    ar: list[str] | None = None,
    body: str = "A note with enough words to be a real note for the e-reader queue.\n",
    extra: dict[str, str] | None = None,
) -> bytes:
    m = EmailMessage()
    for v in [GMAIL_PASS] if ar is None else ar:
        m["Authentication-Results"] = v
    m["From"] = frm
    if to:
        m["To"] = to
    m["Subject"] = "Hello"
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(body)
    return m.as_bytes(policy=SMTP)


def check(raw: bytes, o: Options) -> tuple[bool, str]:
    return accept_email(parse_email(raw), o)


# ------------------------------------------------------------------------- happy path


def test_accepts_fixture_from_allowed_sender(gopts):
    ok, reason = accept_email(parse_email((EMAILS / "gmail_url_only.eml").read_bytes()), gopts)
    assert ok, reason
    assert reason.startswith("accepted") and "qiong@example.org" in reason and "dmarc=pass" in reason


def test_accepts_everything_when_filters_are_off(opts):
    # '*' needs a dedicated folder (or a plus-tag): see test_fix2_C_wildcard.py
    o = replace(opts, require_dmarc=False, imap_plus_tag="", allowed_senders=["*"], imap_folder="Send to X3")
    ok, _ = check(build(ar=[], to="someone@example.com"), o)
    assert ok


# --------------------------------------------------------------------------- plus tag


def test_plus_tag_required(gopts):
    ok, reason = check(build(to="x3reader@gmail.com"), gopts)
    assert not ok and "+tag" in reason
    assert "k7q2" not in reason  # the secret is not written to logs


@pytest.mark.parametrize("tag", ["k7q2", "K7Q2", "+k7q2", " x3reader+k7q2@gmail.com "])
def test_plus_tag_config_forms(gopts, tag):
    assert check(build(to="X3Reader+K7Q2@Gmail.com"), replace(gopts, imap_plus_tag=tag))[0]


@pytest.mark.parametrize(
    "headers",
    [
        {"Delivered-To": "x3reader+k7q2@gmail.com"},
        {"X-Original-To": "x3reader+k7q2@gmail.com"},
        {"Cc": "Friend <friend@example.com>, x3reader+k7q2@gmail.com"},
    ],
)
def test_plus_tag_found_in_envelope_and_cc(gopts, headers):
    raw = build(to="friend@example.com", extra=headers)  # e.g. Bcc'd: only the envelope has it
    assert check(raw, gopts)[0]


def test_plus_tag_must_match_exactly(gopts):
    assert not check(build(to="x3reader+k7q2x@gmail.com"), gopts)[0]
    assert not check(build(to="x3reader+other@gmail.com"), gopts)[0]
    assert not check(build(to="+k7q2@gmail.com"), gopts)[0]  # no user part
    assert check(build(to="x3reader+k7q2+extra@gmail.com"), gopts)[0]


def test_addressed_to_tag_helper():
    p = parse_email(build(to="x3reader+k7q2@gmail.com"))
    assert addressed_to_tag(p, "k7q2") and not addressed_to_tag(p, "zz") and not addressed_to_tag(p, "")


# ----------------------------------------------------------------------- allowlist


@pytest.mark.parametrize(
    ("frm", "ok"),
    [
        ("Qiong Example <QIONG@Example.org>", True),
        ("Mum <mum@family.example>", True),
        ("Spoof <qiong@example.org.evil.test>", False),
        ("Sub <a@sub.family.example>", False),  # "@domain" is that exact domain
        ("Other <other@example.org>", False),
    ],
)
def test_allowed_senders(gopts, frm, ok):
    o = replace(gopts, require_dmarc=False)
    result, reason = check(build(frm=frm), o)
    assert result is ok, reason
    if not ok:
        assert "allowed_senders" in reason


def test_allowed_senders_bare_domain_and_string_option(gopts):
    o = replace(gopts, require_dmarc=False, allowed_senders="example.org, @family.example")  # type: ignore[arg-type]
    assert check(build(frm="any@example.org"), o)[0]
    assert not check(build(frm="any@example.com"), o)[0]


# --------------------------------------------------------------------- DMARC / DKIM


def test_forged_lower_authentication_results_are_ignored(gopts):
    p = parse_email((EMAILS / "forged_auth_results.eml").read_bytes())
    assert "dmarc=fail" in p.auth_results  # topmost is the real one
    assert not p.dmarc_pass and not p.dkim_aligned_pass
    ok, reason = accept_email(p, gopts)
    assert not ok and "DMARC failed" in reason


def test_lower_pass_cannot_rescue_missing_top_result(gopts):
    forged = "mx.google.com; dkim=pass header.i=@example.org; dmarc=pass header.from=example.org"
    real = "mx.google.com; spf=softfail smtp.mailfrom=qiong@example.org"
    ok, reason = check(build(ar=[real, forged]), gopts)
    assert not ok and "no DMARC pass" in reason


def test_gmail_requires_google_authserv_id(gopts):
    ok, reason = check(build(ar=["evil.example; dmarc=pass header.from=example.org"]), gopts)
    assert not ok and "mx.google.com" in reason
    # Fastmail stamps from mx<n>.messagingengine.com
    o = replace(gopts, imap_host="imap.fastmail.com")
    assert check(build(ar=["mx3.messagingengine.com; dmarc=pass header.from=example.org"]), o)[0]


def test_no_authentication_results(gopts):
    ok, reason = check(build(ar=[]), gopts)
    assert not ok and "no Authentication-Results" in reason
    assert check(build(ar=[]), replace(gopts, require_dmarc=False))[0]


@pytest.mark.parametrize(
    ("ar", "ok"),
    [
        ("mx.google.com; dkim=pass header.i=@example.org", True),
        ("mx.google.com; dkim=pass header.d=mail.example.org", True),  # relaxed: subdomain
        ("mx.google.com; dkim=pass header.d=example.org header.i=@example.org; dmarc=none", True),
        ("mx.google.com; dkim=pass header.i=@sendgrid.net; spf=pass", False),  # not aligned
        ("mx.google.com; dkim=fail header.i=@example.org", False),
        ("mx.google.com; dkim=pass header.i=@example.org; dmarc=fail header.from=example.org", False),
        ("mx.google.com; dmarc=pass header.from=other.example", False),  # pass for another domain
        ("mx.google.com; dmarc=pass", True),
        ("mx.google.com; dmarc=PASS (p=NONE; sp=NONE) header.from=Example.ORG", True),
        ("mx.google.com 1; none", False),
    ],
)
def test_dmarc_and_aligned_dkim(gopts, ar, ok):
    result, reason = check(build(ar=[ar]), gopts)
    assert result is ok, reason


def test_multiple_from_addresses_rejected(gopts):
    raw = build(frm="Qiong Example <qiong@example.org>, Evil <evil@evil.test>")
    ok, reason = check(raw, replace(gopts, allowed_senders=[]))
    assert not ok and "From addresses" in reason


def test_microsoft_style_results_without_authserv_id(gopts):
    o = replace(gopts, imap_host="outlook.office365.com")
    p = parse_email((EMAILS / "outlook_forward.eml").read_bytes())
    assert p.authserv_id == "" and p.dmarc_pass
    assert accept_email(p, replace(o, imap_plus_tag="k7q2"))[0]


# ----------------------------------------------------------------------- size / content


def test_size_cap(gopts):
    big = build(body="x " * 10) + b"\r\n" + b"y" * (MAX_EMAIL_BYTES + 10)
    p = parse_email(big)
    ok, reason = accept_email(p, gopts)
    assert not ok and "MB" in reason


def test_size_cap_on_headers_only_parse_uses_reported_size(gopts):
    p = parse_email(build(), headers_only=True)
    assert accept_email(p, gopts)[0]  # content check skipped for headers-only
    p.size = MAX_EMAIL_BYTES + 1  # RFC822.SIZE from the server
    assert not accept_email(p, gopts)[0]


def test_empty_message_rejected_with_reason(gopts):
    ok, reason = check(build(body="\n"), gopts)
    assert not ok and "no readable content" in reason


def test_password_never_in_reason(gopts):
    for raw in (build(ar=[]), build(to="nobody@example.com"), build(frm="x@evil.test")):
        _, reason = check(raw, gopts)
        assert gopts.imap_password not in reason


# ------------------------------------------------------------------------- parsers


def test_parse_auth_results_comments_quotes_and_versions():
    ar = parse_auth_results(
        'mx.example.org 1; dkim/1=pass (good; signature) header.d=example.org header.s="sel;1"; '
        "spf=pass (sender (nested) ok) smtp.mailfrom=a@example.org; dmarc=pass header.from=example.org"
    )
    assert ar.authserv_id == "mx.example.org"
    assert [(r.method, r.result) for r in ar.results] == [("dkim", "pass"), ("spf", "pass"), ("dmarc", "pass")]
    assert ar.results[0].props == {"header.d": "example.org", "header.s": "sel;1"}
    assert ar.results[2].props["header.from"] == "example.org"


def test_parse_auth_results_none_and_garbage():
    assert parse_auth_results("mx.example.org; none").results == []
    assert parse_auth_results("").authserv_id == ""
    assert parse_auth_results(";;;").results == []


@pytest.mark.parametrize(
    ("domain", "org"),
    [
        ("example.org", "example.org"),
        ("mail.news.example.org", "example.org"),
        ("mail.example.co.uk", "example.co.uk"),
        ("example.co.uk", "example.co.uk"),
        ("a.b.example.com.au", "example.com.au"),
        ("x.example.co.jp", "example.co.jp"),
        ("bob.github.io", "bob.github.io"),
        ("www.bbc.co.uk", "bbc.co.uk"),
        ("Mail.Example.ORG.", "example.org"),
    ],
)
def test_organizational_domain(domain, org):
    assert organizational_domain(domain) == org


@pytest.mark.parametrize(
    ("a", "b", "ok"),
    [
        ("example.org", "example.org", True),
        ("mail.example.org", "example.org", True),
        ("example.org", "news.example.org", True),
        ("news.example.org", "mail.example.org", True),  # same organizational domain
        ("evil.co.uk", "bank.co.uk", False),
        ("alice.github.io", "bob.github.io", False),
        ("example.org.evil.test", "example.org", False),
        ("", "example.org", False),
    ],
)
def test_domains_aligned(a, b, ok):
    assert domains_aligned(a, b) is ok
