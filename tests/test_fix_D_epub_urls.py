"""Long URLs in the EPUB body (render/epub.py), review finding 19.

CrossPoint offers only the hyphen positions as break points in a word that contains
a hyphen between letters (Hyphenator.cpp:194-221), so a URL slug whose first
hyphen-free stretch is wider than the line runs past the margin. The EPUB body shows
such links as ``[host]``; the TXT rendition keeps them in full.
"""

from __future__ import annotations

import io
import re
import zipfile

from sendtox3.render import epub
from sendtox3.render.epub import render_epub
from sendtox3.render.txt import render_txt
from sendtox3.textmodel import Document, Heading, ListItem, Paragraph, Quote

GUARDIAN = "https://www.theguardian.com/world/2026/oct/25/why-e-ink-readers-are-back?utm_source=x"
SOURCE = "https://news.example.org/2026/10/25/the-long-slug-of-this-story"


def _doc(blocks, **kw) -> Document:
    base = dict(title="Gardens", byline="", site="", source_url=SOURCE, published="2026-10-25", lang="en",
                blocks=blocks)
    base.update(kw)
    return Document(**base)


def _body(doc: Document) -> str:
    z = zipfile.ZipFile(io.BytesIO(render_epub([doc])))
    name = next(n for n in z.namelist() if n.startswith("OEBPS/text/"))
    return z.read(name).decode("utf-8").split("<body>")[1]


def _paragraphs(body: str) -> list[str]:
    """Body paragraphs, without the italic meta, note and Source lines."""
    return [p for p in re.findall(r"<p>(.*?)</p>", body) if not p.startswith("<i>")]


def test_inline_long_url_becomes_its_host():
    body = _body(_doc([Paragraph(f"Sponsored by Acme: {GUARDIAN}. Read it.")]))
    assert "<p>Sponsored by Acme: [theguardian.com]. Read it.</p>" in body
    assert "https://" not in body and "theguardian.com/world" not in body


def test_txt_keeps_the_full_url():
    txt = render_txt(_doc([Paragraph(f"Sponsored by Acme: {GUARDIAN}. Read it.")])).decode("utf-8")
    assert GUARDIAN in txt


def test_short_links_are_kept():
    short = "https://x.io/a"
    assert len(short) <= getattr(epub, "MAX_INLINE_URL_CHARS", 30)
    body = _body(_doc([Paragraph(f"See {short} for more.")]))
    assert f"<p>See {short} for more.</p>" in body


def test_lists_quotes_headings_and_notes():
    url = "https://lists.example.com/some-long/path-with-hyphens-in-every-segment"
    body = _body(_doc([Heading(f"Links from {url}", 2), ListItem(f"one {url}", False, 1), Quote(f"q {url}")],
                      notes=[f"Comment: look at {url}"]))
    assert "https://" not in body
    assert "<h2>Links from [lists.example.com]</h2>" in body
    assert "<li>one [lists.example.com]" in body
    assert "<p>q [lists.example.com]</p>" in body
    assert "<p><i>Note: Comment: look at [lists.example.com]</i></p>" in body


def test_punctuation_parentheses_and_www_links():
    body = _body(_doc([
        Paragraph("(see https://example.com/some/long/path-name-here-for-real)"),
        Paragraph("Wiki: https://en.wikipedia.org/wiki/E_Ink_(company), then more."),
        Paragraph("Or www.example.net/a-very-long-path-with-hyphens-in-it!"),
        Paragraph("Quoted “https://quotes.example.com/a-long-and-hyphenated-slug” here."),
    ]))
    assert "<p>(see [example.com])</p>" in body
    assert "<p>Wiki: [en.wikipedia.org], then more.</p>" in body
    assert "<p>Or [example.net]!</p>" in body
    assert "<p>Quoted “[quotes.example.com]” here.</p>" in body


def test_paragraph_that_is_only_the_source_url_is_dropped():
    body = _body(_doc([Paragraph("A note about this story."), Paragraph(SOURCE + "/?utm_medium=share"),
                       Paragraph(GUARDIAN)]))
    paras = _paragraphs(body)
    assert "A note about this story." in paras
    assert "[theguardian.com]" in paras  # another page: kept, shortened
    assert not any("news.example.org" in p for p in paras)
    assert "<p><i>Source: news.example.org</i></p>" in body  # the source is still named once


def test_source_url_mention_inside_a_sentence_is_kept():
    body = _body(_doc([Paragraph(f"Original at {SOURCE} today.")]))
    assert "<p>Original at [news.example.org] today.</p>" in body
