"""Regression tests for review finding 5: quadratic regexes on one long line.

``_ATX_RE`` / ``_WIKI_HEAD_RE`` in extract/text.py (and the same pattern shapes in
derive_title, sites.wiki_extract_to_blocks, URL trimming and de-duplication) took
seconds of GIL time on a single long line, freezing the whole server. The rewritten
helpers must be linear and give exactly the old results.
"""

from __future__ import annotations

import time

import pytest

from sendtox3.extract import sites
from sendtox3.extract.text import (
    classify,
    derive_title,
    find_urls,
    first_url,
    text_to_blocks,
    text_to_document,
)
from sendtox3.textmodel import Heading, Paragraph, Rule

N = 40_000  # the old patterns needed ~11 s for this; the new code needs milliseconds
LIMIT = 0.5


def _timed(fn, *args):
    t0 = time.perf_counter()
    out = fn(*args)
    return out, time.perf_counter() - t0


@pytest.mark.parametrize(
    "line",
    [
        "# a" + " " * N + "b",
        "### a" + " " * N + "b ###",
        "== a" + " " * N + "b",
        "== a" + " " * N + "b ==",
        "a" + " " * N + "== b",
    ],
)
def test_long_line_heading_detection_is_linear(line):
    doc, secs = _timed(text_to_document, line)
    assert secs < LIMIT
    assert doc.word_count() >= 2


def test_long_heading_line_in_wiki_extract_is_linear():
    blocks, secs = _timed(sites.wiki_extract_to_blocks, "Intro line.\n== a" + " " * N + "b\nText.")
    assert secs < LIMIT
    assert any(isinstance(b, Paragraph) for b in blocks)


def test_derive_title_on_a_long_line_is_linear():
    title, secs = _timed(derive_title, "a" + " " * N + "b ==")
    assert secs < LIMIT and title.startswith("a b")


def test_url_followed_by_many_closing_brackets_is_linear():
    urls, secs = _timed(find_urls, "see http://example.org/x" + ")" * 100_000)
    assert secs < LIMIT and urls == ["http://example.org/x"]


def test_many_urls_are_deduplicated_in_linear_time():
    text = " ".join(f"https://example.com/article/{i}" for i in range(30_000)) + " https://example.com/article/7"
    urls, secs = _timed(find_urls, text)
    assert secs < LIMIT and len(urls) == 30_000 and urls[7] == "https://example.com/article/7"
    _, secs = _timed(classify, text)
    assert secs < LIMIT
    first, secs = _timed(first_url, text)
    assert secs < LIMIT and first == "https://example.com/article/0"


def test_malformed_bracket_url_does_not_raise():
    assert find_urls("broken http://[oops/ and https://example.com/ok") == ["https://example.com/ok"]
    assert first_url("broken http://[oops/ link") == ""


# Results recorded from the previous (regex) implementation, edge cases included.
@pytest.mark.parametrize(
    "line, expected",
    [
        ("# Title", [Heading("Title", 1)]),
        ("# Title ##", [Heading("Title", 1)]),
        ("#  Title  #", [Heading("Title", 1)]),
        ("# C#", [Heading("C#", 1)]),
        ("# ##", [Heading("##", 1)]),
        ("## ##", [Heading("##", 2)]),
        ("#### x ####  ", [Heading("x", 4)]),
        ("####### seven", [Paragraph("####### seven")]),
        ("   # three spaces", [Heading("three spaces", 1)]),
        ("    # four", [Paragraph("# four")]),
        ("#nospace", [Paragraph("#nospace")]),
        ("# Title #x", [Heading("Title #x", 1)]),
        ("###### six ######", [Heading("six", 6)]),
        ("==x==", [Heading("x", 2)]),
        ("== x ==", [Heading("x", 2)]),
        ("== x ===", [Heading("x =", 2)]),
        ("=== x ==", [Paragraph("=== x ==")]),
        ("======= x =======", [Paragraph("======= x =======")]),
        ("== ==", []),
        ("==  ==", []),
        ("=== ===", []),
        ("== =x==", [Heading("=x", 2)]),
        ("   == Heading ==   ", [Heading("Heading", 2)]),
        ("==Heading== tail", [Paragraph("==Heading== tail")]),
        ("== a == b ==", [Heading("a == b", 2)]),
        ("== x =", [Paragraph("== x =")]),
        ("===== five =====", [Heading("five", 5)]),
        ("== x===", [Heading("x=", 2)]),
        ("- - -", [Rule()]),
    ],
)
def test_heading_results_are_unchanged(line, expected):
    assert text_to_blocks(line) == expected


@pytest.mark.parametrize(
    "line, expected",
    [
        ("== Heading ==", "Heading"),
        ("# Title ##", "Title ##"),
        ("Title  ===  ", "Title"),
        ("abc =======", "abc ="),
        ("== a == b ==", "a == b"),
        ("x ==y", "x ==y"),
    ],
)
def test_derive_title_results_are_unchanged(line, expected):
    assert derive_title(line) == expected


@pytest.mark.parametrize(
    "line, expected",
    [
        ("== Heading ==", (2, "Heading")),
        ("==Heading==", (2, "Heading")),
        ("== a == b ==", (2, "a == b")),
        ("=== x ==", (2, "= x")),
        ("== x ===", (2, "x =")),
        ("==  ==", (2, "")),
        ("====", (2, "")),
        ("===", None),
        ("== x =", None),
        ("x ==", None),
        ("======= x =======", (6, "= x =")),
    ],
)
def test_wiki_extract_heading_results_are_unchanged(line, expected):
    assert sites._wiki_head(line) == expected
