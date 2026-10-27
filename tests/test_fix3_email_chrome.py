"""Round 3, item 1: extract/email.py's own newsletter-chrome filter (``_strip_html_chrome``
for HTML bodies, ``_strip_chrome_lines`` for plain ones) uses html.py's strict rules
and only near either end of the body.

Before, a loose ``_CHROME_STRONG_RE`` ("update ... subscription", "you ... received
this", "view ... in browser" anywhere in a line or text node) ran on every block of
every email, so parse_email deleted ordinary sentences before html.py saw them: "Hi
Bob, / Have you received this invoice yet? ... / Please update your subscription by
Friday ... / Thanks, Ann" kept only the greeting and the sign-off.

The end-to-end tests go parse_email -> the real Processor -> the stored TXT.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

import pytest

from sendtox3.extract import email as em
from sendtox3.extract.email import parse_email
from sendtox3.extract.html import html_to_document, is_mail_chrome, is_mail_chrome_link
from sendtox3.ingest import Processor

from test_fix2_B_html import BODY, FOOTERS, ORDINARY

FIXTURES = Path(__file__).parent / "fixtures" / "emails"

INVOICE = [
    "Hi Bob,",
    "Have you received this invoice yet? It is due Friday.",
    "Please update your subscription by Friday so the renewal goes through.",
    "Thanks, Ann",
]


def _mail(body: str, *, html: bool, subject: str = "Hello") -> bytes:
    m = EmailMessage()
    m["From"] = "Ann <ann@example.com>"
    m["To"] = "me+x3@example.com"
    m["Subject"] = subject
    m.set_content(body, subtype="html" if html else "plain")
    return m.as_bytes(policy=SMTP)


def _body(paras: list[str], html: bool) -> str:
    return "".join(f"<p>{p}</p>" for p in paras) if html else "\n\n".join(paras)


def _parsed_text(raw: bytes) -> str:
    """What the email's requests carry, as html.py would turn an HTML body into text."""
    out = []
    for r in parse_email(raw).requests:
        out.append(html_to_document(r.html, whole=True).plain_text() if r.kind == "html" else r.text)
    return "\n\n".join(out)


async def _stored_txt(store, opts, raw: bytes) -> str:
    reqs = parse_email(raw).requests
    assert reqs, "the email gave no request"
    proc = Processor(store, opts)
    out = []
    for req in reqs:
        item = await proc.process(req)
        assert item.status == "ready", item.error
        out.append(store.file_bytes(item.id, "txt").decode("utf-8"))
    return "\n".join(out)


def _flat(s: str) -> str:
    return " ".join(s.split())


# ------------------------------------------------------------------ the verifier's emails


@pytest.mark.parametrize("html", [False, True], ids=["plain", "html"])
async def test_verifiers_invoice_email_reaches_the_txt_whole(store, opts, html):
    txt = _flat(await _stored_txt(store, opts, _mail(_body(INVOICE, html), html=html, subject="Invoice")))
    for sentence in INVOICE:
        assert sentence in txt, sentence


@pytest.mark.parametrize("html", [False, True], ids=["plain", "html"])
def test_parse_email_keeps_the_invoice_email(html):
    raw = _mail(_body(INVOICE, html), html=html)
    [req] = parse_email(raw).requests
    text = req.text if req.kind == "text" else em._html_visible_text(req.html)
    assert _flat(text) == _flat(" ".join(INVOICE))


@pytest.mark.parametrize("html", [False, True], ids=["plain", "html"])
@pytest.mark.parametrize("sentence", ORDINARY)
async def test_ordinary_sentence_in_a_short_email_reaches_the_txt(store, opts, sentence, html):
    raw = _mail(_body(["Hi Bob,", sentence, "Thanks, Ann"], html), html=html)
    assert sentence in _flat(await _stored_txt(store, opts, raw))


@pytest.mark.parametrize("html", [False, True], ids=["plain", "html"])
@pytest.mark.parametrize("where", ["alone", "first", "last", "both ends of a long email"])
def test_ordinary_sentences_survive_at_every_edge(html, where):
    for sentence in ORDINARY:
        paras = {
            "alone": [sentence],
            "first": [sentence] + BODY[:2],
            "last": BODY[:2] + [sentence],
            "both ends of a long email": [sentence] + BODY + [sentence],
        }[where]
        assert sentence in _flat(_parsed_text(_mail(_body(paras, html), html=html))), sentence


def test_gmail_style_single_block_email_keeps_every_line():
    lines = INVOICE[:3] + ["View this email in your browser to see the chart.", "Thanks, Ann"]
    html = '<div dir="ltr">' + "<br><br>".join(lines) + "</div>"
    text = _parsed_text(_mail(html, html=True))
    for line in lines:
        assert line in text, line


