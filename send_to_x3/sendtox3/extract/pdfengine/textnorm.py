"""Unicode clean-up for text pulled out of PDFs.

We deliberately do NOT apply blanket NFKC: it would turn superscripts into digits,
'½' into '1/2', '™' into 'TM', etc.  Instead we expand ligatures and strip
characters that are invalid in XHTML or meaningless on an e-ink reader.
"""
import re
import unicodedata

LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "ﬅ": "st", "ﬆ": "st", "Ĳ": "IJ", "ĳ": "ij",
    "Ꜳ": "AA", "ꜳ": "aa",
}
# Symbol / Wingdings private-use bullets commonly emitted by Word & PowerPoint
PUA_BULLETS = {
    "": "•", "": "▪", "": "❖", "": "➢", "": "✓",
    "": "□", "": "■", "": "→", "": "➔", "": "–",
}
SPACES = dict.fromkeys(map(ord, "           "
                                "   　\t"), " ")
DROP = dict.fromkeys(map(ord, "​‌‍⁠﻿￾￼"), None)
HYPHENS = dict.fromkeys(map(ord, "‐‑"), "-")
# XML 1.0 forbids most C0 controls; also drop C1 controls and lone surrogates.
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_PUA = re.compile("[-]")

TABLE = {}
for _d in (SPACES, DROP, HYPHENS):
    TABLE.update(_d)
TABLE.update({ord(k): v for k, v in LIGATURES.items()})
TABLE.update({ord(k): v for k, v in PUA_BULLETS.items()})


def clean(text: str) -> str:
    """Normalise one span of text. Soft hyphens are kept (handled by dehyphenation)."""
    if not text:
        return ""
    t = text.translate(TABLE)
    t = _BAD_XML.sub("", t)
    t = _PUA.sub("", t)           # unknown symbol-font glyphs: drop rather than show tofu
    return unicodedata.normalize("NFC", t)


def final(text: str) -> str:
    """Last pass on a joined string: remove leftover soft hyphens, squeeze spaces."""
    t = text.replace("­", "")
    return re.sub(r"[ ]{2,}", " ", t).strip()


def garbage_ratio(text: str) -> float:
    """Share of characters that indicate a broken text layer (U+FFFD, PUA, (cid:N))."""
    if not text:
        return 0.0
    bad = text.count("�") + len(_PUA.findall(text)) + 5 * text.count("(cid:")
    return bad / max(1, len(text))
