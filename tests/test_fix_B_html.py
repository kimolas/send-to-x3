"""Regression tests for extract/html.py fixes (review findings 0, 20 and 24).

0:  email HTML went through trafilatura's article extraction, which drops newsletter
    lists and whole sections; ``html_to_document(..., whole=True)`` converts it all.
20: ``clean_title`` stripped leading title words that merely start with the site name.
24: apple.news / meta-refresh / canonical redirect stubs were not followed.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from aiohttp import web

from sendtox3.extract import html as html_mod
from sendtox3.extract.email import parse_email
from sendtox3.extract.html import clean_title, html_to_document, url_to_document
from sendtox3.textmodel import Heading, ListItem, Quote

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLE = (FIXTURES / "pages" / "article.html").read_bytes()
ARTICLE_TITLE = "Lighthouse keepers return to Gull Rock after forty years"


# --------------------------------------------------------------------------------------
# 0: whole=True for email bodies
# --------------------------------------------------------------------------------------

INTRO = (
    "<p>Good morning. This week brought a flurry of announcements about e-paper displays, "
    "a new open firmware release and a long essay about reading on small screens.</p>"
)
QUICK = (
    "<h2>Quick hits</h2><ul>"
    + "".join(
        f'<li><a href="https://news.example.com/{i}">Headline number {w} about displays</a></li>'
        for i, w in enumerate(["one", "two", "three", "four", "five", "six"])
    )
    + "</ul>"
)
READING = (
    "<h2>Reading list</h2><ol>"
    + "".join(f"<li>Essay {w} on attention and reading</li>" for w in ["alpha", "beta", "gamma", "delta"])
    + "</ol>"
)
DEEP = (
    "<h2>Deep dive</h2>"
    "<p>The firmware team rewrote the text layout engine so that long paragraphs no longer take seconds "
    "to paginate. The change landed after months of profiling on real hardware, and early testers report "
    "that chapters open almost instantly.</p>"
    "<blockquote>We measured every frame and cut what we could.</blockquote>"
)
FOOTER = (
    '<p style="font-size:11px">You are receiving this email because you subscribed. '
    '<a href="https://news.example.com/unsub">Unsubscribe</a></p>'
)
PREHEADER = '<div style="display:none;max-height:0;overflow:hidden">Preview text: this week in e-paper</div>'
PIXEL = '<img src="https://track.example.com/open.gif" width="1" height="1">'


def _div_layout() -> str:
    return (
        "<html><head><title>This week in e-paper</title><style>p{color:red}</style></head><body>"
        + PREHEADER
        + "<div class='wrapper'><div class='content'>"
        + INTRO + QUICK + READING + DEEP + FOOTER + PIXEL
        + "</div></div></body></html>"
    )  # fmt: skip


def _table_layout() -> str:
    def cell(inner: str) -> str:
        return f"<tr><td>{inner}</td></tr>"

    return (
        "<html><head><title>This week in e-paper</title></head><body>"
        + PREHEADER
        + "<table width='600'><tbody>"
        + cell(INTRO) + cell(QUICK) + cell(READING) + cell(DEEP) + cell(FOOTER + PIXEL)
        + "</tbody></table></body></html>"
    )  # fmt: skip


@pytest.mark.parametrize("layout", [_div_layout, _table_layout])
def test_whole_newsletter_keeps_every_list_and_section(layout):
    doc = html_to_document(layout(), whole=True)
    items = [b.text for b in doc.blocks if isinstance(b, ListItem)]
    assert items == [f"Headline number {w} about displays" for w in ["one", "two", "three", "four", "five", "six"]] + [
        f"Essay {w} on attention and reading" for w in ["alpha", "beta", "gamma", "delta"]
    ]
    assert [b.index for b in doc.blocks if isinstance(b, ListItem) and b.ordered] == [1, 2, 3, 4]
    headings = [b.text for b in doc.blocks if isinstance(b, Heading)]
    assert headings == ["Quick hits", "Reading list", "Deep dive"]
    assert any(isinstance(b, Quote) and "every frame" in b.text for b in doc.blocks)
    text = doc.plain_text()
    assert "Good morning" in text and "chapters open almost instantly" in text
    # chrome is still dropped: hidden preheader, styles, tracking pixel, unsubscribe footer
    assert "Preview text" not in text and "color:red" not in text
    assert "Unsubscribe" not in text and "receiving this email" not in text
    assert doc.title == "This week in e-paper"
    assert doc.notes == []


@pytest.mark.parametrize("layout", [_div_layout, _table_layout])
def test_article_extraction_still_used_without_whole(layout):
    """The default (web pages) is unchanged: trafilatura still decides."""
    doc = html_to_document(layout())
    assert doc.title == "This week in e-paper"
    assert "Deep dive" in doc.plain_text()


def test_whole_does_not_run_article_extraction(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("trafilatura must not run for whole=True")

    monkeypatch.setattr(html_mod, "_trafilatura_extract", boom)
    doc = html_to_document(_div_layout(), whole=True)
    assert "Headline number six about displays" in doc.plain_text()


def test_whole_html_attachment_keeps_the_previous_title():
    """Title metadata still comes from trafilatura's metadata pass, not the raw <title>."""
    html = (
        "<html><head><title>Saved page: the quiet screen</title></head>"
        "<body><h1>The quiet screen</h1><p>An essay saved as a web page, about screens that stay still.</p>"
        "</body></html>"
    )
    doc = html_to_document(html, whole=True)
    assert doc.title == html_to_document(html).title == "The quiet screen"
    assert doc.plain_text() == "An essay saved as a web page, about screens that stay still."


