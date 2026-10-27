"""Firmware-safe titles (render/names.py, ARCHITECTURE §8).

The property tests check every produced title against Python ports of the firmware's
file-name rules (2.x ``sanitizeFilenamePreservingExtension`` + ``..`` rejection + 8.3
alias rejection; 1.6.5 ``sanitizeFilename``). ``tests/test_render_validation.py``
checks the same titles against the real C++ code when the firmware harness is built.
"""

from __future__ import annotations

import random
import time
import unicodedata

import pytest

from sendtox3.render import names
from sendtox3.render.names import (
    fallback_title,
    fw_is_short_alias,
    fw_plugin_filename_ok,
    fw_sanitize_filename,
    fw_sanitize_preserving_extension,
    is_firmware_safe,
    safe_title,
    strip_site_affixes,
    title_problems,
    unique_title,
)

NOW = 1_761_400_000  # fixed clock for the empty-title fallback


def _b(s: str) -> int:
    return len(s.encode("utf-8"))


# --------------------------------------------------------------------------------------
# Firmware ports: pinned to behaviour observed in the real firmware
# --------------------------------------------------------------------------------------


class TestFirmwarePorts:
    def test_2x_observed_sanitizer_output(self):
        # plugins-hosttest.md §2 item 5: the simulator turned this title into
        # "Wait... what_ _a_b__c_d.txt" and then refused it because of "..".
        name = fw_sanitize_preserving_extension("Wait... what? <a/b>:c*d.txt")
        assert name == "Wait... what_ _a_b__c_d.txt"
        assert not fw_plugin_filename_ok(name)

    def test_2x_keeps_existing_extension_inside_title(self):
        # plugins-hosttest.md §2 item 6: "notes.md" + ".txt" -> "notes.md.txt"
        assert fw_sanitize_preserving_extension("notes.md.txt") == "notes.md.txt"

    def test_leading_and_trailing_trim(self):
        assert fw_sanitize_filename("  ..hidden. . ") == "hidden"
        assert fw_sanitize_filename(" . ") == "book"
        assert fw_sanitize_filename("") == "book"

    def test_control_characters_dropped_illegal_replaced(self):
        assert fw_sanitize_filename("a\nb\tc\x7fd") == "abcd"
        assert fw_sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"

    def test_byte_cap_on_codepoint_boundary(self):
        s = "\u00e9" * 60  # 120 bytes
        out = fw_sanitize_filename(s)
        assert out == "\u00e9" * 50 and _b(out) == 100
        # 2.x: the stem budget is 100 - len(".epub") = 95 bytes -> 47 two-byte chars
        out2 = fw_sanitize_preserving_extension(s + ".epub")
        assert out2 == "\u00e9" * 47 + ".epub"

    def test_unconventional_extension_falls_back(self):
        # extension with a non-alnum character is not preserved; whole name capped at 100
        assert fw_sanitize_preserving_extension("x" * 120 + ".e-b") == "x" * 100

    def test_short_alias_detection(self):
        assert fw_is_short_alias("Note~1.txt")
        assert fw_is_short_alias("CROSSP~1")
        assert fw_is_short_alias("ab~9")
        assert not fw_is_short_alias("Note~1.epub")  # extension longer than 3
        assert not fw_is_short_alias("LongerName~1.txt")  # base longer than 8
        assert not fw_is_short_alias("Note~ 1.txt")
        assert not fw_is_short_alias("Note-1.txt")
        assert not fw_plugin_filename_ok("Note~1.txt", "/Inbox")
        assert fw_plugin_filename_ok("Note~ 1.txt", "/Inbox")

    def test_plugin_filename_rules(self):
        assert fw_plugin_filename_ok("Hello.epub")
        assert not fw_plugin_filename_ok("")
        assert not fw_plugin_filename_ok("a..b.epub")
        assert not fw_plugin_filename_ok("a/b.epub")
        assert fw_plugin_filename_ok("Hello.epub", "/")


