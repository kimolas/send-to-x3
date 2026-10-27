"""Repair ligature glyphs whose ToUnicode entry is wrong.

Seen in the wild: US Supreme Court preliminary prints map the 'fi'/'fl' ligature glyphs
to a plain 'f', so every extractor (MuPDF, pdfminer, PDFium, poppler, pypdf) prints
"fled" for "filed" and "fow" for "flow". The broken glyph is detectable: it is mapped
to 'f' but its advance is ~1.4x that of the real 'f' in the same font. PDFium exposes
no glyph ids, so glyphs are told apart by their advance width relative to the font
size (each glyph has one fixed advance in the font's width table). Which ligature it
is gets decided from the letters that follow it and from word stems.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

# P(next letter | ligature) - rough English estimates; unknown letters get a floor.
NEXT = {
    "fi": dict(n=.26, r=.12, c=.12, e=.09, l=.08, g=.05, x=.04, s=.04, t=.03, v=.03, f=.02, d=.02, a=.02, b=.01, z=.01),
    "fl": dict(o=.22, e=.18, u=.14, a=.14, i=.10, y=.06, r=.01),
    "ff": dict(e=.30, i=.25, o=.12, a=.08, l=.06, s=.04, u=.04, y=.02),
    "ffi": dict(c=.40, e=.10, d=.10, n=.10, r=.10, x=.05),
    "ffl": dict(e=.50, u=.20, i=.10, a=.10),
}

# word stems that disambiguate fi / fl / ff / ffi / ffl (matched as substrings)
STEMS = {
    "fi": "first find field file final financ figur fine fit fix fifth fifty fiction confirm affirm office "
          "specific signific benefit profit confiden defin infinit identif classif justif modif notif "
          "certif satisf verif qualif clarif artific scientif fiscal filed filing fill film filter fire firm "
          "fish fiber fierce fight",
    "fl": "flow flood floor flight flat fluid flex flag flame flash flaw fleet flesh flip float flock flour "
          "flu fly reflect influen conflict inflat deflect affluen flawed fledg flee",
    "ff": "effect affect afford effort offer differ suffer staff stuff traffic offic offen coffee buff "
          "cliff tariff sheriff plaintiff bailiff scaff diffus suffic",
    "ffi": "office offic suffic effici affirm affili affix diffic traffick",
    "ffl": "affluen baffl ruffl shuffl waffl scuffl",
}

PREFIXES = ("", "re", "in", "un", "de", "dis", "con", "pre", "non", "mis", "over", "under", "sub", "self", "self-")

# width bins: advance / font size, in steps of 0.02 em
BIN = 0.02


def width_key(font: str, advance: float, size: float):
    if size <= 0:
        return None
    return (font, round(advance / size / BIN))


WORD = re.compile(r"[a-z]+")


def candidates(ratio: float) -> tuple:
    return ("ffi", "ffl") if ratio > 2.1 else ("fi", "fl", "ff")


def _stem_hit(word: str, c: str) -> bool:
    """The word starts with a known stem (after a common prefix)."""
    return any(word.startswith(px + st) for st in STEMS[c].split() for px in PREFIXES)


def _stem_inside(word: str, c: str) -> bool:
    """Weaker evidence: a stem anywhere in the word (compounds like 'factfinder')."""
    return any(st in word for st in STEMS[c].split())


class Collector:
    """Statistics about single 'f' glyphs, gathered while the pages are extracted."""

    def __init__(self, max_pages: int = 40):
        self.max_pages = max_pages
        self.count = Counter()  # key -> occurrences
        self.nexts = defaultdict(Counter)  # key -> next letter counts
        self.words = defaultdict(list)  # key -> [(prefix, suffix)]
        self.vocab = Counter()  # words of the document (lowercase letters only)
        self.decided: dict = {}

    def add(self, key, prefix: str, suffix: str) -> None:
        self.count[key] += 1
        self.nexts[key][suffix[:1].lower() or " "] += 1
        if len(self.words[key]) < 400:
            self.words[key].append((prefix.lower(), suffix.lower()))

    def add_text(self, text: str) -> None:
        for w in WORD.findall(text.lower()):
            self.vocab[w] += 1

    def decide(self) -> dict:
        """Return {key: (default expansion, candidates)} for broken ligature glyphs."""
        by_font = defaultdict(list)
        for key, n in self.count.items():
            by_font[key[0]].append((n, key[1], key))
        out = {}
        for font, glyphs in by_font.items():
            glyphs.sort(reverse=True)
            n_main, w_main, _ = glyphs[0]
            if w_main <= 0 or n_main < 3:
                continue
            for n, w, key in glyphs[1:]:
                ratio = w / w_main
                if ratio < 1.25 or n < 2:
                    continue
                cands = candidates(ratio)
                nx = self.nexts[key]
                score = {c: sum(k * math.log(NEXT[c].get(ch, 0.003)) for ch, k in nx.items()) for c in cands}
                hits = {}
                for c in cands:  # dictionary evidence dominates the letter statistics
                    hits[c] = sum(_stem_hit(pre + c + post, c) for pre, post in self.words[key])
                    score[c] += 6.0 * hits[c]
                best = max(score, key=score.get)
                if sum(hits.values()) >= 1:  # a word stem must confirm it: a wide 'f' alone is no proof
                    out[key] = (best, cands)
        self.decided = out
        return out

    def choose(self, key, prefix: str, suffix: str) -> str | None:
        """Expansion for one occurrence: glyphs of different ligatures can share an
        advance width (Century Expanded's fi and fl do), so each word is decided on its
        own from the document's vocabulary, word stems and letter statistics."""
        info = self.decided.get(key)
        if info is None:
            return None
        default, cands = info
        nxt = suffix[:1].lower() or " "
        best, best_score = default, None
        for c in cands:
            word = (prefix + c + suffix).lower()
            sc = math.log(NEXT[c].get(nxt, 0.003))
            if self.vocab.get(word):
                sc += 8.0
            if _stem_hit(word, c):
                sc += 6.0
            elif _stem_inside(word, c):
                sc += 2.0
            if c == default:
                sc += 1.5
            if best_score is None or sc > best_score:
                best, best_score = c, sc
        return best
