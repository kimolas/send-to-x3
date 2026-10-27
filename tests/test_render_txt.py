"""TXT renderer (render/txt.py, ARCHITECTURE §6.1)."""

from __future__ import annotations

import pytest

from sendtox3.render.txt import (
    MAX_LINE_BYTES,
    host_of,
    iso_date,
    meta_parts,
    pre_lines,
    render_txt,
    split_long_line,
)
from sendtox3.textmodel import Document, Heading, ListItem, Paragraph, Preformatted, Quote, Rule

RULE = "\u2014\u2014\u2014"


def _b(s: str) -> int:
    return len(s.encode("utf-8"))


def _doc(**kw) -> Document:
    base = dict(
        title="How E-Ink Works",
        byline="Jane Doe",
        site="Example News",
        source_url="https://www.example.com/articles/e-ink?id=7",
        published="2026-10-24",
    )
    base.update(kw)
    return Document(**base)


def _text(doc: Document) -> str:
    data = render_txt(doc)
    assert isinstance(data, bytes)
    return data.decode("utf-8")


def _assert_file_rules(data: bytes) -> None:
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data and b"\t" not in data and b"\x00" not in data
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    text = data.decode("utf-8")  # valid UTF-8
    for line in text.split("\n"):
        assert _b(line) <= MAX_LINE_BYTES, line[:80]


def test_full_layout():
    doc = _doc(
        blocks=[
            Paragraph("First paragraph."),
            Heading("A section", 2),
            Paragraph("Second paragraph."),
            Heading("Minor heading", 3),
            ListItem("apples"),
            ListItem("green ones", depth=1),
            ListItem("pears"),
            Quote("To be or not to be."),
            Preformatted("def f():\n    return 1"),
            Rule(),
            Paragraph("Last."),
        ]
    )
    data = render_txt(doc)
    _assert_file_rules(data)
    assert data.decode("utf-8") == (
        "How E-Ink Works\n"
        "Jane Doe \u00b7 Example News \u00b7 2026-10-24\n"
        f"{RULE}\n"
        "\n"
        "First paragraph.\n"
        "\n"
        "A SECTION\n"
        "\n"
        "Second paragraph.\n"
        "\n"
        "Minor heading\n"
        "\n"
        "\u2022 apples\n"
        "  \u2022 green ones\n"
        "\u2022 pears\n"
        "\n"
        "> To be or not to be.\n"
        "\n"
        "def f():\n"
        "    return 1\n"
        "\n"
        "* * *\n"
        "\n"
        "Last.\n"
        "\n"
        f"{RULE}\n"
        "Source: https://www.example.com/articles/e-ink?id=7\n"
    )


def test_header_parts_omitted_when_empty():
    t = _text(_doc(byline="", site="", source_url="", published="", blocks=[Paragraph("x")]))
    assert t == f"How E-Ink Works\n{RULE}\n\nx\n"
    t = _text(_doc(byline="", site="", published="", blocks=[Paragraph("x")]))
    assert t.splitlines()[1] == "example.com"  # site falls back to the URL host
    t = _text(_doc(byline="BBC News", site="BBC News", blocks=[]))
    assert t.splitlines()[1] == "BBC News \u00b7 2026-10-24"  # no duplicate
    t = _text(_doc(published="2026-13-40", blocks=[]))
    assert t.splitlines()[1] == "Jane Doe \u00b7 Example News \u00b7 2026"


def test_no_blocks_and_no_title():
    t = _text(Document(title=""))
    assert t == f"Untitled\n{RULE}\n"


def test_headings_levels():
    t = _text(_doc(source_url="", blocks=[Heading("level one", 1), Heading("Stra\u00dfe", 2), Heading("keep Case", 3)]))
    lines = t.split("\n")
    assert "LEVEL ONE" in lines and "STRASSE" in lines and "keep Case" in lines


