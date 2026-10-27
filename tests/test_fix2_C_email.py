"""Review fixes round 2, group C: extract/email.py.

R2-C1: the comment kept next to an emailed link loses the brackets and "see"/"via" that
only framed the link ("Read <https://...> now" was kept as "Read < > now").
R2-C4: the iOS Shortcut's email route ("[x3] <title>" subject, body "<link>\\n\\n<text>"
whenever the share has a link, "<text>" alone otherwise) gives the request the HTTP
route would send.

The Processor tests use the real Processor with only the page fetch faked.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.policy import SMTP

import pytest

from sendtox3 import ingest
from sendtox3.extract import email as em
from sendtox3.extract.email import parse_email
from sendtox3.ingest import Processor
from sendtox3.models import IngestRequest
from sendtox3.store import Store
from sendtox3.textmodel import Document, Paragraph

ARTICLE_WORDS = 200
PAGE = "https://news.example.com/2026/10/big-story"


def mail(subject: str, plain: str) -> bytes:
    m = EmailMessage()
    m["From"] = "Me <me@example.com>"
    m["To"] = "me+x3@gmail.com"
    m["Subject"] = subject
    m.set_content(plain)
    return m.as_bytes(policy=SMTP)


def shortcut_mail(subject: str, body: str) -> bytes:
    """What the Shortcut's Send Email action sends (plain text, no compose sheet)."""
    return (
        f"From: Me <me@example.com>\r\nTo: me+x3@gmail.com\r\nSubject: {subject}\r\n"
        "Message-ID: <shortcut@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + body
    ).encode()


@pytest.fixture
def pages(monkeypatch):
    """url -> page title; fetching any other URL fails the test."""
    titles: dict[str, str] = {}

    async def fake_url_to_document(url, *, opts, title_hint=""):
        assert url in titles, f"unexpected fetch of {url}"
        return Document(title=titles[url], blocks=[Paragraph(" ".join(["word"] * ARTICLE_WORDS))], source_url=url)

    monkeypatch.setattr(ingest, "url_to_document", fake_url_to_document)
    return titles


@pytest.fixture
def proc(store, opts):
    return Processor(store, opts)


def txt(store: Store, item) -> str:
    return store.file_bytes(item.id, "txt").decode("utf-8")


# ================================================================ R2-C1: link brackets


@pytest.mark.parametrize(
    ("line", "comment"),
    [
        ("Read <https://example.com/a/b> now", "Read now"),
        ("Nice one (see https://example.com/x)", "Nice one"),
        ("Nice one (see: https://example.com/x)", "Nice one"),
        ("Read this (via https://a.example/x)", "Read this"),
        ("Look [https://example.com/x] here", "Look here"),
        ("Great read <URL:https://example.com/x>", "Great read"),
        ("Great piece (https://example.com/x).", "Great piece."),
        ("Wiki (https://en.wikipedia.org/wiki/Foo_(bar)) rocks", "Wiki rocks"),
        ("Worth it (source: https://a.example/x, https://b.example/y)", "Worth it"),
        ("Nice one, see https://example.com/x", "Nice one"),
        ("Great article via https://example.com/x.", "Great article"),
        ("Great article - https://example.com/x", "Great article"),
        ("Note to self: https://example.com/x", "Note to self"),
        ("Read this! https://example.com/x!", "Read this!"),
        ("[the paper](https://example.com/p) is worth it", "the paper is worth it"),
        # The sender's own words inside the brackets stay; only the link goes.
        ("My post (my favourite, https://example.com/x) is up", "My post (my favourite) is up"),
        ("Wikipedia https://en.wikipedia.org/wiki/Foo_(bar) is good", "Wikipedia is good"),
        # Brackets that are not around a link are the sender's text.
        ("2 < 3 (really) and https://example.com/x [sic]", "2 < 3 (really) and [sic]"),
        ("(https://example.com/x)", ""),
        ("see https://example.com/x", ""),
    ],
)
def test_comment_loses_the_brackets_and_lead_words_that_framed_the_link(line, comment):
    assert em._link_comment(line, [])[0] == comment