# --------------------------------------------------------------------------------------
# safe_title: specific rules
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Plain title", "Plain title"),
        ("Wait... what? <a/b>:c*d", "Wait\u2026 what a-b - cd"),
        ("Wait\u2026 what", "Wait\u2026 what"),
        ("Dots.. and.... more", "Dots\u2026 and\u2026 more"),
        ("End with dots...", "End with dots\u2026"),
        ("Ellipsis\u2026.", "Ellipsis\u2026"),
        ("Python: A guide", "Python - A guide"),
        ("Re:Lunch", "Re - Lunch"),
        ("Meeting at 10:30", "Meeting at 10.30"),
        ("AC/DC and back\\slash", "AC-DC and back-slash"),
        ("Article | Site", "Article - Site"),
        ("Why?", "Why"),
        ("F*** this", "F this"),
        ('He said "hi"', "He said 'hi'"),
        ("<script>alert</script>", "scriptalert-script"),
        ("{url} and {title}", "url and title"),
        ("Takes ~5 minutes", "Takes ~ 5 minutes"),
        ("Note~1", "Note~ 1"),
        ("...and then", "\u2026and then"),
        (".htaccess explained", "htaccess explained"),
        ("  spaced   out  ", "spaced out"),
        ("Trailing dot.", "Trailing dot"),
        ("Title:", "Title"),
        ("A -> B => C", "A - B - C"),
        ("Rock \U0001f600\U0001f3b8 on", "Rock on"),
        ("Caf\u0065\u0301 cr\u00e8me", "Caf\u00e9 cr\u00e8me"),  # NFC
        ("Line\nbreak\ttab", "Line break tab"),
        ("zero\u200bwidth\u00adsoft", "zerowidthsoft"),
        ("Trademark\u2122", "Trademark(TM)"),
        ("https://example.com/foo/bar", "example.com-foo-bar"),
        ("\u041f\u0440\u0438\u0432\u0435\u0442 \u043c\u0438\u0440", "\u041f\u0440\u0438\u0432\u0435\u0442 \u043c\u0438\u0440"),
        ("\u65e5\u672c\u8a9e\u306e\u30bf\u30a4\u30c8\u30eb", "\u65e5\u672c\u8a9e\u306e\u30bf\u30a4\u30c8\u30eb"),
    ],
)
def test_safe_title_examples(raw, expected):
    out = safe_title(raw, now=NOW)
    assert out == expected
    assert title_problems(out) == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("CON", "CON_"),
        ("con", "con_"),
        ("Prn", "Prn_"),
        ("AUX", "AUX_"),
        ("NUL", "NUL_"),
        ("COM1", "COM1_"),
        ("lpt9", "lpt9_"),
        ("COM\u00b9", "COM\u00b9_"),
        ("CON.txt", "CON_.txt"),
        ("Aux. notes", "Aux_. notes"),
        ("CON Air", "CON Air"),  # not reserved: the part before the first dot is "CON Air"
        ("Console", "Console"),
        ("COM10", "COM10"),
    ],
)
def test_windows_reserved_names_are_suffixed(raw, expected):
    assert safe_title(raw) == expected


def test_site_affixes():
    assert safe_title("Python (programming language) - Wikipedia") == "Python (programming language)"
    assert safe_title("Paris \u2014 Wikip\u00e9dia") == "Paris"
    assert safe_title("Madrid - Wikipedia, la enciclopedia libre") == "Madrid"
    assert safe_title("GitHub - simonw/llm: Access large language models") == "simonw-llm - Access large language models"
    assert safe_title("Fix the bug \u00b7 Issue #12 \u00b7 owner/repo \u00b7 GitHub") == "Fix the bug \u00b7 Issue #12 \u00b7 owner-repo"
    assert safe_title("My video - YouTube") == "My video"
    assert safe_title("A story | by Jane Doe | Medium") == "A story"
    assert safe_title("Story | The Guardian", site="The Guardian") == "Story"
    assert safe_title("Story | The Guardian", site="theguardian.com") == "Story"
    assert safe_title("The Guardian - Story here", site="The Guardian") == "Story here"
    assert safe_title("BBC: Something happened", site="BBC") == "Something happened"
    # only when it equals the site
    assert safe_title("Story | Other Site", site="The Guardian") == "Story - Other Site"
    # never strips everything
    assert safe_title("Wikipedia") == "Wikipedia"
    assert strip_site_affixes("The Guardian", "The Guardian") == "The Guardian"
    assert strip_site_affixes("Go | Go", "Go") == "Go | Go"  # remainder too short


