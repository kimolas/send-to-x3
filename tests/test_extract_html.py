"""HTML -> Document (trafilatura mapping, metadata, notes) and URL -> Document routing."""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import time
import types
from pathlib import Path

import pytest
from aiohttp import web

from sendtox3.extract.fetch import decode_html
from sendtox3.extract.html import (
    PRUNE_XPATH,
    clean_author,
    clean_title,
    html_fragment_to_blocks,
    html_to_document,
    url_to_document,
)
from sendtox3.textmodel import Document, Heading, ListItem, Paragraph, Preformatted, Quote, Rule

PAGES = Path(__file__).parent / "fixtures" / "pages"


def page(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


def kinds(blocks) -> list[str]:
    return [type(b).__name__ for b in blocks]


def all_text(doc: Document) -> str:
    return "\n".join(getattr(b, "text", "") for b in doc.blocks)


# --------------------------------------------------------------------------------------
# Fixture pages
# --------------------------------------------------------------------------------------


def test_article_with_boilerplate():
    doc = html_to_document(page("article.html"), url="https://www.example-gazette.test/2026/gull-rock")
    assert doc.title == "Lighthouse keepers return to Gull Rock after forty years"
    assert doc.byline == "Marta Quillfeather"
    assert doc.site == "The Example Gazette"
    assert doc.published == "2026-09-14"
    assert doc.lang == "en"
    assert doc.source_url == "https://www.example-gazette.test/2026/gull-rock"
    assert doc.notes == []
    text = all_text(doc)
    for junk in ("Accept all", "Related stories", "Sign up for our morning briefing", "Privacy policy", "Weather"):
        assert junk not in text
    assert "140 stone steps" in text and "five strangers" in text
    # the h1 repeating the title is not duplicated in the body
    assert doc.blocks[0].text.startswith("For the first time since the light was automated")
    assert [(b.text, b.level) for b in doc.blocks if isinstance(b, Heading)] == [
        ("A rota of volunteers", 2),
        ("What the restoration involved", 2),
    ]
    assert (
        Quote(
            "The light still turns every ten seconds, exactly as it did when my grandfather wound the clockwork by hand."
        )
        in doc.blocks
    )
    assert doc.word_count() > 300


def test_lists_code_tables_and_line_breaks():
    doc = html_to_document(page("structures.html"), url="https://notebook.example.net/tide-clock")
    assert doc.title == "Building a tide clock from spare parts"
    assert doc.byline == "R. Pennywhistle"  # punctuation from the meta tag kept
    items = [b for b in doc.blocks if isinstance(b, ListItem)]
    assert [(i.text, i.ordered, i.index, i.depth) for i in items] == [
        ("A quartz clock movement with a seconds output", True, 1, 0),
        ("A gear train with these stages:", True, 2, 0),
        ("a 12:1 reduction from the movement", False, 1, 1),
        ("a 1.035:1 correction stage", False, 2, 1),
        ("A dial painted with high and low water marks", True, 3, 0),
    ]
    code = [b for b in doc.blocks if isinstance(b, Preformatted)]
    assert len(code) == 1
    assert code[0].text.splitlines()[:3] == [
        "def adjusted_seconds(now):",
        "    period = 44714  # seconds in a mean tidal cycle",
        "    return (now * 43200) // period",
    ]
    # inline <code> stays inside its paragraph (trafilatura emits it as <pre> inside <p>)
    assert any(isinstance(b, Paragraph) and "with pulse_coil() once per adjusted second" in b.text for b in doc.blocks)
    for row in ("Week | Drift (minutes)", "1 | 2", "2 | 3", "3 | 1"):
        assert Paragraph(row) in doc.blocks
    i = doc.blocks.index(Paragraph("Time and tide wait for no one,"))
    assert doc.blocks[i + 1 : i + 3] == [
        Paragraph("but the clock will wait for both,"),
        Paragraph("and the sea will keep its own count."),
    ]
    assert "Great build" not in all_text(doc)  # comments are not content


def test_wikipedia_markup_is_cleaned():
    doc = html_to_document(page("wikipedia_like.html"), url="https://en.wikipedia.org/wiki/Glass_harmonica_tuning")
    assert doc.title == "Glass harmonica tuning"
    assert doc.site == "Wikipedia"
    assert doc.byline == ""  # "Authority control databases ..." is not an author
    assert doc.published == ""  # an encyclopedia article has no publication date
    text = all_text(doc)
    for junk in ("[edit]", "[1]", "[2]", "citation needed", "Authority control", "^ Example", "Navigation menu"):
        assert junk not in text
    assert "edit" not in text.split()
    heads = [(b.text, b.level) for b in doc.blocks if isinstance(b, Heading)]
    assert heads == [("History", 2), ("Workshop practice", 3)]  # empty References section dropped
    assert [b.text for b in doc.blocks if isinstance(b, ListItem)] == [
        "Strike the bowl and compare it with the fork.",
        "Grind a narrow band of the rim if the bowl is flat.",
        "Rest the bowl and repeat the comparison.",
    ]
    assert doc.blocks[0].text.startswith("Glass harmonica tuning is the process of adjusting the pitch")
    assert doc.blocks[0].text.endswith("before and after every pass of the grinding wheel.")


def test_substack_post_survives_and_widgets_are_dropped():
    doc = html_to_document(page("substack_like.html"), url="https://rooftophive.substack.com/p/notes-on-keeping-bees")
    assert doc.title == "Notes on keeping bees in a city flat"
    assert doc.byline == "Juniper Ashgrove"
    assert doc.site == "The Rooftop Hive"
    assert doc.published == "2026-08-02"
    text = all_text(doc)
    assert "Thanks for reading" not in text and "Type your email" not in text and "Share this post" not in text
    h2 = [b.text for b in doc.blocks if isinstance(b, Heading) and b.level == 2]
    assert h2 == ["Choosing the hive", "Water and neighbours", "The first harvest"]
    assert doc.word_count() > 200  # the "newsletter-post" wrapper did not swallow the post


def test_paywalled_article_gets_a_note_and_the_preview():
    doc = html_to_document(page("paywall.html"), url="https://ledger.example.com/free-shipping")
    assert doc.title == "The hidden cost of free shipping"
    assert any("subscriber-only" in n for n in doc.notes)
    assert any(n.startswith("Only ") for n in doc.notes)
    assert "somebody still pays for the van" in all_text(doc)


def test_js_only_page():
    doc = html_to_document(page("js_only.html"), url="https://planner.example.com/p/weekend")
    assert doc.title == "Weekend plan: three walks along the river"
    assert any("needs JavaScript" in n for n in doc.notes)
    assert "enable JavaScript" not in all_text(doc)
    assert doc.blocks == [
        Paragraph("A short list of riverside walks for a sunny weekend, with timings and cafe stops.")
    ]


def test_repeated_extraction_is_not_deduplicated_away():
    # trafilatura's default dedup cache is process-wide: the 3rd+ extraction of a page
    # would lose paragraphs and finally the whole document.
    html = page("article.html")
    counts = {html_to_document(html, url="https://www.example-gazette.test/a").word_count() for _ in range(5)}
    assert len(counts) == 1 and counts.pop() > 300


def test_prune_rules_are_precise():
    for rule in PRUNE_XPATH:
        assert "concat(' ',normalize-space(@class),' ')" in rule and "newsletter" not in rule


# --------------------------------------------------------------------------------------
# Encodings through the extractor
# --------------------------------------------------------------------------------------

RU_PARAS = [
    "Молодая рыжая собака бегала взад и вперед по тротуару и беспокойно оглядывалась по сторонам.",
    "Изредка она останавливалась и, плача, приподнимая то одну озябшую лапу, то другую, старалась понять, как это могло случиться.",
    "Она отлично помнила, как провела день и как в конце концов попала на этот незнакомый тротуар.",
    "День начался с того, что ее хозяин надел шапку, взял под мышку какую-то деревянную штуку и крикнул ее по имени.",
]


def test_koi8r_page_with_template_lang_en_is_russian():
    body = "".join(f"<p>{p}</p>" for p in RU_PARAS * 3)
    html = f'<html lang="en"><head><title>Каштанка</title></head><body><article>{body}</article></body></html>'
    raw = html.encode("koi8-r")
    doc = html_to_document(decode_html(raw, "koi8-r"), url="http://lib.example.ru/kashtanka.txt")
    assert doc.title == "Каштанка"
    assert doc.lang == "ru"
    assert RU_PARAS[0] in all_text(doc)


def test_shift_jis_meta_page():
    paras = [
        "ジョバンニは、口笛を吹いているような、さびしい口付きで、檜のまっ黒にならんだ町の坂を下りて来たのでした。"
    ] * 6
    html = (
        '<?xml version="1.0" encoding="Shift_JIS"?>\n<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja"><head>'
        '<meta http-equiv="Content-Type" content="text/html;charset=Shift_JIS" /><title>銀河鉄道の夜</title></head>'
        '<body><div class="main_text">' + "<br />\n".join(paras) + "</div></body></html>"
    )
    doc = html_to_document(decode_html(html.encode("cp932"), None), url="https://books.example.jp/456.html")
    assert doc.title == "銀河鉄道の夜"
    assert doc.lang == "ja"
    assert "ジョバンニは" in all_text(doc)


def test_lying_meta_charset():
    html = (
        '<html><head><meta charset="iso-8859-1"><title>Crème brûlée</title></head><body><article>'
        + "<p>Le café du coin sert une crème brûlée délicieuse, préparée chaque matin par le chef.</p>" * 6
        + "</article></body></html>"
    )
    doc = html_to_document(decode_html(html.encode("utf-8"), None), url="https://cuisine.example.fr/creme")
    assert doc.title == "Crème brûlée"
    assert "Le café du coin sert une crème brûlée délicieuse" in all_text(doc)
    assert doc.lang == "fr" or doc.lang == "en"  # no lang attribute: stop-word guess


# --------------------------------------------------------------------------------------
# Metadata helpers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, site, host, expected",
    [
        ("Electronic paper - Wikipedia", "", "en.wikipedia.org", "Electronic paper"),
        ("Electronic paper – Wikipedia", "Wikipedia", "de.wikipedia.org", "Electronic paper"),
        ("GitHub - owner/repo: A tool", "GitHub", "github.com", "owner/repo: A tool"),
        ("Story title | The Example Gazette", "The Example Gazette", "example-gazette.com", "Story title"),
        ("The Example Gazette - Story title", "The Example Gazette", "example-gazette.com", "Story title"),
        ("Battle Drones | Simon Willison’s Weblog", "", "simonwillison.net", "Battle Drones"),
        ("Title | Technology | The Guardian", "The Guardian", "theguardian.com", "Title | Technology"),
        ("UK to invest in labs - BBC News", "BBC News", "bbc.co.uk", "UK to invest in labs"),
        ("How to Do Great Work", "paulgraham.com", "paulgraham.com", "How to Do Great Work"),
        (
            "Your Book Review: The Ghost In My Brain",
            "Astral Codex Ten",
            "astralcodexten.com",
            "Your Book Review: The Ghost In My Brain",
        ),
        # a leading "Site: " goes only when a headline of 4+ words remains
        ("Astral Codex Ten: Your Book Review", "Astral Codex Ten", "astralcodexten.com",
         "Astral Codex Ten: Your Book Review"),
        ("Astral Codex Ten: Your Book Review of Ghosts", "Astral Codex Ten", "astralcodexten.com",
         "Your Book Review of Ghosts"),
        ("Breaking - Timeline of events", "", "time.com", "Breaking - Timeline of events"),
        ("A - B", "", "", "A - B"),
        ("Wikipedia", "Wikipedia", "en.wikipedia.org", "Wikipedia"),
        ("  Spaced   \u200btitle  ", "", "", "Spaced title"),
        ("", "Site", "site.com", ""),
    ],
)
def test_clean_title(title, site, host, expected):
    assert clean_title(title, site=site, host=host) == expected