def test_whole_short_email_body_gets_no_notes():
    doc = html_to_document("<html><body><p>Thanks, see you on Tuesday.</p></body></html>", whole=True)
    assert doc.plain_text() == "Thanks, see you on Tuesday."
    assert doc.notes == []  # no "Only N words" / "needs JavaScript"


def test_whole_keeps_a_body_that_is_only_chrome_like():
    # never strip everything: a one-line email about unsubscribing is still the content
    doc = html_to_document("<p>Please unsubscribe me from the club list.</p>", whole=True)
    assert doc.plain_text() == "Please unsubscribe me from the club list."


def test_whole_drops_chrome_only_at_the_edges():
    filler = "".join(f"<p>Paragraph {i} of the issue, about e-paper.</p>" for i in range(10))
    middle = "<p>Unsubscribe here: why people leave mailing lists, a short history.</p>"
    body = (
        "<p>View this email in your browser</p>" + filler + middle + filler
        + "<p>Unsubscribe | Manage preferences</p><p>Click here to unsubscribe.</p>"
    )  # fmt: skip
    doc = html_to_document("<html><body>" + body + "</body></html>", whole=True)
    text = doc.plain_text()
    assert "View this email" not in text and "Manage preferences" not in text and "Click here" not in text
    assert "why people leave mailing lists" in text  # in the middle of the content: kept


def test_whole_personal_email_mentioning_unsubscribe_is_kept():
    body = "<p>Hi Bob,</p><p>Can you unsubscribe me from the club list? It sends three emails a day.</p><p>Thanks, Ann</p>"
    doc = html_to_document(body, whole=True)
    assert "Can you unsubscribe me from the club list?" in doc.plain_text()


def test_forward_as_attachment_email_keeps_its_list():
    """The repo fixture from the review: the essay list was gone from the stored TXT."""
    p = parse_email((FIXTURES / "emails" / "forward_as_attachment.eml").read_bytes())
    [req] = [r for r in p.requests if r.html]
    doc = html_to_document(req.html, whole=True)
    items = [b.text for b in doc.blocks if isinstance(b, ListItem)]
    assert items == ["On boredom, by an anonymous reader", "The quiet screen", "Paragraphs as places"]
    assert "three essays about attention" in doc.plain_text()
    assert doc.notes == []