def test_footer_words_as_links_inside_sentences_are_kept():
    html = (
        "<p>Hi Bob,</p>"
        '<p>Here is how to <a href="https://help.example/subs">manage your subscriptions</a> in the app settings.</p>'
        '<p>Can you <a href="https://club.example/u">unsubscribe</a> me from the club list? It sends three a day.</p>'
        "<p>Thanks, Ann</p>"
    )
    text = _parsed_text(_mail(html, html=True))
    assert "Here is how to manage your subscriptions in the app settings." in text
    assert "Can you unsubscribe me from the club list? It sends three a day." in text


# -------------------------------------------------------------------- real footers go


@pytest.mark.parametrize("html", [False, True], ids=["plain", "html"])
@pytest.mark.parametrize("footer", FOOTERS)
def test_newsletter_footer_is_removed_from_short_and_long_emails(html, footer):
    for paras in (BODY[:2] + [footer], BODY + [footer], [footer] + BODY[:2], [footer] + BODY):
        text = _parsed_text(_mail(_body(paras, html), html=html))
        assert footer not in text, (footer, len(paras))
        assert paras[1 if paras[0] == footer else 0] in text


def test_footer_in_the_middle_of_a_long_email_is_kept():
    paras = BODY[:8] + ["Unsubscribe"] + BODY[8:]
    for html in (False, True):
        assert "Unsubscribe" in _parsed_text(_mail(_body(paras, html), html=html))


def test_plain_text_footer_wrapped_over_lines_goes_whole():
    body = "\n\n".join(BODY[:3]) + (
        "\n\nThanks for reading!\nYou are receiving this email because you subscribed to\n"
        "The Quiet Letter at example.net.\nUnsubscribe: https://example.net/u/1\n"
    )
    [req] = parse_email(_mail(body, html=False)).requests
    assert "Thanks for reading!" in req.text
    for gone in ("receiving this email", "The Quiet Letter at example.net", "Unsubscribe"):
        assert gone not in req.text, gone


def test_plain_text_footer_links_in_brackets_and_angles():
    body = (
        "View this email in your browser <https://example.net/v/1>\n\n" + "\n\n".join(BODY[:3]) +
        "\n\nUnsubscribe [https://example.net/u/1] | Manage preferences (https://example.net/p/1)\n"
    )
    [req] = parse_email(_mail(body, html=False)).requests
    assert req.text == "\n\n".join(BODY[:3])


def test_mailchimp_classic_footer_lines_in_one_cell():
    """Mailchimp's classic template writes the whole footer into one cell with <br>s:
    its chrome lines go, the copyright and postal address stay (not chrome)."""
    article = "".join(f"<p>{p}</p>" for p in BODY[:4])
    html = (
        '<table><tr><td><a href="https://us1.campaign-archive.com/?u=1">View this email in your browser</a></td></tr>'
        f"<tr><td>{article}</td></tr>"
        '<tr><td class="mcnTextContent"><em>Copyright © 2026 The Quiet Letter, All rights reserved.</em><br>'
        "You are receiving this email because you opted in via our website.<br><br>"
        "<strong>Our mailing address is:</strong><br>"
        '<div class="vcard"><span class="org fn">The Quiet Letter</span>'
        '<div class="adr"><div class="street-address">1 Example Street</div></div><br>'
        '<a href="https://example.net/vcard" class="hcard-download">Add us to your address book</a></div><br><br>'
        "Want to change how you receive these emails?<br>"
        'You can <a href="https://example.net/p">update your preferences</a> or '
        '<a href="https://example.net/u">unsubscribe from this list</a>.<br><br></td></tr></table>'
    )
    text = _parsed_text(_mail(html, html=True))
    assert all(p in text for p in BODY[:4])
    for gone in ("View this email", "receiving this email", "address book", "Want to change", "unsubscribe"):
        assert gone not in text, gone
    assert "Copyright © 2026 The Quiet Letter" in text and "1 Example Street" in text


@pytest.mark.parametrize(
    "footer",
    [
        'You received this because you subscribed. <a href="https://example.net/u">Unsubscribe</a>',
        'This email was sent to bob@example.com. <a href="https://example.net/f">Forward to a friend</a> | '
        '<a href="https://example.net/u">Unsubscribe</a>',
        "You're receiving this email because you subscribed to Quiet Notes. "
        '<a href="https://buttondown.example/u">Unsubscribe</a>',
        'Having trouble viewing this email? <a href="https://example.net/v">View it in your browser</a>',
        '© 2026 Author · <a href="https://example.net/u">Unsubscribe</a>',
    ],
)
def test_short_footer_cell_around_a_footer_link_goes(footer):
    article = "".join(f"<p>{p}</p>" for p in BODY[:3])
    html = f"<table><tr><td>{article}</td></tr><tr><td style='font-size:10px'>{footer}</td></tr></table>"
    text = _parsed_text(_mail(html, html=True))
    assert all(p in text for p in BODY[:3])
    for word in ("Unsubscribe", "receiv", "sent to", "browser", "Author"):
        assert word not in text, word


