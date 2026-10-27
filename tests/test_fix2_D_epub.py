"""EPUB body links and empty bodies (render/epub.py), review round 2: R2-D1..R2-D3.

- R2-D1: a paragraph that is only a link to ANOTHER page of the same host and path
  (``item?id=2``, ``watch?v=...``) is kept; only the document's own URL is dropped,
  whatever its scheme, ``www.``, trailing slash, tracking parameters or fragment.
- R2-D2: text attached right after a link (``'s``, an em dash, an ellipsis, CJK
  punctuation) stays in the text when the link becomes ``[host]``.
- R2-D3: a chapter with nothing left to read says so in one paragraph, and the body
  never has two ``<hr/>`` in a row.
"""

from __future__ import annotations

import io
import re
import sys
import zipfile

import pytest

from sendtox3.render import epub
from sendtox3.render.epub import _body_text, _is_source_link, render_epub
from sendtox3.textmodel import Document, Heading, Paragraph, Preformatted, Rule

LONG = "https://example.com/a-very-long-path-with-hyphens-in-every-segment"


def _doc(blocks, **kw) -> Document:
    base = dict(title="Story", byline="", site="", source_url="", published="", lang="en", blocks=blocks)
    base.update(kw)
    return Document(**base)


def _chapters(doc: Document) -> list[str]:
    z = zipfile.ZipFile(io.BytesIO(render_epub([doc])))
    return [z.read(n).decode("utf-8").split("<body>")[1].split("</body>")[0].strip()
            for n in z.namelist() if n.startswith("OEBPS/text/")]  # spine order


def _body(doc: Document) -> str:
    (body,) = _chapters(doc)
    return body


def _text_paras(body: str) -> list[str]:
    """Body paragraphs without the italic meta and Source lines."""
    return [p for p in re.findall(r"<p>(.*?)</p>", body) if not p.startswith("<i>")]


def _tags(body: str) -> list[str]:
    return re.findall(r"<(h1|h2|h3|p|hr|ul|ol|blockquote)\b", body)


# ---------------------------------------------------------------------------------------
# R2-D1: only the document's own URL is dropped
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("source, other", [
    ("https://news.ycombinator.com/item?id=1", "https://news.ycombinator.com/item?id=2"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=9bZkp7q19f0"),
    ("https://example.com/search?q=e-ink", "https://example.com/search?q=e-ink&page=2"),
    ("https://example.com/", "https://example.com/#/articles/42"),  # a hash route is another page
    ("https://example.com/", "https://example.com/?p=42"),
])
def test_link_to_another_page_of_the_same_path_is_kept(source, other):
    assert not _is_source_link(other, source)
    body = _body(_doc([Paragraph("Intro."), Paragraph(other)], source_url=source))
    shown = other if len(other) <= epub.MAX_INLINE_URL_CHARS else f"[{epub.host_of(other)}]"
    assert _text_paras(body) == ["Intro.", shown]
    assert f"<p><i>Source: {epub.host_of(source)}</i></p>" in body


@pytest.mark.parametrize("link", [
    "https://news.ycombinator.com/item?id=1",
    "http://news.ycombinator.com/item?id=1",  # scheme does not matter
    "https://www.news.ycombinator.com/item/?id=1",  # nor www. or a trailing slash
    "https://news.ycombinator.com/item?id=1&utm_source=share&fbclid=abc",  # nor tracking
    "https://news.ycombinator.com/item?id=1#comments",  # nor an anchor
    "www.news.ycombinator.com/item?id=1",
    "https://news.ycombinator.com/item?id=1.",  # trailing punctuation
])
def test_the_documents_own_url_is_still_dropped(link):
    src = "https://news.ycombinator.com/item?id=1"
    assert _is_source_link(link, src)
    body = _body(_doc([Paragraph("Intro."), Paragraph(link)], source_url=src))
    assert _text_paras(body) == ["Intro."]
    assert body.endswith("<p><i>Source: news.ycombinator.com</i></p>")


def test_query_order_and_youtube_share_parameters_do_not_matter():
    src = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42"
    assert _is_source_link("https://youtube.com/watch?t=42&v=dQw4w9WgXcQ", src)
    assert _is_source_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42&si=Xy12_ab", src)
    assert not _is_source_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=99", src)


def test_source_without_a_query_still_matches_its_tracked_copy():
    src = "https://news.example.org/2026/10/25/the-long-slug-of-this-story"
    assert _is_source_link(src + "/?utm_medium=share", src)
    assert not _is_source_link(src + "?page=2", src)


def test_tracking_rules_hold_without_the_fetcher(monkeypatch):
    # the renderer must not need the fetcher; utm_* still does not make another page
    monkeypatch.setitem(sys.modules, "sendtox3.extract.fetch", None)  # import -> ImportError
    with pytest.raises(ImportError):
        from sendtox3.extract.fetch import clean_url  # noqa: F401
    src = "https://news.ycombinator.com/item?id=1"
    assert _is_source_link("https://news.ycombinator.com/item?id=1&utm_source=x", src)
    assert not _is_source_link("https://news.ycombinator.com/item?id=2", src)


# ---------------------------------------------------------------------------------------
# R2-D2: text attached after a link is kept
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    (f"Read {LONG}'s page", "Read [example.com]'s page"),
    (f"Read {LONG}’s page", "Read [example.com]’s page"),
    (f"It is {LONG}'s.", "It is [example.com]'s."),
    (f"{LONG}'ll do", "[example.com]'ll do"),
    (f"See {LONG}—which is long", "See [example.com]—which is long"),
    (f"See {LONG}–and more", "See [example.com]–and more"),
    (f"See {LONG}… and more", "See [example.com]… and more"),
    (f"See {LONG}»quoted", "See [example.com]»quoted"),
    (f"参见{LONG}。然后", "参见[example.com]。然后"),
    (f"「{LONG}」", "「[example.com]」"),
])
def test_attached_text_survives_shortening(text, expected):
    assert _body_text(text) == expected