# --------------------------------------------------------------------------------------
# 20: clean_title
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, site, host, expected",
    [
        (
            "Nature’s 10: ten people who helped shape science in 2023",
            "Nature",
            "nature.com",
            "Nature’s 10: ten people who helped shape science in 2023",
        ),
        ("Science of sleep: why we dream", "", "science.org", "Science of sleep: why we dream"),
        ("Wired for sound: how headphones changed music", "", "wired.com", "Wired for sound: how headphones changed music"),
        ("Wired for sound: how headphones changed music", "WIRED", "wired.com", "Wired for sound: how headphones changed music"),
        ("Wired for sound - how headphones changed music", "", "wired.com", "Wired for sound - how headphones changed music"),
        # exact site names are still stripped at either end (a leading "Site: " only when
        # it is the site name itself, not just the host's name: R3 / python.org)
        ("Wired: how headphones changed music", "WIRED", "wired.com", "how headphones changed music"),
        ("Wired: how headphones changed music", "", "wired.com", "Wired: how headphones changed music"),
        ("Nature: ten people who helped shape science", "Nature", "nature.com", "ten people who helped shape science"),
        ("How headphones changed music | WIRED", "WIRED", "wired.com", "How headphones changed music"),
        # a trailing segment that starts with the site name is still the site
        ("Battle Drones | Simon Willison’s Weblog", "", "simonwillison.net", "Battle Drones"),
    ],
)
def test_clean_title_leading_segments_need_an_exact_site_match(title, site, host, expected):
    assert clean_title(title, site=site, host=host) == expected


# --------------------------------------------------------------------------------------
# 24: redirect stubs (apple.news, meta refresh, canonical on another host)
# --------------------------------------------------------------------------------------

STUB_DESC = "A short teaser about the lighthouse keepers who return to the rock after forty years away."


def _stub(head_extra: str = "", body_extra: str = "") -> bytes:
    return (
        "<html><head><title>Lighthouse keepers return</title>"
        '<meta property="og:title" content="Lighthouse keepers return to Gull Rock">'
        f'<meta property="og:description" content="{STUB_DESC}">'
        f"{head_extra}</head><body><p>Opening in Apple News...</p>{body_extra}</body></html>"
    ).encode()


@pytest.fixture
async def stub_site(aiohttp_server):
    app = web.Application()
    app["hits"] = []
    state: dict[str, str] = {}

    @web.middleware
    async def count(request, handler):
        app["hits"].append(request.path)
        return await handler(request)

    app.middlewares.append(count)

    def html(body: bytes, status: int = 200):
        async def handler(request):
            return web.Response(status=status, body=body, content_type="text/html")

        return handler

    async def apple(request):
        script = f'<script>redirectToUrlAfterTimeout("{state["base"]}/article", 0);</script>'
        return web.Response(body=_stub(body_extra=script), content_type="text/html")

    async def apple_dead(request):
        script = f'<script>redirectToUrlAfterTimeout("{state["base"]}/gone", 0);</script>'
        return web.Response(body=_stub(body_extra=script), content_type="text/html")

    async def apple_local(request):
        script = f'<script>redirectToUrlAfterTimeout("{state["other"]}/article", 0);</script>'
        return web.Response(body=_stub(body_extra=script), content_type="text/html")

    async def canonical_other_host(request):
        link = f'<link rel="canonical" href="{state["other"]}/article">'
        return web.Response(body=_stub(head_extra=link), content_type="text/html")

    async def canonical_same_host(request):
        link = f'<link rel="canonical" href="{state["base"]}/article">'
        return web.Response(body=_stub(head_extra=link), content_type="text/html")

    async def refresh_long(request):
        paras = "".join(f"<p>Paragraph {i} of a perfectly normal and rather long article body.</p>" for i in range(40))
        body = (
            '<html><head><title>Long page</title><meta http-equiv="refresh" content="0; url=/article"></head>'
            f"<body><article>{paras}</article></body></html>"
        )
        return web.Response(body=body.encode(), content_type="text/html")

    routes = {
        "/article": html(ARTICLE),
        "/apple": apple,
        "/apple-dead": apple_dead,
        "/apple-local": apple_local,
        "/refresh": html(_stub(head_extra='<meta http-equiv="refresh" content="0; URL=\'/article\'">')),
        "/loop-a": html(_stub(head_extra='<meta http-equiv="refresh" content="0;url=/loop-b">')),
        "/loop-b": html(_stub(head_extra='<meta http-equiv="refresh" content="0;url=/loop-a">')),
        "/self": html(_stub(head_extra='<meta http-equiv="refresh" content="5;url=/self">')),
        "/reload": html(_stub(head_extra='<meta http-equiv="refresh" content="300">')),
        "/canonical-other": canonical_other_host,
        "/canonical-same": canonical_same_host,
        "/refresh-long": refresh_long,
        "/gone": html(b"<html><body><h1>Not found</h1></body></html>", status=404),
    }
    for path, handler in routes.items():
        app.router.add_get(path, handler)
    srv = await aiohttp_server(app, host="127.0.0.1")
    state["base"] = f"http://127.0.0.1:{srv.port}"
    state["other"] = f"http://localhost:{srv.port}"  # same server, different host name
    return types.SimpleNamespace(hits=app["hits"], url=lambda path: f"{state['base']}{path}")


