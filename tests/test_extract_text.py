"""Pasted text -> Document: paragraphing, lists, headings, classification, titles."""

from __future__ import annotations

import pytest

from sendtox3.extract.text import (
    classify,
    clean_preformatted,
    derive_title,
    find_urls,
    first_url,
    guess_lang,
    looks_hard_wrapped,
    normalize_heading_levels,
    text_to_blocks,
    text_to_document,
)
from sendtox3.textmodel import Heading, Paragraph, Preformatted, Quote


def kinds(blocks) -> list[str]:
    return [type(b).__name__ for b in blocks]


def texts(blocks) -> list[str]:
    return [getattr(b, "text", "") for b in blocks]


WRAPPED = """\
The keepers wrote in the logbook every evening, recording the wind, the
state of the sea and any ships that passed close to the rock during the
day. The entries are short, but together they describe forty years of
weather on this stretch of coast in remarkable detail.

In the winter of 1979 the entries stop for nine days. The relief boat
could not land, and the keepers rationed the last of the paraffin while
they waited for the storm to pass.
"""


# --------------------------------------------------------------------------------------
# Paragraphing
# --------------------------------------------------------------------------------------


def test_blank_lines_separate_paragraphs():
    blocks = text_to_blocks("First paragraph.\n\nSecond paragraph.\n\n\n\nThird.")
    assert blocks == [Paragraph("First paragraph."), Paragraph("Second paragraph."), Paragraph("Third.")]


def test_single_newlines_are_paragraphs_in_unwrapped_text():
    # Phone selections and chat apps: one paragraph per line, lines much longer than 100.
    line1 = "A " + "very long sentence that keeps going " * 5 + "end."
    line2 = "Another " + "line that is also quite long and wordy " * 4 + "end."
    blocks = text_to_blocks(f"{line1}\n{line2}\nShort third line")
    assert kinds(blocks) == ["Paragraph"] * 3
    assert blocks[2].text == "Short third line"


def test_hard_wrapped_text_is_rejoined():
    assert looks_hard_wrapped(WRAPPED)
    blocks = text_to_blocks(WRAPPED)
    assert kinds(blocks) == ["Paragraph", "Paragraph"]
    assert blocks[0].text.startswith("The keepers wrote in the logbook every evening, recording the wind, the state")
    assert "\n" not in blocks[0].text
    assert blocks[1].text.endswith("while they waited for the storm to pass.")


def test_hard_wrapped_short_line_ending_a_sentence_ends_the_paragraph():
    text = (
        "The relief boat could not land for nine days, and the keepers rationed\n"
        "the last of the paraffin while they waited for the storm to pass by.\n"
        "Nobody complained.\n"
        "When the boat finally arrived it brought letters, bread and a new lamp\n"
        "mantle, which the senior keeper fitted that same evening before dark.\n"
    )
    blocks = text_to_blocks(text)
    assert texts(blocks)[0].endswith("storm to pass by. Nobody complained.")
    assert texts(blocks)[1].startswith("When the boat finally arrived")


def test_hard_wrapped_indented_lines_start_paragraphs():
    # lib.ru / Gutenberg style: paragraphs start with an indent, no blank lines.
    text = (
        "     The young dog ran up and down the pavement, looking anxiously about her\n"
        "and stopping now and then to lift one frozen paw and then the other.\n"
        "     She remembered very well how she had spent the day and how she had\n"
        "ended up on this unfamiliar pavement in the dark of a winter evening.\n"
        "     The day had begun when her master put on his cap and took a wooden\n"
        "thing wrapped in a red handkerchief under his arm and called her name.\n"
    )
    blocks = text_to_blocks(text)
    assert kinds(blocks) == ["Paragraph"] * 3
    assert blocks[1].text.startswith("She remembered very well")


@pytest.mark.parametrize(
    "text, expected",
    [
        (WRAPPED, True),
        ("one\ntwo\nthree\nfour", False),  # short lines: a list or a poem
        ("x" * 300 + "\n" + "y" * 300 + "\n" + "z" * 300 + "\n" + "w" * 300, False),  # one line per paragraph
        ("only one line of text that is about sixty-five characters long here", False),
        ("", False),
    ],
)
def test_looks_hard_wrapped(text, expected):
    assert looks_hard_wrapped(text) is expected