@pytest.mark.parametrize(
    "author, expected",
    [
        ("Authority control databases National Japan", ""),
        ("x" * 81, ""),
        ("By Jane Doe", "Jane Doe"),
        ("Jane Doe; John Roe", "Jane Doe, John Roe"),
        ("https://example.com/author/jane", ""),
        ("Wikipedia contributors", ""),
        ("Marta Quillfeather", "Marta Quillfeather"),
        ("", ""),
    ],
)
def test_clean_author(author, expected):
    assert clean_author(author) == expected


# --------------------------------------------------------------------------------------
# html_fragment_to_blocks
# --------------------------------------------------------------------------------------


def test_fragment_lists_and_items_with_paragraphs():
    blocks = html_fragment_to_blocks(
        '<ol start="3"><li><p>first part</p><p>second part</p></li><li>next<ul><li>child</li></ul>tail</li></ol>'
    )
    assert blocks == [
        ListItem("first part second part", ordered=True, index=3, depth=0),
        ListItem("next", ordered=True, index=4, depth=0),
        ListItem("child", ordered=False, index=1, depth=1),
        ListItem("tail", ordered=True, index=4, depth=0),
    ]


def test_fragment_blockquote_with_list_and_rule():
    blocks = html_fragment_to_blocks(
        "<blockquote><p>Said:</p><ul><li>one</li></ul><ol><li>two</li></ol></blockquote><hr><p>After</p>"
    )
    assert blocks == [Quote("Said:"), Quote("• one"), Quote("1. two"), Rule(), Paragraph("After")]


