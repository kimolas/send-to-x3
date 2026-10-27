"""Round-2 regression tests for the iOS Shortcut.

R2-G1: a share that is only a domain name (``example.com/a``) is a link again, sent as
``https://example.com/a``. R2-G2: an emailed text starts with its first web link again
(``<link>\\n\\n<text>``), and with no link the body is the text alone. The end-to-end
tests send one share both ways (the simulated HTTP request, and the simulated email
through the real email parser) into the real Processor, with only the page fetch faked,
and check that the X3 gets the same item.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.policy import SMTP

import pytest
from test_shortcuts import bs, run, sim

from sendtox3 import delivery, ingest
from sendtox3.extract.email import parse_email
from sendtox3.ingest import Processor
from sendtox3.models import IngestRequest
from sendtox3.textmodel import Document, Paragraph

AWAY = "Cafe Free WiFi"  # not a home network: auto mode emails
PAGE = "https://news.example.com/2026/10/big-story"
ARTICLE_WORDS = 200

# ------------------------------------------------------ R2-G1: a domain name alone


@pytest.mark.parametrize("shared, url", [
    ("example.com", "https://example.com"),
    ("  Example.com/a?b=1\n", "https://Example.com/a?b=1"),
    ("www.example.org/2026/story", "https://www.example.org/2026/story"),
    ("news.example.co.uk", "https://news.example.co.uk"),
])
def test_a_share_that_is_only_a_domain_name_is_sent_as_a_link(shared, url):
    r, [req] = run(inp=[sim.Text(shared)])
    assert req.json_body == {"url": url, "text": "", "title": "", "source": "ios-shortcut"}
    assert r.notifications and not r.alerts


def test_a_domain_name_alone_is_emailed_as_a_link():
    r, log = run(wifi=AWAY, inp=[sim.Text("example.com/a")])
    assert not log and r.emails[0].body == "https://example.com/a\n\n"


@pytest.mark.parametrize("shared", [
    "see example.com",  # a sentence that mentions a domain
    "example.com.",     # ... or ends with a full stop
    "Node.js",          # shaped like a domain, but iOS finds no link in it
    "jane@acme.org",    # an email address (iOS: a mailto: link)
    "3.14",
])
def test_text_that_only_looks_like_a_domain_stays_text(shared):
    _, [req] = run(inp=[sim.Text(shared)])
    assert req.json_body["url"] == "" and req.json_body["text"] == shared


def test_the_domain_check_needs_ios_to_see_a_link():
    """The premise of the "Node.js" case: the regex alone would take it, iOS does not."""
    assert sim.replace_text(bs.BARE_HOST_RE, "http://$1", "Node.js", case_sensitive=False) == "http://Node.js"
    assert sim.find_links("Node.js") == []


# ------------------------------------------------ R2-G2: the email starts with the link


@pytest.mark.parametrize("inp, link", [
    ([sim.Text("Interesting thread\nhttps://example.com/t")], "https://example.com/t"),
    ([sim.Text("Interesting thread"), sim.URL("https://example.com/t")], "https://example.com/t"),  # app: text + link
    ([sim.Text("Call bob.smith@example.co.uk, then read https://example.com/q3.")], "https://example.com/q3"),
    ([sim.Text("The report: HTTPS://Example.com/r")], "HTTPS://Example.com/r"),
    ([sim.RichText("A long note. " * 60 + "Source: https://example.com/long", links=("https://hidden.example/x",))],
     "https://example.com/long"),
])
def test_emailed_text_starts_with_its_first_web_link(inp, link):
    text = "\n".join(sim.item_text(i) for i in inp)
    r, log = run(wifi=AWAY, inp=inp)
    assert not log and [m.body for m in r.emails] == [f"{link}\n\n{text}"]
    _, [req] = run(inp=inp)  # over HTTP the same share is still the text alone
    assert req.json_body["url"] == "" and req.json_body["text"] == text


@pytest.mark.parametrize("inp", [
    [sim.Text("Email jane@acme.org about the Q3 numbers")],  # only a mailto: link
    [sim.Text("Pricing is on example.com somewhere")],       # a domain iOS reads as a link, not written as one
    [sim.Text("Just a thought, no links at all.")],
    [sim.URL("mailto:jane@acme.org")],
    [sim.URL("tel:+15551234567")],
])
def test_emailed_text_without_a_written_web_link_is_the_text_alone(inp):
    r, _ = run(wifi=AWAY, inp=inp)
    assert [m.body for m in r.emails] == [sim.item_text(inp[0])]


# ------------------------------------------- end to end: both routes give the same item


def as_sent(mail: sim.Email) -> bytes:
    """The simulated Send Email as the Mail app sends it (plain text, UTF-8)."""
    m = EmailMessage()
    m["From"] = "Me <me@example.com>"
    m["To"] = ", ".join(mail.to)
    m["Subject"] = mail.subject
    m.set_content(mail.body)
    return m.as_bytes(policy=SMTP)


def requests_for(route: str, inp: list) -> list[IngestRequest]:
    if route == "http":
        _, [req] = run(inp=inp)
        body = req.json_body  # what web/ingest_api.py makes of the JSON
        return [IngestRequest(kind="auto", source="http", url=body["url"].strip(), text=body["text"],
                              title=body["title"].strip())]
    r, log = run(wifi=AWAY, inp=inp)
    assert not log
    [mail] = r.emails
    return parse_email(as_sent(mail)).requests


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


ROUTES = pytest.mark.parametrize("route", ["http", "email"])
NOTE_KIND = {"http": "text", "email": "email"}  # a note that came by email is listed as an email


@ROUTES
async def test_link_with_a_comment_is_the_article_with_the_comment(route, proc, store, pages):
    pages[PAGE] = "Big Story Headline"
    [item] = await proc.process_many(requests_for(route, [sim.Text(f"Worth reading before Monday\n{PAGE}")]))
    assert (item.kind, item.status, item.source_url, item.word_count) == ("url", "ready", PAGE, ARTICLE_WORDS)
    assert delivery.load_document(store, item.id).notes == ["Comment: Worth reading before Monday"]


@ROUTES
async def test_long_note_keeps_its_link_as_the_source(route, proc, pages):
    note = ("These are my notes from the meeting, written up on the train home. " * 12
            + f"The slides are at {PAGE} if anyone wants them. " + "More thoughts follow here. " * 8).strip()
    [item] = await proc.process_many(requests_for(route, [sim.Text(note)]))
    assert (item.kind, item.source_url) == (NOTE_KIND[route], PAGE)  # the email used to lose the source


@ROUTES
async def test_domain_name_alone_is_the_fetched_page(route, proc, pages):
    pages["https://example.com/a"] = "Example page"
    [item] = await proc.process_many(requests_for(route, [sim.Text("example.com/a")]))
    assert (item.kind, item.source_url, item.word_count) == ("url", "https://example.com/a", ARTICLE_WORDS)


@ROUTES
async def test_domain_mentioned_before_the_link(route, proc, store, pages):
    """iOS's first link is then the domain, not written as a link, so the email does not
    start with a link; the app still finds the written one in the text, as over HTTP."""
    text = f"Pricing is on acme.io, the report at {PAGE}"
    if route == "email":
        assert [r.url for r in requests_for(route, [sim.Text(text)])] != ["http://acme.io"]
    pages[PAGE] = "Big Story Headline"
    [item] = await proc.process_many(requests_for(route, [sim.Text(text)]))
    assert (item.kind, item.source_url, item.word_count) == ("url", PAGE, ARTICLE_WORDS)
    assert delivery.load_document(store, item.id).notes == ["Comment: Pricing is on acme.io, the report at"]


@ROUTES
async def test_text_without_a_link_is_a_note(route, proc, pages):
    [item] = await proc.process_many(requests_for(route, [sim.Text("Buy more e-ink paper.\nAnd a pen.")]))
    assert (item.kind, item.source_url) == (NOTE_KIND[route], "")