def test_line_endings_and_unicode_separators():
    blocks = text_to_blocks("One\r\nTwo\rThree\u2028Four\u2029Five\x0cSix")
    assert texts(blocks) == ["One", "Two", "Three", "Four", "Five", "Six"]


# --------------------------------------------------------------------------------------
# Lists, quotes, code, rules, headings
# --------------------------------------------------------------------------------------


def test_bullets_and_numbers_become_list_items():
    blocks = text_to_blocks("Shopping:\n- milk\n* eggs\n  - free range\n• bread\n1. first\n2) second\n10. tenth")
    assert kinds(blocks) == ["Paragraph"] + ["ListItem"] * 7
    milk, _eggs, free, bread, first, second, tenth = blocks[1:]
    assert (milk.text, milk.ordered, milk.depth) == ("milk", False, 0)
    assert (free.text, free.depth) == ("free range", 1)
    assert bread.text == "bread"
    assert (first.ordered, first.index, second.index, tenth.index) == (True, 1, 2, 10)


def test_markdown_auto_numbering_is_renumbered():
    blocks = text_to_blocks("1. alpha\n1. beta\n1. gamma")
    assert [b.index for b in blocks] == [1, 2, 3]


def test_loose_numbered_list_keeps_counting_across_blank_lines():
    blocks = text_to_blocks("1. alpha\n\n1. beta\n\n1. gamma")
    assert [b.index for b in blocks] == [1, 2, 3]


def test_wrapped_list_item_continuation_lines_are_joined():
    text = (
        "The restoration involved the following pieces of work, all of which were\n"
        "paid for by the appeal and the heritage grant that arrived last spring:\n"
        "- replacing every slate on the cottage roof, which took the volunteers\n"
        "  most of the first summer and a great deal of rope and patience\n"
        "- repainting the lantern room and the gallery railings in the original\n"
        "  colours found under twelve later coats of paint by the conservators\n"
    )
    blocks = text_to_blocks(text)
    assert kinds(blocks) == ["Paragraph", "ListItem", "ListItem"]
    assert blocks[1].text.endswith("a great deal of rope and patience")


def test_quotes_and_nested_quote_content():
    blocks = text_to_blocks(
        "> The light still turns.\n> Every ten seconds.\n\nThat is what he said.\n\n> - a quoted item"
    )
    assert blocks[0] == Quote("The light still turns.")
    assert blocks[1] == Quote("Every ten seconds.")
    assert blocks[2] == Paragraph("That is what he said.")
    assert blocks[3] == Quote("• a quoted item")


def test_fenced_code_keeps_indentation_and_newlines():
    text = "Run this:\n\n```python\ndef f(x):\n    return  x * 2\n\tpass\n```\n\nDone."
    blocks = text_to_blocks(text)
    assert kinds(blocks) == ["Paragraph", "Preformatted", "Paragraph"]
    assert blocks[1].text == "def f(x):\n    return  x * 2\n    pass"


def test_unclosed_fence_runs_to_the_end():
    blocks = text_to_blocks("```\nline one\n  line two")
    assert blocks == [Preformatted("line one\n  line two")]


@pytest.mark.parametrize("rule", ["---", "***", "* * *", "___", "———", "- - -", "====="])
def test_rules(rule):
    blocks = text_to_blocks(f"Before.\n\n{rule}\n\nAfter.")
    assert kinds(blocks) == ["Paragraph", "Rule", "Paragraph"]


def test_only_clearly_marked_headings_become_headings():
    text = "# The Title\n\nIntroduction\nSome text under a heading-looking line.\n\n## Section\n\n=== Wiki style ===\n\nBody."
    blocks = text_to_blocks(text)
    assert blocks[0] == Heading("The Title", level=1)
    assert blocks[1] == Paragraph("Introduction")  # looks like a heading, but is not marked
    assert blocks[3] == Heading("Section", level=2)
    assert blocks[4] == Heading("Wiki style", level=3)