def test_fragment_skips_scripts_hidden_and_citations():
    blocks = html_fragment_to_blocks(
        "<p>E = mc<sup>2</sup> is famous<sup>[1]</sup><sup>[when?]</sup>.<script>evil()</script>"
        '<span hidden>secret</span><span aria-hidden="true">icon</span><style>p{}</style></p>'
        "<noscript>enable js</noscript><template><p>t</p></template>"
    )
    assert blocks == [Paragraph("E = mc2 is famous.")]


def test_fragment_tables():
    data = html_fragment_to_blocks(
        "<table><caption>Results</caption><tr><th>A</th><th>B</th></tr><tr><td>1</td><td></td></tr></table>"
    )
    assert data == [Paragraph("Results"), Paragraph("A | B"), Paragraph("1")]
    layout = html_fragment_to_blocks("<table><tr><td><p>Column prose one.</p><p>Two.</p></td></tr></table>")
    assert layout == [Paragraph("Column prose one."), Paragraph("Two.")]


def test_fragment_pre_and_br():
    blocks = html_fragment_to_blocks("<pre>\n  indented\n\tline</pre><p>a<br>b<br/><br/>c</p><h2>Head<br>ing</h2>")
    assert blocks == [
        Preformatted("  indented\n    line"),
        Paragraph("a"),
        Paragraph("b"),
        Paragraph("c"),
        Heading("Head ing", 2),
    ]