def test_ordered_lists_and_nesting():
    blocks = [
        ListItem("one", ordered=True, index=1),
        ListItem("two", ordered=True, index=2),
        ListItem("sub a", ordered=False, depth=1),
        ListItem("deep", ordered=True, index=1, depth=2),
        ListItem("ten", ordered=True, index=10),
    ]
    t = _text(_doc(source_url="", blocks=blocks))
    assert "1. one\n2. two\n  \u2022 sub a\n    1. deep\n10. ten\n" in t


def test_bullet_list_then_numbered_list_are_separate():
    blocks = [ListItem("a"), ListItem("b"), ListItem("one", ordered=True, index=1), Paragraph("p"), ListItem("c")]
    t = _text(_doc(source_url="", blocks=blocks))
    assert "\u2022 a\n\u2022 b\n\n1. one\n\np\n\n\u2022 c\n" in t


def test_quotes_each_paragraph_prefixed():
    t = _text(_doc(source_url="", blocks=[Quote("one"), Quote("two")]))
    assert "> one\n\n> two\n" in t


def test_preformatted_keeps_lines_and_indentation():
    pre = Preformatted("if x:\n\tdo()\n\n    two  spaces\nend")
    t = _text(_doc(source_url="", blocks=[Paragraph("before"), pre, Paragraph("after")]))
    assert "before\n\nif x:\n    do()\n\n    two  spaces\nend\n\nafter\n" in t


def test_pre_lines_cleaning():
    assert pre_lines("\n\n  a\u200b b \U0001f600 c\x07\n\n") == ["  a b  c"]
    assert pre_lines("x\r\ny\rz") == ["x", "y", "z"]
    assert pre_lines("") == []


def test_notes_at_top():
    t = _text(_doc(source_url="", notes=["Paywalled: only the preview was available"], blocks=[Paragraph("x")]))
    assert t.split("\n")[3:6] == ["", "Note: Paywalled: only the preview was available", ""]


def test_text_is_cleaned():
    doc = _doc(
        title="T\u200bitle \U0001f600",
        source_url="https://example.com/a b\tc",
        blocks=[Paragraph("bad\x00 \ufeffchars\u00ad and  spaces\u2122 \u2192 ok")],
    )
    data = render_txt(doc)
    _assert_file_rules(data)
    t = data.decode("utf-8")
    assert t.startswith("Title\n")
    assert "bad chars and spaces(TM) -> ok\n" in t
    assert t.endswith("Source: https://example.com/abc\n")


def test_lone_surrogates_do_not_crash():
    data = render_txt(_doc(title="a\ud800b", blocks=[Paragraph("x\udfffy")]))
    assert data.decode("utf-8").startswith("ab\n")


# --------------------------------------------------------------------------------------
# 900-byte rule
# --------------------------------------------------------------------------------------


def _sentences(n: int, word: str = "word") -> str:
    return " ".join(f"Sentence {i} has a few {word} words in it." for i in range(n))


def test_short_lines_untouched():
    s = "x" * MAX_LINE_BYTES
    assert split_long_line(s) == [s]
    assert split_long_line("") == []


def test_split_at_sentence_boundaries():
    text = _sentences(60)
    chunks = split_long_line(text)
    assert len(chunks) > 1
    assert " ".join(chunks) == text
    for c in chunks:
        assert _b(c) <= MAX_LINE_BYTES
        assert c.endswith("."), c[-30:]
        assert c[0].isupper()


def test_split_sentence_with_closing_quote():
    text = ('He said "stop." ' + "x " * 200 + 'Then: "go!" ') * 4
    chunks = split_long_line(text.strip())
    assert all(_b(c) <= MAX_LINE_BYTES for c in chunks)
    assert " ".join(chunks) == " ".join(text.split())


def test_split_falls_back_to_spaces():
    text = " ".join(["lorem"] * 400)  # no sentence ends
    chunks = split_long_line(text)
    assert all(_b(c) <= MAX_LINE_BYTES for c in chunks)
    assert all(c.split(" ") == ["lorem"] * len(c.split(" ")) for c in chunks)
    assert " ".join(chunks) == text