@pytest.mark.parametrize("path", ["/apple", "/refresh", "/canonical-other"])
async def test_redirect_stub_is_followed_to_the_article(stub_site, opts, path):
    url = stub_site.url
    doc = await url_to_document(url(path), opts=opts)
    assert doc.title == ARTICLE_TITLE
    assert doc.word_count() > 300 and doc.notes == []
    assert doc.source_url.endswith("/article")
    assert stub_site.hits == [path, "/article"]


async def test_stub_target_that_fails_keeps_the_teaser(stub_site, opts):
    doc = await url_to_document(stub_site.url("/apple-dead"), opts=opts)
    assert STUB_DESC in doc.plain_text()  # the teaser, with its "only N words" note
    assert any("words could be extracted" in n for n in doc.notes)
    assert doc.source_url.endswith("/apple-dead")
    assert stub_site.hits == ["/apple-dead", "/gone"]


async def test_stubs_are_followed_only_once(stub_site, opts):
    doc = await url_to_document(stub_site.url("/loop-a"), opts=opts)
    assert stub_site.hits == ["/loop-a", "/loop-b"]
    assert STUB_DESC in doc.plain_text()


@pytest.mark.parametrize("path", ["/self", "/reload", "/canonical-same", "/refresh-long"])
async def test_no_follow_for_self_links_same_host_canonicals_or_real_pages(stub_site, opts, path):
    await url_to_document(stub_site.url(path), opts=opts)
    assert stub_site.hits == [path]


async def test_stub_target_goes_through_the_ssrf_guard(stub_site, opts, monkeypatch):
    import dataclasses

    from sendtox3.extract import fetch as fetch_mod

    real = fetch_mod.is_blocked_ip
    monkeypatch.setattr(fetch_mod, "is_blocked_ip", lambda ip: False if ip == "127.0.0.1" else real(ip))
    guarded = dataclasses.replace(opts, allow_private_urls=False)
    doc = await url_to_document(stub_site.url("/apple-local"), opts=guarded)
    assert stub_site.hits == ["/apple-local"]  # http://localhost:... was refused, not fetched
    assert STUB_DESC in doc.plain_text()


@pytest.mark.parametrize(
    "content, expected",
    [
        ("0;url=https://x.example/a", "https://x.example/a"),
        ("0; URL='https://x.example/b'", "https://x.example/b"),
        ("0;https://x.example/c", "https://x.example/c"),
        ("url=https://x.example/d", "https://x.example/d"),
        ('  0 ,  url = "/f" ', "/f"),
        ("300", ""),  # only reloads the page
        ("5;", ""),
        ("0; url=", ""),
        ("0;" + " " * 100_000, ""),
    ],
)
def test_meta_refresh_url_parsing(content, expected):
    assert html_mod._refresh_url(content) == expected


# --------------------------------------------------------------------------------------
# Same bug class as finding 5, in this module: crafted pages must not hold the GIL
# --------------------------------------------------------------------------------------


def _secs(fn, *args, **kw):
    import time

    t0 = time.perf_counter()
    out = fn(*args, **kw)
    return out, time.perf_counter() - t0


def test_unclosed_title_tags_are_linear():
    # trafilatura finds no title, so the <title> fallback runs (it took 11 s before)
    html = "<html><body><p>Hello world.</p>" + "<title>" * 20_000 + "</body></html>"
    doc, secs = _secs(html_to_document, html, url="https://x.example/a")
    assert secs < 2 and doc.title == "Hello world."


def test_unclosed_meta_and_link_tags_are_linear():
    head = "<html><head>" + "<meta " * 60_000
    _, secs = _secs(html_mod._meta_tags, head)
    assert secs < 0.5
    _, secs = _secs(html_mod._canonical_link, "<html><head>" + "<link " * 60_000)
    assert secs < 0.5


def test_hidden_style_check_is_linear():
    html = '<p style="max-height:0' + " " * 100_000 + 'x">hello</p>'
    doc, secs = _secs(html_to_document, html, whole=True)
    assert secs < 0.5 and doc.plain_text() == "hello"