@pytest.mark.parametrize("url", [
    "https://en.wikipedia.org/wiki/The_Hitchhiker's_Guide_to_the_Galaxy",  # apostrophe inside the URL
    "https://example.com/people/bob's-page-about-many-things",
    "https://example.com/archive/2026/Bob's.html",
    "https://example.com/wiki/Rock_'n'_roll_and_the_long_title",
])
def test_apostrophes_inside_a_url_stay_in_the_url(url):
    assert _body_text(f"See {url} now") == f"See [{epub.host_of(url)}] now"


def test_attached_text_is_kept_in_the_epub_and_the_short_link_too():
    body = _body(_doc([Paragraph(f"Read {LONG}'s page"), Paragraph("Short https://x.io/a's page")]))
    assert "<p>Read [example.com]&#39;s page</p>" in body
    assert "<p>Short https://x.io/a&#39;s page</p>" in body


def test_source_url_with_attached_text_is_not_a_bare_link():
    src = LONG
    assert not _is_source_link(f"{src}'s page", src)
    body = _body(_doc([Paragraph(f"{src}'s page")], source_url=src))
    assert "<p>[example.com]&#39;s page</p>" in body


# ---------------------------------------------------------------------------------------
# R2-D3: an empty body says so; never two <hr/> in a row
# ---------------------------------------------------------------------------------------


def test_document_that_is_only_its_source_url():
    body = _body(_doc([Paragraph(LONG)], source_url=LONG))
    assert _tags(body) == ["h1", "p", "hr", "p", "hr", "p"]
    paras = re.findall(r"<p>(.*?)</p>", body)
    assert paras[1] == ("There is no text here, only a link to example.com. "
                        "Open it on another device to read it.")
    assert paras[2] == "<i>Source: example.com</i>"
    assert "<hr/>\n<hr/>" not in body


def test_note_only_document_gets_the_same_paragraph():
    body = _body(_doc([], source_url="https://paper.example/story", notes=["The site refused access"]))
    assert "<hr/>\n<hr/>" not in body
    assert "<p>There is no text here, only a link to paper.example. Open it on another device to read it.</p>" \
        in body


def test_empty_document_without_a_link():
    body = _body(_doc([Rule(), Paragraph(""), Heading("", 2)]))
    assert _tags(body) == ["h1", "hr", "p"]
    assert body.endswith("<p>There is no text here.</p>")


def test_rules_never_double_up():
    body = _body(_doc([Rule(), Paragraph("a"), Rule(), Rule(), Paragraph("b"), Rule()],
                      source_url="https://example.com/x"))
    assert "<hr/>\n<hr/>" not in body
    assert _tags(body) == ["h1", "p", "hr", "p", "hr", "p", "hr", "p"]
    assert "There is no text here" not in body


def test_preformatted_counts_as_text():
    body = _body(_doc([Preformatted("x = 1")], source_url="https://example.com/x"))
    assert "There is no text here" not in body and "<p>x = 1</p>" in body


def test_split_document_has_no_empty_message_and_no_double_rules():
    para = "word " * 400
    blocks = [Heading("Part one", 2), *[Paragraph(para) for _ in range(30)], Rule(),
              Heading("Part two", 2), *[Paragraph(para) for _ in range(30)], Rule()]
    chapters = _chapters(_doc(blocks, source_url="https://example.com/x"))
    assert len(chapters) >= 2
    for body in chapters:
        assert "There is no text here" not in body
        assert "<hr/>\n<hr/>" not in body
        assert not body.lstrip().startswith("<hr/>")
    assert chapters[-1].rstrip().endswith("<p><i>Source: example.com</i></p>")
