"""Site APIs (GitHub README, Wikipedia, Substack, Bluesky, X) with a fake fetcher."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from sendtox3.extract import fetch as fetch_mod
from sendtox3.extract import sites
from sendtox3.extract.fetch import FetchError, FetchResult
from sendtox3.extract.html import url_to_document
from sendtox3.extract.sites import match_site, site_document, wiki_extract_to_blocks
from sendtox3.textmodel import Heading, ListItem, Paragraph, Preformatted, Quote

API = Path(__file__).parent / "fixtures" / "pages" / "api"
PAGES = API.parent


class FakeFetch:
    """Routes URLs (by substring) to canned responses and records every call."""

    def __init__(self, routes: dict[str, tuple[int, str, bytes] | Exception]):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def __call__(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResult:
        self.calls.append((url, headers))
        for needle, answer in self.routes.items():
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                status, ctype, body = answer
                return FetchResult(
                    url=url,
                    status=status,
                    content_type=ctype,
                    charset="utf-8",
                    headers={},
                    body=body,
                    blocked=f"HTTP {status}" if status >= 400 else None,
                )
        return FetchResult(url=url, status=404, content_type="text/plain", charset=None, body=b"", blocked="HTTP 404")


def ok(name: str, ctype: str = "application/json") -> tuple[int, str, bytes]:
    return (200, ctype, (API / name).read_bytes())


# --------------------------------------------------------------------------------------
# URL matching
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, site, params",
    [
        ("https://github.com/acme/tidewatch", "github", {"owner": "acme", "repo": "tidewatch", "kind": "readme"}),
        (
            "https://www.github.com/acme/tidewatch.git",
            "github",
            {"owner": "acme", "repo": "tidewatch", "kind": "readme"},
        ),
        ("https://github.com/acme/tidewatch/tree/v2/docs", "github", {"ref": "v2", "dir": "docs", "kind": "readme"}),
        (
            "https://github.com/acme/tidewatch/blob/main/docs/GUIDE.md",
            "github",
            {"kind": "file", "ref": "main", "path": "docs/GUIDE.md"},
        ),
        (
            "https://en.wikipedia.org/wiki/Electronic_paper#History",
            "wikipedia",
            {"lang": "en", "title": "Electronic paper"},
        ),
        ("https://en.m.wikipedia.org/wiki/E-paper", "wikipedia", {"lang": "en", "title": "E-paper"}),
        (
            "https://de.wikipedia.org/w/index.php?title=Glasharmonika",
            "wikipedia",
            {"lang": "de", "title": "Glasharmonika"},
        ),
        (
            "https://fr.wikipedia.org/wiki/%C3%89cran_%C3%A0_encre",
            "wikipedia",
            {"lang": "fr", "title": "Écran à encre"},
        ),
        (
            "https://rooftophive.substack.com/p/notes",
            "substack",
            {"base": "https://rooftophive.substack.com", "slug": "notes"},
        ),
        (
            "https://open.substack.com/pub/rooftophive/p/notes",
            "substack",
            {"base": "https://rooftophive.substack.com", "slug": "notes"},
        ),
        (
            "https://bsky.app/profile/tide.example.social/post/3abc",
            "bluesky",
            {"actor": "tide.example.social", "rkey": "3abc"},
        ),
        ("https://bsky.app/profile/did:plc:abc123/post/3abc", "bluesky", {"actor": "did:plc:abc123", "rkey": "3abc"}),
        (
            "https://x.com/tidewatcher/status/1840000000000000001",
            "x",
            {"user": "tidewatcher", "id": "1840000000000000001"},
        ),
        ("https://twitter.com/jack/status/20?s=20", "x", {"user": "jack", "id": "20"}),
        ("https://mobile.twitter.com/i/web/status/12345", "x", {"user": "i", "id": "12345"}),
        ("https://fxtwitter.com/a_b/status/99/photo/1", "x", {"user": "a_b", "id": "99"}),
    ],
)
def test_match_site(url, site, params):
    m = match_site(url)
    assert m is not None and m.site == site
    for k, v in params.items():
        assert m.params[k] == v


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme",
        "https://github.com/acme/tidewatch/issues/3",
        "https://github.com/acme/tidewatch/blob/main/src/main.py",
        "https://github.com/settings/profile",
        "https://en.wikipedia.org/wiki/Special:Random",
        "https://en.wikipedia.org/wiki/Talk:Electronic_paper",
        "https://en.wikipedia.org/w/index.php?title=E-paper&action=history",
        "https://en.wikipedia.org/",
        "https://rooftophive.substack.com/archive",
        "https://bsky.app/profile/tide.example.social",
        "https://x.com/tidewatcher",
        "https://example.com/p/notes",
        "ftp://github.com/a/b",
        "not a url",
    ],
)
def test_unhandled_urls(url):
    assert match_site(url) is None


async def test_site_document_none_for_other_sites():
    fetch = FakeFetch({})
    assert await site_document("https://example.com/a", fetch=fetch) is None
    assert fetch.calls == []


# --------------------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------------------


async def test_github_readme():
    fetch = FakeFetch({"api.github.com/repos/acme/tidewatch/readme": ok("github_readme.html", "text/html")})
    doc = await site_document("https://github.com/acme/tidewatch", fetch=fetch)
    url, headers = fetch.calls[0]
    assert url == "https://api.github.com/repos/acme/tidewatch/readme"
    assert headers["Accept"] == "application/vnd.github.html+json"
    assert doc.title == "Tidewatch" and doc.byline == "acme" and doc.site == "GitHub"
    assert doc.source_url == "https://github.com/acme/tidewatch"
    heads = [(b.text, b.level) for b in doc.blocks if isinstance(b, Heading)]
    assert heads == [("Installation", 2), ("Configuration", 2), ("Supported harbours", 3)]
    code = [b.text for b in doc.blocks if isinstance(b, Preformatted)]
    assert code == [
        'pip install tidewatch\ntidewatch --harbour "Gull Rock"',
        '[harbour]\nname = "Gull Rock"\n    offset_minutes = 12',
    ]
    text = "\n".join(getattr(b, "text", "") for b in doc.blocks)
    assert "Permalink" not in text and "Copied" not in text and "CI status" not in text
    assert Paragraph("Settings live in ~/.config/tidewatch.toml:") in doc.blocks
    assert Paragraph("Option | Meaning") in doc.blocks and Paragraph("offset_minutes | Local correction") in doc.blocks
    assert [(b.text, b.depth) for b in doc.blocks if isinstance(b, ListItem)] == [
        ("Gull Rock", 0), ("Saltmarsh Quay", 0), ("inner basin", 1), ("outer basin", 1),
    ]  # fmt: skip
    assert Paragraph("Version 0.2 added the offset option.") in doc.blocks


@pytest.mark.parametrize(
    "url, api",
    [
        (
            "https://github.com/acme/tidewatch/tree/v2/docs",
            "https://api.github.com/repos/acme/tidewatch/readme/docs?ref=v2",
        ),
        (
            "https://github.com/acme/tidewatch/blob/main/docs/GUIDE.md",
            "https://api.github.com/repos/acme/tidewatch/contents/docs/GUIDE.md?ref=main",
        ),
    ],
)
async def test_github_api_urls(url, api):
    fetch = FakeFetch({"api.github.com": ok("github_readme.html", "text/html")})
    doc = await site_document(url, fetch=fetch)
    assert fetch.calls[0][0] == api and doc is not None


@pytest.mark.parametrize(
    "answer",
    [
        (404, "application/json", b'{"message":"Not Found"}'),
        (403, "application/json", b'{"message":"API rate limit exceeded"}'),
        (200, "application/json", b"{}"),
    ],
)
async def test_github_api_unusable(answer):
    fetch = FakeFetch({"api.github.com": answer})
    assert await site_document("https://github.com/acme/tidewatch", fetch=fetch) is None


# --------------------------------------------------------------------------------------
# Wikipedia
# --------------------------------------------------------------------------------------


async def test_wikipedia_extract():
    fetch = FakeFetch({"de.wikipedia.org/w/api.php": ok("wikipedia_extract.json")})
    doc = await site_document("https://de.wikipedia.org/wiki/Glasharmonika_Stimmung", fetch=fetch)
    q = parse_qs(urlsplit(fetch.calls[0][0]).query)
    assert q["titles"] == ["Glasharmonika Stimmung"] and q["explaintext"] == ["1"] and q["redirects"] == ["1"]
    assert doc.title == "Glasharmonika-Stimmung"
    assert doc.site == "Wikipedia" and doc.byline == "" and doc.lang == "de"
    assert doc.source_url == "https://de.wikipedia.org/wiki/Glasharmonika-Stimmung"
    heads = [(b.text, b.level) for b in doc.blocks if isinstance(b, Heading)]
    assert heads == [("Geschichte", 2), ("Werkstattpraxis", 3)]  # empty + reference sections dropped
    text = "\n".join(b.text for b in doc.blocks)
    assert "displaystyle" not in text and "Stimmgabel\n" not in text and "Offizielle Seite" not in text
    assert "Die Tonhöhe folgt ungefähr der Formel f∝1/r² für den Radius der Schale." in text
    assert Paragraph("Rand schleifen, wenn die Schale zu tief klingt.") in doc.blocks


async def test_wikipedia_missing_page():
    body = json.dumps({"query": {"pages": [{"ns": 0, "title": "Nope", "missing": True}]}}).encode()
    fetch = FakeFetch({"w/api.php": (200, "application/json", body)})
    assert await site_document("https://en.wikipedia.org/wiki/Nope", fetch=fetch) is None


# TextExtracts spreads each formula over indented one-token lines, then the TeX.
MATH_EXTRACT = (
    "The tidal period is \n  \n    \n      \n        T\n        =\n        12.42\n      \n    \n"
    "    {\\displaystyle T=12.42\\,\\mathrm {h} }\n  \n for a semidiurnal tide.\n"
    "The height follows\n  \n    \n      \n        h\n        (\n        t\n        )\n      \n    \n"
    "    {\\displaystyle h(t)=A\\cos \\left({\\frac {2\\pi t}{T}}\\right)}\n  \n\n"
    "Next paragraph."
)


def test_wiki_extract_math_is_collapsed_into_the_sentence():
    assert wiki_extract_to_blocks(MATH_EXTRACT) == [
        Paragraph("The tidal period is T=12.42 h for a semidiurnal tide."),
        Paragraph("The height follows h(t)=Acos (2πt/T)"),
        Paragraph("Next paragraph."),
    ]


@pytest.mark.parametrize(
    "tex, text",
    [
        (r"E=mc^{2}.", "E=mc²."),
        (r"\Delta E\approx 10^{-3}", "ΔE≈10⁻³"),
        (r"H_{2}O", "H₂O"),
        (r"x^{10}", "x^(10)"),  # the device font has no superscript zero
        (r"\frac{1}{2}", "1/2"),
        (r"\left( \frac{a+b}{2} \right)", "((a+b)/2)"),
        (r"\sqrt{1-v^{2}/c^{2}}", "sqrt(1-v²/c²)"),
        (r"\sin \theta + \cos^{2} x", "sin θ+cos²x"),
        (r"x\in \mathbb{R}\text{ for all }t", "x∈R for all t"),
        (r"\mathbf {p}", "p"),
        (r"a\,b", "a b"),
        (r"\unknowncommand{z}", "unknowncommandz"),
        ("", ""),
    ],
)
def test_tex_to_text(tex, text):
    assert sites.tex_to_text(tex) == text


def test_wiki_extract_parent_of_only_empty_sections_is_dropped():
    blocks = wiki_extract_to_blocks("Intro.\n== Parent ==\n=== Empty child ===\n== Next ==\nText.")
    assert blocks == [Paragraph("Intro."), Heading("Next", 2), Paragraph("Text.")]


def test_wiki_extract_several_inline_formulas_on_one_line():
    blocks = wiki_extract_to_blocks("a {\\displaystyle x^{2}} b {\\displaystyle y_{1}} c")
    assert blocks == [Paragraph("a x² b y₁ c")]


def test_wiki_extract_unbalanced_tex_does_not_eat_the_article():
    blocks = wiki_extract_to_blocks("Before {\\displaystyle x^{2} after.\nNext paragraph.")
    assert blocks == [Paragraph("Before x^{2} after."), Paragraph("Next paragraph.")]


def test_wiki_extract_to_blocks_nested_dropped_sections():
    text = "Intro.\n== See also ==\n=== Sub of see also ===\nx\n== Kept ==\nBody {\\displaystyle a_{b}} end.\n== References =="
    blocks = wiki_extract_to_blocks(text)
    assert blocks == [Paragraph("Intro."), Heading("Kept", 2), Paragraph("Body a_b end.")]


# --------------------------------------------------------------------------------------
# Substack
# --------------------------------------------------------------------------------------


async def test_substack_post():
    fetch = FakeFetch({"rooftophive.substack.com/api/v1/posts/notes-on-keeping-bees": ok("substack_post.json")})
    doc = await site_document("https://open.substack.com/pub/rooftophive/p/notes-on-keeping-bees", fetch=fetch)
    assert doc.title == "Notes on keeping bees in a city flat"
    assert doc.byline == "Juniper Ashgrove" and doc.site == "The Rooftop Hive"
    assert doc.published == "2026-08-02" and doc.lang == "en" and doc.notes == []
    assert doc.source_url == "https://rooftophive.example/p/notes-on-keeping-bees"
    assert doc.blocks[0] == Paragraph("What two summers on a fifth-floor balcony taught me")
    assert doc.blocks[1].text.endswith(
        "assumed I was joking.[1] Two summers later the hive has produced eleven jars of honey."
    )
    assert [b.text for b in doc.blocks if isinstance(b, Heading)] == ["Choosing the hive", "Water and neighbours"]
    text = "\n".join(getattr(b, "text", "") for b in doc.blocks)
    assert "Thanks for reading" not in text and "first spring" not in text and "Subscribe" not in text
    assert Quote("Water close to the hive is the best neighbourly gesture.") in doc.blocks
    assert doc.blocks[-1] == Paragraph("[1] One neighbour still thinks it is a joke.")


async def test_substack_paid_post_note_and_fallback_text():
    data = json.loads((API / "substack_post.json").read_text())
    data.update(audience="only_paid", body_html=None, truncated_body_text="The first lines of a paid post.")
    fetch = FakeFetch({"api/v1/posts": (200, "application/json", json.dumps(data).encode())})
    doc = await site_document("https://rooftophive.substack.com/p/notes-on-keeping-bees", fetch=fetch)
    assert doc.notes == ["Subscriber-only post: only the free preview could be read."]
    assert doc.blocks[-1] == Paragraph("The first lines of a paid post.")


# --------------------------------------------------------------------------------------
# Bluesky
# --------------------------------------------------------------------------------------


async def test_bluesky_thread():
    fetch = FakeFetch({"resolveHandle": ok("bsky_resolve.json"), "getPostThread": ok("bsky_thread.json")})
    doc = await site_document("https://bsky.app/profile/tidewatch.example.social/post/3abcroot0001", fetch=fetch)
    assert "handle=tidewatch.example.social" in fetch.calls[0][0]
    uri = parse_qs(urlsplit(fetch.calls[1][0]).query)["uri"][0]
    assert uri == "at://did:plc:testtidewatch000000000001/app.bsky.feed.post/3abcroot0001"
    assert doc.byline == "Tide Watcher (@tidewatch.example.social)"  # emoji stripped from the name
    assert doc.site == "Bluesky" and doc.published == "2026-10-20" and doc.lang == "en"
    # first line is 87 characters: cut to the 80-character title limit at a word boundary
    assert doc.title == "Spring tide this morning at Gull Rock: the water came right up to the old…"
    assert [type(b).__name__ for b in doc.blocks] == ["Paragraph"] * 3 + ["Quote"] + ["Paragraph"] * 3
    assert doc.blocks[1] == Paragraph("A short thread on why that happens")
    assert doc.blocks[2] == Paragraph("[Image: Waves reaching the bottom step of a stone stair]")
    assert doc.blocks[3] == Quote("Harbour Master: Highest tide of the year expected on Monday.")
    assert doc.blocks[4].text.startswith("Spring tides happen when the sun and moon line up")
    assert doc.blocks[5] == Paragraph("[Link: How tides work]")
    assert doc.blocks[6] == Paragraph("Add an onshore wind and the steps get wet. End of thread.")
    assert "Great photo" not in "\n".join(b.text for b in doc.blocks)  # other people's replies excluded


async def test_bluesky_did_needs_no_resolution_and_missing_post():
    fetch = FakeFetch(
        {
            "getPostThread": (
                200,
                "application/json",
                b'{"thread":{"$type":"app.bsky.feed.defs#notFoundPost","uri":"at://x","notFound":true}}',
            )
        }
    )
    assert await site_document("https://bsky.app/profile/did:plc:abc/post/3x", fetch=fetch) is None
    assert len(fetch.calls) == 1 and "getPostThread" in fetch.calls[0][0]


# --------------------------------------------------------------------------------------
# X / Twitter
# --------------------------------------------------------------------------------------


async def test_x_oembed():
    fetch = FakeFetch({"publish.twitter.com/oembed": ok("x_oembed.json")})
    doc = await site_document("https://twitter.com/tidewatcher/status/1840000000000000001?s=20", fetch=fetch)
    q = parse_qs(urlsplit(fetch.calls[0][0]).query)
    assert unquote(q["url"][0]) == "https://x.com/tidewatcher/status/1840000000000000001" and q["omit_script"] == ["1"]
    assert doc.blocks == [
        Paragraph("High water at Gull Rock today was the highest in ten years."),
        Paragraph("The keepers' logbook from 1986 records almost the same level."),
    ]
    assert doc.byline == "Tide Watcher (@tidewatcher)" and doc.site == "X"
    assert doc.published == "2024-09-28" and doc.lang == "en"
    assert doc.title == "High water at Gull Rock today was the highest in ten years."


async def test_x_deleted_post():
    fetch = FakeFetch({"oembed": (404, "application/json", b'{"error":"not found"}')})
    assert await site_document("https://x.com/a/status/1", fetch=fetch) is None


# --------------------------------------------------------------------------------------
# Failure handling and integration with url_to_document
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [FetchError("The site did not answer within 5 seconds."), ValueError("bad json")])
async def test_api_errors_return_none(exc):
    fetch = FakeFetch({"api.github.com": exc})
    assert await site_document("https://github.com/acme/tidewatch", fetch=fetch) is None


async def test_api_bad_json_returns_none():
    fetch = FakeFetch({"w/api.php": (200, "application/json", b"<html>not json</html>")})
    assert await site_document("https://en.wikipedia.org/wiki/X", fetch=fetch) is None


@pytest.fixture
def routed_fetch(monkeypatch):
    """Replace the real fetcher used by url_to_document with canned answers."""

    def install(routes):
        fake = FakeFetch(routes)

        async def fetch_url(
            url, *, timeout=20, max_bytes=8_000_000, allow_private=False, headers=None, max_pdf_bytes=None
        ):
            return await fake(url, headers=headers)

        monkeypatch.setattr(fetch_mod, "fetch_url", fetch_url)
        return fake

    return install


async def test_url_to_document_prefers_site_api(routed_fetch, opts):
    fake = routed_fetch({"w/api.php": ok("wikipedia_extract.json")})
    doc = await url_to_document("https://de.wikipedia.org/wiki/Glasharmonika_Stimmung?utm_source=x", opts=opts)
    assert doc.site == "Wikipedia" and doc.title == "Glasharmonika-Stimmung"
    assert len(fake.calls) == 1 and "api.php" in fake.calls[0][0]


async def test_url_to_document_falls_back_to_the_page(routed_fetch, opts):
    page = (PAGES / "wikipedia_like.html").read_bytes()
    fake = routed_fetch(
        {"w/api.php": (503, "text/html", b"busy"), "/wiki/Glass_harmonica_tuning": (200, "text/html", page)}
    )
    doc = await url_to_document("https://en.wikipedia.org/wiki/Glass_harmonica_tuning", opts=opts)
    assert [c[0].split("?")[0] for c in fake.calls] == [
        "https://en.wikipedia.org/w/api.php",
        "https://en.wikipedia.org/wiki/Glass_harmonica_tuning",
    ]
    assert doc.title == "Glass harmonica tuning" and doc.site == "Wikipedia" and doc.word_count() > 100


async def test_url_to_document_js_only_page_when_api_fails(routed_fetch, opts):
    shell = (PAGES / "js_only.html").read_bytes()
    routed_fetch({"oembed": (404, "application/json", b"{}"), "x.com/a/status/1": (200, "text/html", shell)})
    doc = await url_to_document("https://x.com/a/status/1", opts=opts, title_hint="A post")
    assert any("JavaScript" in n for n in doc.notes)
    assert doc.source_url == "https://x.com/a/status/1"


def test_handlers_cover_every_site():
    assert set(sites._HANDLERS) == {"github", "wikipedia", "substack", "bluesky", "x"}