def test_fragment_xml_mode_keeps_trafilatura_inline_pre():
    xml = "<html><body><p>Run <pre>make all</pre> now.</p><pre>line1\nline2</pre></body></html>"
    assert html_fragment_to_blocks(xml, xml=True) == [Paragraph("Run make all now."), Preformatted("line1\nline2")]


def test_fragment_footnotes():
    blocks = html_fragment_to_blocks(
        '<p>Claim<a class="footnote-anchor" href="#f1">1</a>.</p>'
        '<div class="footnote"><a class="footnote-number" href="#a1">1</a><div class="footnote-content"><p>Source.</p></div></div>'
    )
    assert blocks == [Paragraph("Claim[1]."), Paragraph("[1] Source.")]


def test_fragment_bare_preformatted_prose_is_reflowed():
    prose = (
        "     The young dog ran up and down the pavement, looking anxiously\n"
        "about her and stopping now and then.\n"
        "     - Where is my master? she thought, and whined.\n"
        "     She remembered the morning very well, and the long walk\n"
        "through the snow with the carpenter.\n"
    )
    blocks = html_fragment_to_blocks(f"<html><body>{prose}</body></html>")
    assert blocks == [
        Paragraph("The young dog ran up and down the pavement, looking anxiously about her and stopping now and then."),
        Paragraph("- Where is my master? she thought, and whined."),
        Paragraph("She remembered the morning very well, and the long walk through the snow with the carpenter."),
    ]