async def test_newsletter_fixture_reaches_the_txt_without_chrome(store, opts):
    txt = await _stored_txt(store, opts, (FIXTURES / "newsletter_related.eml").read_bytes())
    assert "Public libraries are one of the few places" in txt
    assert "A library is a quiet argument" in txt
    for junk in ("View this email", "Unsubscribe", "Manage preferences", "You are receiving"):
        assert junk not in txt, junk


async def test_forwarded_attachment_newsletter_reaches_the_txt_without_chrome(store, opts):
    txt = await _stored_txt(store, opts, (FIXTURES / "forward_as_attachment.eml").read_bytes())
    assert "This week we look at three essays" in txt and "Paragraphs as places" in txt
    for junk in ("View in browser", "Unsubscribe", "You received this"):
        assert junk not in txt, junk


async def test_gmail_forwarded_newsletter_loses_its_chrome_not_its_text(store, opts):
    """A newsletter forwarded from Gmail (the fixture's forward wrapper around a
    Mailchimp-style issue)."""
    inner = (
        '<table><tr><td align="center"><a href="https://example.net/v/1">View this email in your browser</a></td></tr>'
        "<tr><td><h1>The Quiet Letter</h1>"
        "<p>Have you received this issue on paper yet? It went out on Friday.</p>"
        "<p>Public libraries are one of the few places where you can stay as long as you like.</p>"
        "<p>Please update your subscription by Friday so the renewal goes through.</p></td></tr>"
        '<tr><td style="font-size:11px">You are receiving this email because you signed up at example.net.<br>'
        '<a href="https://example.net/u/1">Unsubscribe</a> | <a href="https://example.net/p/1">Manage preferences</a>'
        "</td></tr></table>"
    )
    html = (
        '<div dir="ltr">Worth reading.<br><div class="gmail_quote"><div dir="ltr" class="gmail_attr">'
        "---------- Forwarded message ---------<br>From: The Quiet Letter &lt;letter@example.net&gt;<br>"
        "Date: Fri, Oct 23, 2026 at 5:12 PM<br>Subject: Libraries<br>To: Ann &lt;ann@example.com&gt;<br></div>"
        f"<br><br>{inner}</div></div>"
    )
    raw = _mail(html, html=True, subject="Fwd: Libraries")
    txt = _flat(await _stored_txt(store, opts, raw))
    for kept in ("Have you received this issue on paper yet? It went out on Friday.",
                 "Public libraries are one of the few places",
                 "Please update your subscription by Friday so the renewal goes through."):
        assert kept in txt, kept
    for junk in ("View this email", "Unsubscribe", "Manage preferences", "You are receiving", "Forwarded message"):
        assert junk not in txt, junk


async def test_gmail_forward_fixture_is_unchanged(store, opts):
    txt = _flat(await _stored_txt(store, opts, (FIXTURES / "gmail_forward_inline.eml").read_bytes()))
    assert "Here are my notes from Thursday’s session at the café." in txt
    assert "Short paragraphs are kinder." in txt


# --------------------------------------------------------------------- the shared rules


@pytest.mark.parametrize(
    "text",
    [
        "Unsubscribe [https://example.net/u/1] | Manage preferences [https://example.net/p/1]",
        "View this email in your browser: https://example.net/v",
        "why did I get this? unsubscribe from this list update subscription preferences",
        "If you no longer wish to receive these emails, you can unsubscribe here.",
        "To stop receiving these emails, unsubscribe: https://example.net/u",
        "This email was sent to bob@example.com",
        "Forward to a friend | Unsubscribe",
        "Read in app",
    ],
)
def test_strict_chrome_forms(text):
    assert is_mail_chrome(text)


@pytest.mark.parametrize("text", ORDINARY + INVOICE + [
    "This message was sent to the wrong person, sorry!",
    "Feel free to forward this to a friend who might like it.",
    "If you no longer want the club emails, tell Dave.",
])
def test_ordinary_sentences_are_not_chrome(text):
    assert not is_mail_chrome(text, max_words=60, max_chars=300)


def test_footer_link_texts():
    links = ["Unsubscribe", "View in browser", "Manage preferences", "Forward to a friend"]
    assert all(map(is_mail_chrome_link, links))
    assert not any(map(is_mail_chrome_link, ["the full story", "unsubscribe me", "Read more"]))
