"""Review fixes, group C: how email bodies become requests (extract/email.py).

The Processor tests use the real Processor with only the page fetch faked, so they show
what the user gets on the X3, not just which request the email turned into.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.policy import SMTP

import pytest

from sendtox3 import ingest
from sendtox3.extract.email import parse_email
from sendtox3.ingest import Processor
from sendtox3.textmodel import Document, Paragraph

ARTICLE_WORDS = 200
PAGE = "https://news.example.com/2026/10/big-story"


def shortcut_mail(subject: str, body: str) -> bytes:
    """What the iOS Shortcut sends: subject ``[x3] <title>``, body ``<url>\\n\\n<text>``."""
    return (
        f"From: Me <me@example.com>\r\nTo: me+x3@gmail.com\r\nSubject: {subject}\r\n"
        "Message-ID: <shortcut@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + body
    ).encode()


def mail(subject: str, plain: str | None = None, html: str | None = None) -> bytes:
    m = EmailMessage()
    m["From"] = "Me <me@example.com>"
    m["To"] = "me+x3@gmail.com"
    m["Subject"] = subject
    if plain is not None:
        m.set_content(plain)
        if html is not None:
            m.add_alternative(html, subtype="html")
    else:
        m.set_content(html or "", subtype="html")
    return m.as_bytes(policy=SMTP)


@pytest.fixture
def pages(monkeypatch):
    """url -> page title; fetching any other URL fails the test."""
    titles: dict[str, str] = {}

    async def fake_url_to_document(url, *, opts, title_hint=""):
        assert url in titles, f"unexpected fetch of {url}"
        body = " ".join(["word"] * ARTICLE_WORDS)
        return Document(title=titles[url], blocks=[Paragraph(body)], source_url=url)

    monkeypatch.setattr(ingest, "url_to_document", fake_url_to_document)
    return titles


@pytest.fixture
def proc(store, opts):
    return Processor(store, opts)


# ------------------------------------------------------------- finding 2: title + link


@pytest.mark.parametrize(
    "selection",
    [f"Big Story Headline\r\n{PAGE}", f"Big Story Headline {PAGE}"],
    ids=["title-line-then-link", "title-and-link-one-line"],
)
async def test_shortcut_title_and_link_share_by_email_is_the_fetched_article(proc, pages, selection):
    # Safari shares "Title\n<url>" as the text; the Shortcut emails "<url>\n\n<that text>".
    raw = shortcut_mail("[x3] ", f"{PAGE}\r\n\r\n{selection}\r\n")
    [r] = parse_email(raw).requests
    assert (r.kind, r.url) == ("auto", PAGE)  # the same url + text the Shortcut POSTs over HTTP
    assert "Big Story Headline" in r.text
    pages[PAGE] = "Big Story Headline - Example News"
    [item] = await proc.process_many([r])
    assert item.kind == "url" and item.status == "ready"
    assert item.word_count == ARTICLE_WORDS  # the article, not a 3-word note
    assert item.source_url == PAGE


async def test_shortcut_page_without_text_is_still_a_plain_link(proc, pages):
    [r] = parse_email(shortcut_mail("[x3] Big Story Headline", f"{PAGE}\r\n")).requests
    assert (r.kind, r.url, r.title) == ("url", PAGE, "Big Story Headline")


# ------------------------------------------------------- finding 30: selection with a link


async def test_shortcut_selection_containing_another_link_is_one_note_with_the_page_as_source(proc, store, pages):
    page = "https://page.example.com/p"
    raw = shortcut_mail("[x3] Foo", f"{page}\r\n\r\nSee https://other.example.org/x for the dataset\r\n")
    [r] = parse_email(raw).requests  # not two untitled link fetches
    assert (r.kind, r.url, r.title) == ("auto", page, "Foo")
    assert r.text == "See https://other.example.org/x for the dataset"
    pages[page] = "Some Page"
    [item] = await proc.process_many([r])
    assert item.source_url == page and item.kind in ("email", "text")
    assert "for the dataset" in store.file_bytes(item.id, "txt").decode("utf-8")


# ------------------------------------------------- finding 9: rich link with a long title

LONG_TITLE = "The 10 best things to do in Paris this summer, according to the locals who live there"  # 16 words
CARD = (
    '<a href="https://example.com/a" class="lp-rich-link"><table><tr><td><div class="lp-rich-link-captionBar">'
    f'<div><a href="https://example.com/a"><font>{LONG_TITLE}</font></a></div>'
    '<div><a href="https://example.com/a"><font>example.com</font></a></div></div></td></tr></table></a>'
)


@pytest.mark.parametrize("subject", ["", LONG_TITLE], ids=["no-subject", "title-subject"])
@pytest.mark.parametrize(
    "shape",
    [dict(html=CARD), dict(plain=f"{LONG_TITLE}\nhttps://example.com/a\n", html=CARD)],
    ids=["html-only", "plain-and-html"],
)
async def test_rich_link_with_a_long_title_is_a_link_share(proc, pages, subject, shape):
    [r] = parse_email(mail(subject, **shape)).requests
    assert (r.kind, r.url) == ("url", "https://example.com/a")
    assert r.title == LONG_TITLE
    pages["https://example.com/a"] = LONG_TITLE + " | Example"
    [item] = await proc.process_many([r])
    assert item.kind == "url" and item.word_count == ARTICLE_WORDS


def test_plain_link_with_a_long_subject_title_is_a_link_share():
    [r] = parse_email(mail(LONG_TITLE, plain=f"{LONG_TITLE}\nhttps://example.com/a\n")).requests
    assert (r.kind, r.url, r.title) == ("url", "https://example.com/a", LONG_TITLE)


# ------------------------------------------- classification rule shared with HTTP shares

COMMENT = "My brother sent me this one and I think you will like the part about the old trains"  # 18 words


def test_one_link_with_a_comment_goes_to_the_processor_as_a_text_only_share():
    [r] = parse_email(mail("Trains", plain=f"{COMMENT}\nhttps://example.com/trains\n")).requests
    assert (r.kind, r.url, r.title) == ("auto", "", "Trains")
    assert r.text == f"{COMMENT}\n\nhttps://example.com/trains"


def test_several_links_with_little_text_become_one_link_each_with_the_comment():
    links = ["https://example.com/one", "https://example.com/two", "https://example.com/three"]
    reqs = parse_email(mail("Links", plain=f"{COMMENT}:\n" + "\n".join(links) + "\n")).requests
    assert [(r.kind, r.url, r.text, r.title) for r in reqs] == [("auto", "", f"{COMMENT}:\n\n{u}", "") for u in links]
    reqs = parse_email(mail("Links", plain="\n".join(links) + "\n")).requests
    assert [(r.kind, r.url) for r in reqs] == [("url", u) for u in links]


async def test_link_with_a_comment_is_the_fetched_article_with_the_comment(proc, store, pages):
    raw = mail("Trains", plain=f"{COMMENT}\nhttps://example.com/trains\n")
    pages["https://example.com/trains"] = "Night trains of Europe"
    [item] = await proc.process_many(parse_email(raw).requests)
    assert item.kind == "url" and item.word_count >= ARTICLE_WORDS
    assert "the part about the old trains" in store.file_bytes(item.id, "txt").decode("utf-8")


def test_link_with_more_than_thirty_words_stays_a_note():
    text = f"{COMMENT}, and the long section about the night train to Vienna that we took together two summers ago."
    [r] = parse_email(mail("Trains", plain=f"{text}\nhttps://example.com/trains\n")).requests
    assert r.kind == "text" and r.url == "https://example.com/trains" and text in r.text