def test_affix_created_by_replacement_is_stripped_too():
    assert safe_title("Foo / Wikipedia") == "Foo"
    assert safe_title("Story / The Guardian", site="The Guardian") == "Story"
    for raw in ("Foo / Wikipedia", "A: B | YouTube", "x / github", "Story / The Guardian"):
        once = safe_title(raw, site="The Guardian")
        assert safe_title(once) == once


def test_byte_cap_prefers_word_boundary_and_marks_cut():
    raw = "An extremely long headline about everything that happened in the world today and yesterday too"
    out = safe_title(raw)
    assert _b(out) <= 90
    assert out.endswith("\u2026")
    assert raw.startswith(out[:-1])
    assert out[-2] != " "
    assert out[:-1] == raw[: len(out) - 1]
    assert raw[len(out) - 1] == " "  # cut at a word boundary


def test_byte_cap_on_character_boundary():
    for ch in ("x", "\u00e9", "\u20ac", "\u4e2d"):
        out = safe_title(ch * 300)
        assert _b(out) <= 90
        assert out.endswith("\u2026")
        assert set(out[:-1]) == {ch}


def test_byte_cap_does_not_split_combining_sequences():
    # q + dot below + dot above has no precomposed form: NFC keeps 3 code points (5 bytes)
    cluster = unicodedata.normalize("NFC", "q\u0307\u0323")
    assert len(cluster) == 3
    for pad in range(5):
        raw = "a" * pad + cluster * 40
        out = safe_title(raw)
        body = out[:-1]
        assert out.endswith("\u2026")
        assert raw.startswith(body)
        assert not unicodedata.combining(raw[len(body)]), (pad, out)


def test_custom_max_bytes():
    out = safe_title("word " * 40, max_bytes=30)
    assert _b(out) <= 30
    assert title_problems(out, max_bytes=30) == []
    tiny = safe_title("abcdefgh", max_bytes=4)
    assert tiny == "abcd"
    with pytest.raises(ValueError):
        safe_title("x", max_bytes=0)


@pytest.mark.parametrize("raw", ["", "   ", "\n\t", "....", "???", "{}", "\U0001f600\U0001f600", "\u200b", "///"])
def test_empty_results_fall_back_to_dated_note(raw):
    out = safe_title(raw, now=NOW)
    if raw == "....":
        assert out == "\u2026"
        return
    if raw == "///":
        assert out == "---"
        return
    assert out == fallback_title(NOW)
    assert out == time.strftime("Note %Y-%m-%d %H.%M", time.localtime(NOW))
    assert title_problems(out) == []


def test_fallback_uses_clock_when_now_missing(monkeypatch):
    monkeypatch.setattr(names.time, "time", lambda: NOW)
    assert safe_title("") == fallback_title(NOW)


# --------------------------------------------------------------------------------------
# Property tests over many tricky titles
# --------------------------------------------------------------------------------------