def test_setext_headings_need_a_matching_underline():
    assert text_to_blocks("Chapter One\n===========\n\nText.")[0] == Heading("Chapter One", level=1)
    assert text_to_blocks("Part two\n--------\n\nText.")[0] == Heading("Part two", level=2)
    # a long dash line under a short line is a separator, not an underline
    blocks = text_to_blocks("Thanks\n" + "-" * 40 + "\nMore text")
    assert kinds(blocks) == ["Paragraph", "Rule", "Paragraph"]


def test_hashtags_are_not_headings():
    assert text_to_blocks("#tbt to the summer")[0] == Paragraph("#tbt to the summer")


def test_normalize_heading_levels_maps_used_levels_to_2_and_3():
    blocks = [Heading("a", 1), Paragraph("x"), Heading("b", 3), Heading("c", 5), Heading("d", 1)]
    assert [b.level for b in normalize_heading_levels(blocks) if isinstance(b, Heading)] == [2, 3, 3, 2]


def test_drop_empty_sections_cascades():
    from sendtox3.extract.text import drop_empty_sections

    blocks = [Heading("A", 2), Heading("A.1", 3), Heading("B", 2), Paragraph("b"), Heading("C", 2)]
    assert drop_empty_sections(blocks) == [Heading("B", 2), Paragraph("b")]


def test_clean_preformatted_keeps_layout_but_cleans_characters():
    raw = "\n\nx\u00ad =\t1  # note\u200b ✓\r\n    y = 2   \n\n"
    assert clean_preformatted(raw) == "x =    1  # note [x]\n    y = 2"


# --------------------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------------------


def test_text_to_document_derives_title_and_drops_repeated_first_line():
    doc = text_to_document("Shopping list\n- milk\n- eggs")
    assert doc.title == "Shopping list"
    assert kinds(doc.blocks) == ["ListItem", "ListItem"]


def test_text_to_document_keeps_single_paragraph_even_if_it_is_the_title():
    doc = text_to_document("The only way to do great work is to love what you do.")
    assert doc.title == "The only way to do great work is to love what you do."
    assert doc.blocks == [Paragraph("The only way to do great work is to love what you do.")]


def test_text_to_document_uses_given_title_and_source():
    doc = text_to_document("Body text here.", title="  My   note ", source_url="https://www.example.com/a")
    assert doc.title == "My note"
    assert doc.source_url == "https://www.example.com/a"
    assert doc.site == "example.com"
    assert doc.blocks == [Paragraph("Body text here.")]


def test_text_to_document_markdown_title_heading_is_used_and_levels_normalized():
    doc = text_to_document("# Trip notes\n\nDay one.\n\n## Morning\n\nWalk.\n\n### Detail\n\nMore.")
    assert doc.title == "Trip notes"
    heads = [b for b in doc.blocks if isinstance(b, Heading)]
    assert [(h.text, h.level) for h in heads] == [("Morning", 2), ("Detail", 3)]


def test_text_to_document_cleans_text_for_the_device():
    doc = text_to_document("Café ™ trip 🎉\n\nzero\u200bwidth soft\u00adhyphen  spaces")
    assert doc.title == "Café (TM) trip"
    assert doc.blocks == [Paragraph("zerowidth softhyphen spaces")]


def test_text_to_document_empty_input():
    doc = text_to_document("   \n\n  ")
    assert doc.blocks == [] and doc.title == ""


def test_text_to_document_language_guess():
    ru = "Молодая рыжая собака бегала взад и вперед по тротуару и беспокойно оглядывалась по сторонам."
    assert text_to_document(ru).lang == "ru"
    assert text_to_document("Hello there, this is the note that I wanted to send to the reader today.").lang == "en"


