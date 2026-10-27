"""Format-neutral document model shared by every extractor and renderer.

Extractors (HTML, pasted text, email, PDF) produce a :class:`Document`; renderers
(TXT, EPUB) consume one. Text inside blocks is already clean (see :func:`clean_text`):
NFC, single-spaced, no control characters, no characters the X3's built-in fonts
cannot draw.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Union


@dataclass(frozen=True)
class Heading:
    text: str
    level: int = 2  # 1..3; level 1 is reserved for the document title in renderers


@dataclass(frozen=True)
class Paragraph:
    text: str


@dataclass(frozen=True)
class ListItem:
    text: str
    ordered: bool = False
    index: int = 1  # 1-based position for ordered lists
    depth: int = 0  # nesting depth, 0 = top level


@dataclass(frozen=True)
class Quote:
    text: str


@dataclass(frozen=True)
class Preformatted:
    text: str  # may contain "\n"; renderers keep line structure


@dataclass(frozen=True)
class Rule:
    pass


Block = Union[Heading, Paragraph, ListItem, Quote, Preformatted, Rule]


@dataclass
class Document:
    title: str
    blocks: list[Block] = field(default_factory=list)
    byline: str = ""  # author(s), or sender for email
    site: str = ""  # site name or host, e.g. "The Guardian" / "example.com"
    source_url: str = ""
    published: str = ""  # ISO date "YYYY-MM-DD" when known
    lang: str = "en"
    notes: list[str] = field(default_factory=list)  # e.g. "Paywalled: only the preview was available"

    def word_count(self) -> int:
        n = 0
        for b in self.blocks:
            t = getattr(b, "text", "")
            n += len(t.split())
        return n

    def plain_text(self) -> str:
        return "\n\n".join(getattr(b, "text", "") for b in self.blocks if getattr(b, "text", ""))


# --------------------------------------------------------------------------------------
# Text cleaning shared by all extractors
# --------------------------------------------------------------------------------------

# Characters dropped outright: BOM, zero-width, soft hyphen, word joiner, bidi controls,
# variation selectors, object replacement.
_DROP_RE = re.compile(
    "[­​‌‍⁠﻿‎‏‪-‮⁦-⁩︀-️￼]"
)
# C0/C1 controls except \n and \t (handled separately).
_CTRL_RE = re.compile("[\u0000-\u0008\u000b-\u001f\u007f-\u009f]")
# BMP noncharacters (U+FDD0-FDEF, U+FFFE, U+FFFF; the supplementary-plane ones fall in
# the emoji range below) and lone surrogates (JSON "\ud800", surrogateescape decoding):
# the latter cannot even be encoded as UTF-8. Applied after NFC, which first joins
# valid surrogate pairs into the real character.
_NONCHAR_RE = re.compile("[\ud800-\udfff\ufdd0-\ufdef\ufffe\uffff]")

# Symbols outside the X3 built-in font coverage with readable ASCII stand-ins.
_SUBSTITUTES = {
    "™": "(TM)",
    "℠": "(SM)",
    "←": "<-",
    "→": "->",
    "↑": "^",
    "↓": "v",
    "↔": "<->",
    "⇒": "=>",
    "⇐": "<=",
    "⇔": "<=>",
    "≤": "<=",
    "≥": ">=",
    "≠": "!=",
    "≈": "~",
    "×": "x",  # covered by font, kept by map below (identity) - see _keep
    "∞": "infinity",
    "✓": "[x]",
    "✔": "[x]",
    "✗": "[ ]",
    "✘": "[ ]",
    "☐": "[ ]",
    "☑": "[x]",
    "☒": "[x]",
    "★": "*",
    "☆": "*",
    "●": "•",
    "▪": "•",
    "■": "•",
    "‣": "•",
    "⁃": "-",
    "⅓": "1/3",
    "⅔": "2/3",
    "⅛": "1/8",
    " ": " ",
    " ": " ",
    "\t": " ",
}
_SUBSTITUTES.pop("×")  # U+00D7 is inside U+00A0-017F, which the font covers

# Code point ranges the X3 built-in reader fonts cover (CrossPoint notoserif/notosans).
_FONT_RANGES = (
    (0x0020, 0x007E),
    (0x00A0, 0x017F),
    (0x01A0, 0x01A1),
    (0x01AF, 0x01B0),
    (0x01C4, 0x021F),
    (0x0300, 0x036F),
    (0x0400, 0x04FF),
    (0x1EA0, 0x1EF9),
    (0x2000, 0x2064),
    (0x2066, 0x2071),
    (0x2074, 0x208E),
    (0x2090, 0x209C),
    (0x20A0, 0x20C0),
    (0x2212, 0x2212),
    (0xFB00, 0xFB06),
)

_WS_RE = re.compile(r"[  ]{2,}")


def font_covers(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _FONT_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _is_emoji_or_symbol(ch: str) -> bool:
    cp = ord(ch)
    if cp >= 0x1F000:  # emoji, pictographs, flags, symbols & pictographs extended
        return True
    if 0x2600 <= cp <= 0x27BF:  # misc symbols, dingbats
        return True
    if 0x2B00 <= cp <= 0x2BFF:  # misc symbols and arrows
        return True
    if cp in (0x200D, 0x20E3):
        return True
    return False


def clean_text(s: str, *, keep_newlines: bool = False) -> str:
    """Normalize a run of text for the X3.

    - NFC, drop invisible/bidi/control characters, noncharacters and lone surrogates
      (so the result always encodes as UTF-8), map unsupported symbols to ASCII,
      drop emoji. Scripts the font does not cover (Greek, CJK, ...) are kept: they
      render as boxes unless the user installed an SD-card font, which is better than
      silently losing text.
    - Collapses runs of spaces; trims. With ``keep_newlines`` newlines and indentation
      survive (for preformatted text: tabs become 4 spaces, only trailing spaces and
      leading/trailing blank lines are removed); otherwise all whitespace collapses to
      single spaces.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    s = _NONCHAR_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if keep_newlines:
        s = s.expandtabs(4)
    s = _DROP_RE.sub("", s)
    out = []
    for ch in s:
        if ch == "\n":
            out.append("\n" if keep_newlines else " ")
            continue
        sub = _SUBSTITUTES.get(ch)
        if sub is not None:
            out.append(sub)
            continue
        if _CTRL_RE.match(ch):
            continue
        if _is_emoji_or_symbol(ch):
            continue
        out.append(ch)
    s = "".join(out)
    if keep_newlines:
        lines = [ln.rstrip() for ln in s.split("\n")]
        # strip leading/trailing blank lines
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)
    s = re.sub(r"\s+", " ", s)
    return s.strip()