TRICKY = [
    "Wait... what?",
    "..",
    ".",
    "...",
    ". . .",
    "..hidden",
    "trailing...",
    "a.?.b",
    "a.*.b",
    "a.{}.b",
    "a. .b",
    "a.\u2026b",
    "\u2026",
    "x" * 300,
    ("\u00e9" * 150),
    ("\u4e2d\u6587" * 80),
    "a " * 200,
    "{",
    "}",
    "{{title}}",
    "{cfg.token}",
    "/",
    "\\\\server\\share",
    "C:\\Windows\\System32",
    "a/../b",
    "../../etc/passwd",
    "CON",
    "PRN.epub",
    "aux .txt",
    "nul.",
    "COM1",
    "LPT1",
    "lpt\u00b3",
    "CLOCK$",
    "~1",
    "NOTE~1",
    "a~9b",
    "~~~",
    "file~10.txt",
    " leading space",
    "trailing space ",
    "\u00a0nbsp\u00a0",
    "\u3000ideographic\u3000space",
    "tab\there",
    "new\nline",
    "cr\rreturn",
    "nul\x00byte",
    "del\x7fchar",
    "c1\x85\x9fcontrols",
    "bidi\u202eoverride\u202c",
    "bom\ufeffstart",
    "\ud800lone surrogate",
    "private\ue000use",
    "nonchar\uffff",
    "unassigned\U000e0001tag",
    "\U0001d400 math bold",
    "\U00020000 cjk ext b",
    "emoji \U0001f468\u200d\U0001f469\u200d\U0001f467 family",
    "flag \U0001f1fa\U0001f1f8",
    "keycap 1\ufe0f\u20e3",
    "\u2603 snowman \u2764\ufe0f",
    "\u00bfQu\u00e9?",
    "\u00a1Hola!",
    "100% <real> \"news\"",
    "Price: $5 | Shop",
    "50*50=2500",
    "a|b|c",
    "<<<>>>",
    "\"\"\"",
    "''",
    "*?*?",
    "  -  ",
    "- dash start",
    "dash end -",
    "\u2014 em dash \u2014",
    "e\u0301\u0301\u0301",
    "\u0301 leading combining",
    "\u05e9\u05dc\u05d5\u05dd hebrew",
    "\u0645\u0631\u062d\u0628\u0627 arabic",
    "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35 thai",
    "\u1f00\u03bb\u03bb\u03ac greek",
    "Tie\u0301\u0301ng Vie\u0323\u0302t",
    "\u2122\u2190\u2192\u2264\u2265\u221e",
    "fi\ufb01 ligature",
    "%20encoded%2F",
    "#hash &amp; entity &lt;",
    "a" * 89 + "\u00e9",
    "a" * 88 + " " + "\u00e9" * 3,
    "word " * 17 + "end",
    "con.tar.gz",
    "Nul byte\x00in middle",
    "\u2026\u2026\u2026",
    ".\u2026.",
    "\u2024\u2025 one/two dot leaders",
    "\uff0e\uff0e fullwidth dots",
]


def _random_titles(n: int, seed: int = 1234) -> list[str]:
    rnd = random.Random(seed)
    pool = list(
        "abcXYZ019 ..........\u2026\u2026{}{}/\\:*?\"<>|~~~~--_'`!@#$%^&()[];,+=\t\n\r\x00\x01\x1f\x7f\u00a0"
        "\u00e9\u00df\u00e6\u0153\u0107\u0161\u017e\u0441\u043b\u043e\u0432\u043e\u4e2d\u6587\u65e5\u672c"
        "\u05e9\u0627\u0301\u0323\u200b\u200d\u00ad\ufeff\u2028\u2029\u202e\ufe0f\u2122\u2192\u2264"
    ) + ["\U0001f600", "\U0001f1fa", "\U0001d400", "\ud800", "\uffff", "\ue000", "CON", "lpt1", "~1", "..", "..."]
    out = []
    for _ in range(n):
        k = rnd.choice([1, 2, 3, 5, 8, 13, 40, 120, 400])
        out.append("".join(rnd.choice(pool) for _ in range(k)))
    return out


ALL_TITLES = TRICKY + _random_titles(1500)