def test_fragment_deeply_nested_markup_keeps_its_text():
    deep = "<div>" * 1200 + "<p>deep text here</p>" + "</div>" * 1200
    assert html_fragment_to_blocks(deep) == [Paragraph("deep text here")]


def test_fragment_empty():
    assert html_fragment_to_blocks("") == [] and html_fragment_to_blocks("   ") == []


def test_short_html_fragment_keeps_everything():
    doc = html_to_document("<p>Hello <b>world</b>, this is a short selection.</p><ul><li>one</li><li>two</li></ul>")
    assert doc.blocks[:3] == [
        Paragraph("Hello world, this is a short selection."),
        ListItem("one", index=1),
        ListItem("two", index=2),
    ]
    assert doc.title == "Hello world, this is a short selection."
    assert doc.lang == "en"


def test_empty_html():
    doc = html_to_document("")
    assert doc.blocks == [] and doc.notes and doc.notes[0].startswith("Only 0 words")


def test_title_falls_back_to_first_heading():
    html = (
        "<html><body><article><h2>Harbour notes</h2>"
        + "<p>Ships came and went all day long in the harbour.</p>" * 8
        + "</article></body></html>"
    )
    doc = html_to_document(html, url="https://notes.example.com/x")
    assert doc.title == "Harbour notes"
    assert not any(isinstance(b, Heading) and b.text == "Harbour notes" for b in doc.blocks)


@pytest.mark.parametrize(
    "html_lang, hint, expected",
    [('<html lang="de-AT">', "", "de"), ("<html>", "fr-FR", "fr"), ('<html lang="pt_BR">', "", "pt")],
)
def test_declared_language(html_lang, hint, expected):
    html = (
        f"{html_lang}<head><title>T</title></head><body><article>"
        + "<p>The ship came in.</p>" * 30
        + "</article></body></html>"
    )
    assert html_to_document(html, url="https://x.example/a", lang_hint=hint).lang == expected


# --------------------------------------------------------------------------------------
# url_to_document over a local server
# --------------------------------------------------------------------------------------


