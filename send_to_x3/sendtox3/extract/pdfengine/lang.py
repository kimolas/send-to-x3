"""Cheap language guess for PDFs without a /Lang entry: the writing system first, then
(for Latin script) a handful of very frequent function words. The X3 uses the
language for hyphenation, so a rough guess beats a wrong "en"."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

SCRIPT_LANG = {
    "CYRILLIC": "ru", "GREEK": "el", "HEBREW": "he", "ARABIC": "ar", "DEVANAGARI": "hi",
    "THAI": "th", "HANGUL": "ko", "HIRAGANA": "ja", "KATAKANA": "ja", "CJK": "zh",
    "ARMENIAN": "hy", "GEORGIAN": "ka", "BENGALI": "bn", "TAMIL": "ta",
}

STOPWORDS = {
    "en": "the and of to in is that for with as are this on be by it from which",
    "de": "der die und das ist nicht mit den von zu ein eine auch sich auf für dem",
    "fr": "le la les et des est une que pour dans du pas qui sur au avec",
    "es": "el la los las que de y en un una por para con es del se",
    "it": "il la che di e un una per con non sono del della gli le",
    "pt": "o a os as que de e do da em um uma para com não é",
    "nl": "de het een en van is dat op te voor niet met zijn er",
    "sv": "och att det som en är på för av med inte till den har",
    "pl": "i w na nie się że jest do to z jak co po od",
}
_SW = {lang: set(words.split()) for lang, words in STOPWORDS.items()}
_WORD = re.compile(r"[^\W\d_]+")


def _script(ch: str) -> str:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return ""
    if name.startswith("CJK"):
        return "CJK"
    return name.split(" ", 1)[0]


def guess_lang(text: str, default: str = "en") -> str:
    sample = text[:20000]
    scripts = Counter(_script(c) for c in sample if c.isalpha())
    if not scripts:
        return default
    total = sum(scripts.values())
    if scripts.get("HIRAGANA", 0) + scripts.get("KATAKANA", 0) > 0.05 * total:
        return "ja"
    top, n = scripts.most_common(1)[0]
    if top != "LATIN" and n > 0.3 * total and top in SCRIPT_LANG:
        lang = SCRIPT_LANG[top]
        if lang == "ru" and re.search("[іїєґ]", sample.lower()):
            return "uk"
        return lang
    words = [w.lower() for w in _WORD.findall(sample)]
    if len(words) < 8:
        return default
    hits = {lang: sum(w in sw for w in words) for lang, sw in _SW.items()}
    best = max(hits, key=hits.get)
    if hits[best] < 0.05 * len(words):
        return default
    return best
