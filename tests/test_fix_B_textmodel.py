"""clean_text drops noncharacters and lone surrogates (the result must encode as UTF-8)."""

from __future__ import annotations

import json

import pytest

from sendtox3.render.txt import render_txt
from sendtox3.textmodel import Document, Paragraph, clean_text


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a\ud800b", "ab"),  # lone high surrogate
        ("a\udfffb", "ab"),  # lone low surrogate
        ("x\ufffey\uffffz", "xyz"),
        ("p\ufdd0q\ufdefr\ufde0s", "pqrs"),
        ("ok\ud83d\ude00", "ok"),  # a valid pair is joined by NFC first (then dropped as emoji)
        ("caf\u00e9 \u00bd", "caf\u00e9 \u00bd"),  # ordinary text untouched
    ],
)
def test_noncharacters_and_lone_surrogates_are_removed(raw, expected):
    out = clean_text(raw)
    assert out == expected
    out.encode("utf-8")  # never raises


def test_keep_newlines_mode_too():
    assert clean_text("line\ud800 one\n\ufffeline two", keep_newlines=True) == "line one\nline two"


def test_json_lone_surrogate_survives_to_a_rendered_file():
    text = json.loads('"Note \\ud83d about \\ufffe things"')  # what a buggy client may send
    doc = Document(title=clean_text(text), blocks=[Paragraph(clean_text(text))])
    assert b"Note about things" in render_txt(doc)