def make_app() -> web.Application:
    app = web.Application()

    def html_route(name, status=200):
        async def handler(request):
            return web.Response(status=status, body=(PAGES / name).read_bytes(), content_type="text/html")

        return handler

    async def text(request):
        body = "Packing list\n\n- rope\n- lamp\n- bread\n".encode("koi8-r")
        return web.Response(body=body, headers={"Content-Type": "text/plain; charset=koi8-r"})

    async def pdf(request):
        return web.Response(body=b"%PDF-1.4\nfake", content_type="application/pdf")

    async def epub(request):
        import io
        import zipfile

        bio = io.BytesIO()
        with zipfile.ZipFile(bio, "w") as z:
            info = zipfile.ZipInfo("mimetype")
            z.writestr(info, b"application/epub+zip")
        return web.Response(body=bio.getvalue(), content_type="application/epub+zip")

    async def image(request):
        return web.Response(body=b"\x89PNG\r\n\x1a\n", content_type="image/png")

    async def audio(request):
        return web.Response(body=b"ID3", content_type="audio/mpeg")

    async def json_route(request):
        return web.json_response({"a": 1})

    async def koi8(request):
        body = "".join(f"<p>{p}</p>" for p in RU_PARAS * 3)
        html = f"<html><head><title>Каштанка</title></head><body><article>{body}</article></body></html>"
        return web.Response(body=html.encode("koi8-r"), headers={"Content-Type": "text/html; charset=koi8-r"})

    async def moved(request):
        raise web.HTTPFound("/article?utm_source=share")

    async def slow(request):
        await asyncio.sleep(5)
        return web.Response(text="late")

    async def empty_text(request):
        return web.Response(body=b"  \n", content_type="text/plain")

    routes = {
        "/article": html_route("article.html"),
        "/paywall": html_route("paywall.html"),
        "/js": html_route("js_only.html"),
        "/blocked": html_route("cloudflare_block.html", 403),
        "/challenge": html_route("cloudflare_challenge.html", 503),
        "/missing": html_route("js_only.html", 404),
        "/notes.txt": text,
        "/empty.txt": empty_text,
        "/paper.pdf": pdf,
        "/book.epub": epub,
        "/photo.png": image,
        "/song.mp3": audio,
        "/data.json": json_route,
        "/koi8": koi8,
        "/moved": moved,
        "/slow": slow,
    }
    for path, handler in routes.items():
        app.router.add_get(path, handler)
    return app


@pytest.fixture
async def site(aiohttp_server):
    srv = await aiohttp_server(make_app(), host="127.0.0.1")
    return lambda path: str(srv.make_url(path))


@pytest.fixture
def fake_pdf(monkeypatch):
    calls = []
    mod = types.ModuleType("sendtox3.extract.pdf")

    async def pdf_to_document_async(data: bytes, *, title_hint: str = "", source_url: str = "", **_kw) -> Document:
        calls.append((data, title_hint, source_url))
        return Document(title=title_hint or "PDF", blocks=[Paragraph("pdf text")], source_url=source_url)

    mod.pdf_to_document_async = pdf_to_document_async
    monkeypatch.setitem(sys.modules, "sendtox3.extract.pdf", mod)
    return calls


async def test_url_article(site, opts):
    doc = await url_to_document(site("/moved"), opts=opts)
    assert doc.title == "Lighthouse keepers return to Gull Rock after forty years"
    assert doc.source_url == site("/article")  # final URL, tracking parameter removed
    assert doc.word_count() > 300 and doc.notes == []


async def test_url_extraction_runs_off_the_event_loop(site, opts, monkeypatch):
    import threading

    from sendtox3.extract import html as html_mod

    seen = []
    real = html_mod.html_to_document

    def spy(*a, **kw):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*a, **kw)

    monkeypatch.setattr(html_mod, "html_to_document", spy)
    await url_to_document(site("/article"), opts=opts)
    assert seen == [False]


async def test_url_plain_text(site, opts):
    doc = await url_to_document(site("/notes.txt"), opts=opts)
    assert doc.title == "Packing list"
    assert [b.text for b in doc.blocks] == ["rope", "lamp", "bread"]


async def test_url_empty_text_file(site, opts):
    doc = await url_to_document(site("/empty.txt"), opts=opts)
    assert doc.blocks == [] and doc.notes == ["The linked text file is empty."]
    assert doc.title == "empty"


async def test_url_pdf_goes_to_pdf_extractor(site, opts, fake_pdf):
    doc = await url_to_document(site("/paper.pdf"), opts=opts, title_hint="Attention paper")
    assert fake_pdf == [(b"%PDF-1.4\nfake", "Attention paper", site("/paper.pdf"))]
    assert doc.title == "Attention paper" and doc.blocks == [Paragraph("pdf text")]