def test_brackets_no_longer_count_as_comment_words():
    assert em._link_comment("Nice one (see https://example.com/x)", []) == ("Nice one", 2)
    assert em._link_comment("<https://example.com/x>", []) == ("", 0)  # a bare link: no comment at all


@pytest.mark.parametrize(
    ("body", "comment"),
    [
        ("My sister wrote this <https://example.com/a> last week", "My sister wrote this last week"),
        ("Lovely piece from my old teacher (see https://example.com/a)", "Lovely piece from my old teacher"),
        ("My old teacher wrote it [https://example.com/a]", "My old teacher wrote it"),
    ],
)
def test_emailed_link_comment_is_clean(body, comment):
    [r] = parse_email(mail("A link", body + "\n")).requests
    assert (r.kind, r.text) == ("auto", f"{comment}\n\nhttps://example.com/a")


async def test_stored_comment_has_no_empty_brackets(proc, store, pages):
    raw = mail("A link", "My sister wrote this <https://example.com/a> last week\n")
    pages["https://example.com/a"] = "Night trains of Europe"
    [item] = await proc.process_many(parse_email(raw).requests)
    assert item.kind == "url" and item.word_count >= ARTICLE_WORDS
    body = txt(store, item)
    assert "My sister wrote this last week" in body
    assert "< >" not in body and "<>" not in body


async def test_stored_comment_has_no_dangling_see(proc, store, pages):
    raw = mail("A link", "Lovely piece from my old teacher (see https://example.com/a)\n")
    pages["https://example.com/a"] = "Night trains of Europe"
    [item] = await proc.process_many(parse_email(raw).requests)
    body = txt(store, item)
    assert "Lovely piece from my old teacher" in body and "(see" not in body


# ================================================== R2-C4: the Shortcut's email route

SELECTION = "The council voted on Tuesday to close the old railway line after forty years of service."


@pytest.mark.parametrize(
    ("subject", "body", "title"),
    [
        ("[x3] Big Story Headline", f"{PAGE}\r\n\r\n", "Big Story Headline"),  # a Safari page, nothing selected
        ("[x3] ", f"{PAGE}\r\n\r\n", ""),  # a link shared on its own
        ("[x3] ", f"{PAGE}\r\n", ""),  # the blank line trimmed by the mail client
        ("[x3] ", f"{PAGE}\r\n\r\n{PAGE}\r\n", ""),  # the link as the text, too
        ("[x3] ", f"<{PAGE}>\r\n\r\n", ""),
    ],
    ids=["safari-page", "link", "no-blank-line", "link-twice", "angle-brackets"],
)
def test_shortcut_link_only_is_a_url_item(subject, body, title):
    [r] = parse_email(shortcut_mail(subject, body)).requests
    assert (r.kind, r.url, r.text, r.title) == ("url", PAGE, "", title)


@pytest.mark.parametrize(
    "body",
    ["http://example.com\r\n\r\nexample.com\r\n", "http://example.com/\r\n\r\nwww.example.com\r\n",
     "https://Example.com/a\r\n\r\nhttps://example.com/a/\r\n"],
    ids=["bare-domain", "www-host", "other-spelling"],
)
def test_shortcut_link_spelled_again_in_the_text_is_still_link_only(body):
    # iOS finds "example.com" in shared text as the link "http://example.com".
    [r] = parse_email(shortcut_mail("[x3] ", body)).requests
    assert r.kind == "url" and r.text == ""


