"""Round 3, item 2: extract/html.py ``clean_title`` removed a leading segment equal to
the host's name, so "Python: the language" fetched from python.org became "the
language" (the book's title and its file name). It now follows the rule of
``ingest.strip_display_affixes``: a leading "Site: " goes only when it is exactly the
site name (og:site_name) and a headline of 4+ words remains; trailing " | Site" /
" - Site" affixes are still stripped as before.
"""

from __future__ import annotations

import pytest

from sendtox3.extract import fetch as fetch_mod
from sendtox3.extract.fetch import FetchResult
from sendtox3.extract.html import clean_title, html_to_document
from sendtox3.ingest import Processor, strip_display_affixes
from sendtox3.models import IngestRequest

ARTICLE = "".join(
    f"<p>Paragraph {i}: Python is a programming language that lets you work quickly and integrate "
    "systems more effectively, and people use it for many kinds of work.</p>"
    for i in range(8)
)


def _page(title: str, site_name: str = "") -> str:
    meta = f'<meta property="og:site_name" content="{site_name}">' if site_name else ""
    return (f"<html><head><title>{title}</title>{meta}</head>"
            f"<body><article><h1>{title}</h1>{ARTICLE}</article></body></html>")


@pytest.mark.parametrize(
    "title, site, host, expected",
    [
        # the verifier's case: the host's name is not the site name
        ("Python: the language", "", "python.org", "Python: the language"),
        ("Python: the language", "Python.org", "python.org", "Python: the language"),
        # the exact site name, but only 3 words would remain
        ("Python: the language", "Python", "python.org", "Python: the language"),
        ("Python: the language for busy people", "Python", "python.org", "the language for busy people"),
        ("Python: the language for busy people", "", "python.org", "Python: the language for busy people"),
        ("Rust | a language empowering everyone", "", "rust-lang.org", "Rust | a language empowering everyone"),
        ("The Example Gazette - Story title", "The Example Gazette", "example-gazette.com", "Story title"),
        ("the example gazette - Story title", "The Example Gazette", "example-gazette.com", "Story title"),
        # trailing affixes: the host's name is still enough
        ("Bun 1.2 | Bun", "", "bun.sh", "Bun 1.2"),
        ("How headphones changed music | Wired", "", "wired.com", "How headphones changed music"),
        ("How headphones changed music - WIRED", "WIRED", "wired.com", "How headphones changed music"),
        ("Battle Drones | Simon Willison’s Weblog", "", "simonwillison.net", "Battle Drones"),
        # GitHub's "GitHub - owner/repo" prefix only on GitHub
        ("GitHub - owner/repo: A tool", "GitHub", "github.com", "owner/repo: A tool"),
        ("GitHub - owner/repo: A tool", "", "github.com", "owner/repo: A tool"),
        ("GitHub - owner/repo: A tool", "", "example.com", "GitHub - owner/repo: A tool"),
        ("Electronic paper - Wikipedia", "", "en.wikipedia.org", "Electronic paper"),
    ],
)
def test_clean_title_leading_site_only_when_exact(title, site, host, expected):
    assert clean_title(title, site=site, host=host) == expected


@pytest.mark.parametrize(
    "title, site, host",
    [
        ("Python: the language", "", "python.org"),
        ("Python: the language", "Python", "python.org"),
        ("Python: the language for busy people", "Python", "python.org"),
        ("Nature: ten people who helped shape science", "Nature", "nature.com"),
        ("Nature’s 10: ten people who helped shape science in 2023", "Nature", "nature.com"),
        ("Science of sleep: why we dream", "", "science.org"),
        ("Wired for sound: how headphones changed music", "WIRED", "wired.com"),
        ("Wired: how headphones changed music", "", "wired.com"),
        ("Astral Codex Ten: Your Book Review", "Astral Codex Ten", "astralcodexten.com"),
    ],
)
def test_leading_colon_rule_matches_strip_display_affixes(title, site, host):
    """The file-safe title (clean_title) and the in-book headline (display_title) keep
    or drop a leading "Site: " alike."""
    assert clean_title(title, site=site, host=host) == strip_display_affixes(title, site, f"https://{host}/x")


@pytest.mark.parametrize("site_name", ["", "Python.org", "Python"])
def test_python_org_page_keeps_its_title(site_name):
    doc = html_to_document(_page("Python: the language", site_name), url="https://www.python.org/about/")
    assert doc.title == "Python: the language"


async def test_python_org_link_keeps_its_title_in_book_and_file_name(store, opts, monkeypatch):
    """End to end through the real url_to_document (only the network is faked) and the
    real Processor: the item title (file name) and the book's headline."""
    url = "https://www.python.org/about/"
    body = _page("Python: the language").encode()

    async def fake_fetch(u, **kw):
        return FetchResult(url=u, status=200, content_type="text/html", charset="utf-8", body=body)

    monkeypatch.setattr(fetch_mod, "fetch_url", fake_fetch)
    item = await Processor(store, opts).process(IngestRequest(url=url))
    assert item.status == "ready", item.error
    assert item.title.startswith("Python") and "the language" in item.title
    txt = store.file_bytes(item.id, "txt").decode("utf-8")
    assert txt.splitlines()[0] == "Python: the language"