async def test_url_pdf_disabled(site, opts, fake_pdf):
    doc = await url_to_document(site("/paper.pdf"), opts=dataclasses.replace(opts, pdf_enabled=False))
    assert fake_pdf == [] and doc.blocks == [] and "turned off" in doc.notes[0]


async def test_url_pdf_module_missing(site, opts, monkeypatch):
    monkeypatch.setitem(sys.modules, "sendtox3.extract.pdf", None)
    doc = await url_to_document(site("/paper.pdf"), opts=opts)
    assert doc.blocks == [] and "not available" in doc.notes[0]
    assert doc.title == "paper" and doc.source_url == site("/paper.pdf")


async def test_url_pdf_extractor_failure(site, opts, monkeypatch):
    mod = types.ModuleType("sendtox3.extract.pdf")

    async def boom(data, *, title_hint="", source_url="", **_kw):
        raise ValueError("broken xref")

    mod.pdf_to_document_async = boom
    monkeypatch.setitem(sys.modules, "sendtox3.extract.pdf", mod)
    doc = await url_to_document(site("/paper.pdf"), opts=opts)
    assert doc.blocks == [] and "could not be read" in doc.notes[0]


@pytest.mark.parametrize(
    "path, phrase",
    [
        ("/book.epub", "EPUB book"),
        ("/photo.png", "an image"),
        ("/song.mp3", "an audio or video file"),
        ("/data.json", "application/json"),
    ],
)
async def test_url_non_text_content(site, opts, path, phrase):
    doc = await url_to_document(site(path), opts=opts)
    assert doc.blocks == [] and phrase in doc.notes[0]
    assert doc.source_url == site(path) and doc.title


async def test_url_blocked_pages(site, opts):
    doc = await url_to_document(site("/blocked"), opts=opts, title_hint="Shared title")
    assert doc.blocks == [] and doc.title == "Shared title"
    assert "blocked the download (Cloudflare block)" in doc.notes[0] and "Nothing was bypassed" in doc.notes[0]
    doc = await url_to_document(site("/challenge"), opts=opts)
    assert "Cloudflare challenge" in doc.notes[0]


async def test_url_http_error(site, opts):
    doc = await url_to_document(site("/missing"), opts=opts)
    assert doc.blocks == [] and doc.notes[0].startswith("The page was not found (HTTP 404)")


async def test_url_paywall_and_js_only(site, opts):
    pay = await url_to_document(site("/paywall"), opts=opts)
    assert any("subscriber-only" in n for n in pay.notes) and pay.word_count() > 10
    js = await url_to_document(site("/js"), opts=opts)
    assert any("JavaScript" in n for n in js.notes)


async def test_url_header_charset(site, opts):
    doc = await url_to_document(site("/koi8"), opts=opts)
    assert doc.title == "Каштанка" and doc.lang == "ru" and RU_PARAS[1] in all_text(doc)


async def test_url_unreachable(opts):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    doc = await url_to_document(f"http://127.0.0.1:{port}/story", opts=opts, title_hint="My story")
    assert doc.blocks == [] and doc.title == "My story"
    assert doc.notes[0].startswith("Could not download the link: Could not connect")


async def test_url_private_address_refused_by_default(opts):
    doc = await url_to_document("http://192.168.1.10/admin", opts=dataclasses.replace(opts, allow_private_urls=False))
    assert doc.blocks == [] and "private network address" in doc.notes[0]


async def test_url_timeout_budget(site, opts):
    t0 = time.monotonic()
    doc = await url_to_document(site("/slow"), opts=dataclasses.replace(opts, fetch_timeout_seconds=1))
    assert time.monotonic() - t0 < 3
    assert doc.blocks == [] and "did not answer within 1 seconds" in doc.notes[0]


@pytest.mark.parametrize("bad", ["ftp://example.com/file.txt", "mailto:a@example.com", "javascript:alert(1)"])
async def test_url_non_http(opts, bad):
    doc = await url_to_document(bad, opts=opts)
    assert doc.blocks == [] and doc.notes == ["Only http and https links can be fetched."]