def test_shortcut_link_and_selection_is_the_request_the_http_route_sends():
    [r] = parse_email(shortcut_mail("[x3] Big Story Headline", f"{PAGE}\r\n\r\n{SELECTION}\r\n")).requests
    # The Shortcut's HTTP POST: {"url": <page>, "text": <selection>, "title": <page title>}.
    http = IngestRequest(kind="auto", source="ios-shortcut", url=PAGE, text=SELECTION, title="Big Story Headline")
    assert (r.kind, r.url, r.text, r.title) == (http.kind, http.url, http.text, http.title)
    assert r.source == "email" and r.sender == "Me <me@example.com>"


async def test_shortcut_link_and_selection_is_a_note_citing_the_page(proc, store, pages):
    raw = shortcut_mail("[x3] Big Story Headline", f"{PAGE}\r\n\r\n{SELECTION}\r\n")
    [item] = await proc.process_many(parse_email(raw).requests)
    assert item.kind in ("email", "text") and item.source_url == PAGE
    assert "old railway line" in txt(store, item)


@pytest.mark.parametrize(
    "body",
    [
        "Remember to buy oat milk and call the plumber about the leak on Monday\r\n",
        "Call jane@acme.org about the plumber on Monday\r\n",  # an email address is not a link
    ],
    ids=["text", "email-address"],
)
def test_shortcut_text_without_a_link_is_a_text_note(body):
    [r] = parse_email(shortcut_mail("[x3] ", body)).requests
    assert (r.kind, r.url, r.title) == ("text", "", "")
    assert r.text == body.strip()


async def test_shortcut_text_without_a_link_is_stored_as_a_note(proc, store, pages):
    raw = shortcut_mail("[x3] ", "Remember to buy oat milk and call the plumber about the leak on Monday\r\n")
    [item] = await proc.process_many(parse_email(raw).requests)
    assert item.kind in ("email", "text") and item.source_url == ""
    assert "oat milk" in txt(store, item)


# Shared text (not a Safari page) that contains a link: the Shortcut emails the first link
# and then the whole text, but POSTs the text alone (url empty). The Processor must end up
# with the same item(s) either way.
LINK = "https://example.com/trains"
OTHER = "https://example.org/boats"
LONG = ("We took the night train from Vienna to Venice two summers ago and I still think about it, "
        f"there is a good write-up at {LINK} with photos of the sleeper cabins and the dining car, and "
        "the timetable has changed since then so check before booking anything for the spring.")
TEXT_SHARES = {
    "comment-and-link": f"My brother sent me this and I think you will like it {LINK}",
    "long-note-with-a-link": LONG,
    "two-links-and-a-comment": f"Both worth reading {LINK} {OTHER}",
}


@pytest.mark.parametrize("text", TEXT_SHARES.values(), ids=TEXT_SHARES.keys())
def test_shortcut_text_with_a_link_keeps_the_text_and_cites_its_first_link(text):
    [r] = parse_email(shortcut_mail("[x3] ", f"{LINK}\r\n\r\n{text}\r\n")).requests
    assert (r.kind, r.url, r.text, r.title) == ("auto", LINK, text, "")


@pytest.mark.parametrize("text", TEXT_SHARES.values(), ids=TEXT_SHARES.keys())
async def test_shortcut_text_with_a_link_gives_the_same_items_as_over_http(opts, store, tmp_path, pages, text):
    pages[LINK] = "Night trains of Europe"
    pages[OTHER] = "Boats of the Adriatic"
    by_email = await Processor(store, opts).process_many(
        parse_email(shortcut_mail("[x3] ", f"{LINK}\r\n\r\n{text}\r\n")).requests
    )
    http_store = Store(tmp_path / "http")
    try:
        by_http = await Processor(http_store, opts).process_many(
            [IngestRequest(kind="auto", source="ios-shortcut", url="", text=text, title="")]
        )

        def shape(items, st):
            return [(i.kind if i.kind == "url" else "note", i.source_url, i.word_count,
                     "Comment:" in txt(st, i)) for i in items]

        assert shape(by_email, store) == shape(by_http, http_store)
    finally:
        http_store.close()
    assert by_email  # and something was made