# --------------------------------------------------------------------------------------
# classify / first_url / derive_title
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, url, title, expected",
    [
        ("https://example.com/a", "", "", "url"),
        ("  <https://example.com/a>  ", "", "", "url"),
        ("https://example.com/a\n\nSent from my iPhone", "", "", "url"),
        ("Check this out https://example.com/a", "", "", "url"),
        ("via https://example.com/a", "", "", "url"),
        ("Great read https://example.com/a", "", "Great read about tides and harbours", "url"),
        (
            "Lighthouse keepers return to Gull Rock - The Example Gazette https://example.com/a",
            "",
            "Lighthouse keepers return to Gull Rock",
            "url",
        ),
        ("Remember to buy milk https://shop.example/list", "", "", "text"),
        ("Remember to buy milk https://shop.example/list", "", "Weekly specials", "text"),
        ("A long comment. " * 20 + "https://example.com/a", "", "", "text"),
        ("", "https://example.com/a", "", "url"),
        ("", "", "", "text"),
        ("no links here at all", "", "", "text"),
        ("Some selected passage of text from the page.", "https://example.com/a", "Page title", "text"),
        ("Page title", "https://example.com/a", "Page title", "url"),
        ("https://a.example/1 https://b.example/2", "", "", "text"),
        ("https://a.example/1 and again https://a.example/1", "", "", "text"),
    ],
)
def test_classify(text, url, title, expected):
    assert classify(text, url, title) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("see https://example.com/a.", "https://example.com/a"),
        ("(https://en.wikipedia.org/wiki/Foo_(bar))", "https://en.wikipedia.org/wiki/Foo_(bar)"),
        ("[label](https://example.com/x)", "https://example.com/x"),
        ("<https://example.com/x>", "https://example.com/x"),
        ("www.example.com/path, and more", "https://www.example.com/path"),
        ("ftp://example.com/file and https://b.example/", "https://b.example/"),
        ("“https://example.com/q?a=1&b=2”", "https://example.com/q?a=1&b=2"),
        ("Read: HTTPS://Example.com/Case!", "HTTPS://Example.com/Case"),
        ("no url here", ""),
        ("", ""),
    ],
)
def test_first_url(text, expected):
    assert first_url(text) == expected


def test_find_urls_dedupes_in_order():
    assert find_urls("a https://x.example/1 b https://y.example/2 c https://x.example/1") == [
        "https://x.example/1",
        "https://y.example/2",
    ]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Shopping list\n- milk\n- eggs", "Shopping list"),
        ("# My Note\n\nBody", "My Note"),
        ("https://example.com\nActual title line", "Actual title line"),
        ("> quoted first line\nmore", "quoted first line"),
        ("🎉 Party at 8pm", "Party at 8pm"),
        ("Notes:", "Notes"),
        ("", ""),
        ("   \n\n", ""),
        ("https://only-a-link.example/", ""),
    ],
)
def test_derive_title(text, expected):
    assert derive_title(text) == expected


def test_derive_title_prefers_first_sentence_of_long_line():
    text = (
        "The relief boat finally landed on Thursday. It brought letters, bread, a new lamp mantle and "
        "a great deal of news from the mainland that the keepers had missed during the storm."
    )
    assert derive_title(text) == "The relief boat finally landed on Thursday."


def test_derive_title_cuts_long_line_at_word_boundary():
    text = "word " * 40
    title = derive_title(text)
    assert len(title) <= 80
    assert title.endswith("…")
    assert not title[:-1].endswith(" ")
    assert title[:-1].split()[-1] == "word"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Молодая рыжая собака бегала взад и вперед по тротуару, беспокойно оглядываясь.", "ru"),
        ("Ця книга розповідає про життя українського села і його мешканців у дев'ятнадцятому столітті.", "uk"),
        ("銀河鉄道の夜は宮沢賢治の童話です。ジョバンニとカムパネルラの旅を描いています。", "ja"),
        ("这是一个用于测试的中文句子，它描述了一个小港口的潮汐变化和灯塔的历史。", "zh"),
        ("Η γλώσσα αυτή γράφεται με το ελληνικό αλφάβητο εδώ και πολλούς αιώνες στην περιοχή.", "el"),
        ("Die Leuchtturmwärter sind nach vierzig Jahren auf die Insel zurückgekehrt, und das ist nicht alles.", "de"),
        ("Les gardiens du phare sont revenus sur le rocher après quarante ans, et la lumière est toujours là.", "fr"),
        ("Los fareros han vuelto a la roca después de cuarenta años y la luz sigue girando como antes.", "es"),
        ("The keepers have returned to the rock after forty years and the light is still turning as it was.", "en"),
        ("short", "en"),
    ],
)
def test_guess_lang(text, expected):
    assert guess_lang(text) == expected


def test_guess_lang_default_for_ambiguous_latin():
    assert guess_lang("Xyzzy plugh frobozz quux blorb zork grue flathead dimwit gnusto rezrov", default="xx") == "xx"