@pytest.mark.parametrize("max_bytes, site", [(90, ""), (60, ""), (120, ""), (90, "Example News")])
def test_property_firmware_safe_idempotent_bounded(max_bytes, site):
    for raw in ALL_TITLES + [t + " | Example News" for t in TRICKY] + [t + " / Wikipedia" for t in TRICKY]:
        out = safe_title(raw, max_bytes=max_bytes, site=site, now=NOW)
        ctx = f"{raw!r} -> {out!r}"
        assert out, ctx
        assert _b(out) <= max_bytes, ctx
        assert title_problems(out, max_bytes=max_bytes) == [], ctx
        assert is_firmware_safe(out, max_bytes=max_bytes), ctx
        assert safe_title(out, max_bytes=max_bytes, now=NOW) == out, ctx  # idempotent
        assert unicodedata.normalize("NFC", out) == out, ctx
        # the exact firmware invariants, spelled out
        for ext in (".epub", ".txt"):
            assert fw_sanitize_preserving_extension(out + ext) == out + ext, ctx
            assert ".." not in out + ext, ctx
            assert fw_plugin_filename_ok(out + ext, "/Inbox"), ctx
        assert fw_sanitize_filename(out) == out, ctx  # 1.6.5 OPDS (title-only format)
        assert fw_sanitize_filename(out) + ".epub" == out + ".epub", ctx
        assert not ({"{", "}"} & set(out)), ctx
        assert out[0] not in " ." and out[-1] not in " .", ctx


def test_property_text_is_kept_when_possible():
    """Ordinary words survive: the result is never the fallback when there is text."""
    for raw in TRICKY:
        cleaned = "".join(ch for ch in raw if ch.isalnum() and ord(ch) < 0x10000)
        if len(cleaned) >= 3:
            out = safe_title(raw, now=NOW)
            assert out != fallback_title(NOW), (raw, out)


# --------------------------------------------------------------------------------------
# unique_title
# --------------------------------------------------------------------------------------


def test_unique_title_basics():
    assert unique_title("Hello", set()) == "Hello"
    assert unique_title("Hello", {"Hello"}) == "Hello (2)"
    assert unique_title("Hello", {"Hello", "Hello (2)"}) == "Hello (3)"
    assert unique_title("Hello", ["hello"]) == "Hello (2)"  # FAT is case-insensitive
    assert unique_title("Stra\u00dfe", {"STRASSE"}) == "Stra\u00dfe (2)"  # casefold superset
    assert unique_title("Wait... what?", {"Wait\u2026 what"}) == "Wait\u2026 what (2)"


def test_unique_title_passes_site_and_clock():
    assert unique_title("Story | The Guardian", {"Story"}, site="The Guardian") == "Story (2)"
    assert unique_title("", set(), now=NOW) == fallback_title(NOW)
    assert unique_title("", {fallback_title(NOW)}, now=NOW) == fallback_title(NOW) + " (2)"


def test_unique_title_continues_existing_counter():
    taken = {"Report", "Report (2)"}
    assert unique_title("Report (2)", taken) == "Report (3)"
    assert unique_title("Report (2)", {"Report (2)", "Report (3)"}) == "Report (4)"


def test_unique_title_fits_budget():
    long = "word " * 40
    first = safe_title(long)
    taken = {first}
    for n in range(2, 15):
        nxt = unique_title(long, taken)
        assert nxt.endswith(f" ({n})")
        assert _b(nxt) <= 90
        assert is_firmware_safe(nxt)
        assert nxt.casefold() not in {t.casefold() for t in taken}
        taken.add(nxt)


def test_unique_title_property():
    rnd = random.Random(7)
    taken: set[str] = set()
    pool = TRICKY[:40] + ["Same title"] * 30 + ["SAME TITLE"] * 5 + ["x" * 300] * 10
    rnd.shuffle(pool)
    for raw in pool:
        t = unique_title(raw, taken, max_bytes=90)
        assert is_firmware_safe(t), (raw, t)
        assert t.casefold() not in {x.casefold() for x in taken}
        taken.add(t)
    assert len(taken) == len(pool)