def test_split_hard_cut_on_character_boundary():
    text = "\u00e9" * 1000  # 2000 bytes, no spaces
    chunks = split_long_line(text)
    assert "".join(chunks) == text
    assert all(_b(c) <= MAX_LINE_BYTES for c in chunks)


def test_split_cjk_full_stops():
    sent = "\u3053\u308c\u306f\u6587\u3067\u3059\u3002"  # 7 chars, 21 bytes
    text = sent * 120
    chunks = split_long_line(text)
    assert "".join(chunks) == text
    assert all(_b(c) <= MAX_LINE_BYTES and c.endswith("\u3002") for c in chunks)


def test_early_sentence_end_prefers_space_over_tiny_chunk():
    text = "Hi. " + " ".join(["lorem"] * 400)
    chunks = split_long_line(text)
    assert _b(chunks[0]) > MAX_LINE_BYTES // 2


def test_long_paragraph_rendered_as_separate_paragraphs():
    text = _sentences(80)
    t = _text(_doc(source_url="", blocks=[Paragraph(text)]))
    body = t.split(f"{RULE}\n\n", 1)[1]
    paras = [p for p in body.split("\n\n") if p.strip()]
    assert len(paras) > 1
    assert " ".join(p.strip() for p in paras) == text


def test_long_list_item_and_quote_respect_limit():
    text = _sentences(60)
    data = render_txt(_doc(source_url="", blocks=[ListItem(text, depth=2), Quote(text), ListItem(text, ordered=True, index=12)]))
    _assert_file_rules(data)
    t = data.decode("utf-8")
    assert "    \u2022 Sentence 0" in t
    assert "\n      Sentence" in t  # continuation lines aligned under the text
    quote_lines = [ln for ln in t.split("\n") if ln.startswith("> ")]
    assert len(quote_lines) > 1
    assert "12. Sentence 0" in t


def test_absurd_list_depth_is_capped():
    data = render_txt(_doc(source_url="", blocks=[ListItem("deep " * 300, depth=1000), ListItem("x", depth=-5)]))
    _assert_file_rules(data)
    lines = data.decode("utf-8").split("\n")
    assert any(ln.startswith(" " * 16 + "\u2022 deep") for ln in lines)
    assert "\u2022 x" in lines


def test_long_preformatted_line_split():
    data = render_txt(_doc(source_url="", blocks=[Preformatted("  " + "token " * 400)]))
    _assert_file_rules(data)


def test_long_url_and_title():
    data = render_txt(_doc(title="t" * 2000, source_url="https://example.com/" + "a" * 3000))
    _assert_file_rules(data)


def test_deterministic():
    doc = _doc(blocks=[Paragraph(_sentences(50)), ListItem("x")])
    assert render_txt(doc) == render_txt(doc)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, host",
    [
        ("https://www.example.com/x", "example.com"),
        ("http://sub.example.co.uk:8080/y", "sub.example.co.uk"),
        ("https://xn--bcher-kva.example/", "b\u00fccher.example"),
        ("not a url", ""),
        ("", ""),
        ("https://[::1]/", "::1"),
        ("http://[bad", ""),
    ],
)
def test_host_of(url, host):
    assert host_of(url) == host


@pytest.mark.parametrize(
    "raw, out",
    [
        ("2026-10-24", "2026-10-24"),
        ("2026-10-24T10:00:00Z", "2026-10-24"),
        ("2026-10", "2026-10"),
        ("2026", "2026"),
        ("2026-00-10", "2026"),
        ("2026-10-99", "2026-10"),
        ("0000-01-01", ""),
        ("yesterday", ""),
        ("", ""),
    ],
)
def test_iso_date(raw, out):
    assert iso_date(raw) == out


def test_meta_parts():
    assert meta_parts(_doc()) == ("Jane Doe", "Example News", "2026-10-24")
    assert meta_parts(_doc(site="", byline="")) == ("", "example.com", "2026-10-24")
