"""Pasted / shared plain text -> :class:`~sendtox3.textmodel.Document`.

Also holds the share-sheet helpers used by ingest: :func:`classify` (is this a link to
fetch or a note to keep?), :func:`first_url` and :func:`derive_title`, plus the small
script/stop-word language guesser shared with the HTML extractor.

Paragraphing rules (§5, §7.1, extraction report §9):

- Blank lines always separate paragraphs.
- A single newline separates paragraphs too (phone selections and chat apps put one
  paragraph per line), *unless* the text looks hard-wrapped (most lines 55-100
  characters, as in email, Gutenberg or RFC text): then wrapped lines are rejoined and
  a paragraph also ends at an indented line or a short line ending a sentence.
- ``-``/``*``/``•`` lines become unordered :class:`ListItem`; ``1.``/``1)`` lines become
  ordered ones. Two spaces (or a tab) of indentation add one nesting level.
- ``>`` lines become :class:`Quote`; fenced code (```` ``` ````) becomes
  :class:`Preformatted`; ``---``/``* * *`` lines become :class:`Rule`.
- Only *clearly marked* headings become :class:`Heading`: Markdown ``#`` lines,
  setext underlines and wiki ``== x ==`` lines. A short line that merely looks like a
  heading stays a paragraph.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from ..textmodel import (
    Block,
    Document,
    Heading,
    ListItem,
    Paragraph,
    Preformatted,
    Quote,
    Rule,
    clean_text,
)

__all__ = [
    "TITLE_MAX_CHARS",
    "classify",
    "clean_preformatted",
    "derive_title",
    "drop_empty_sections",
    "find_urls",
    "first_url",
    "guess_lang",
    "looks_hard_wrapped",
    "normalize_heading_levels",
    "text_to_blocks",
    "text_to_document",
]

TITLE_MAX_CHARS = 80

# --------------------------------------------------------------------------------------
# URLs in free text
# --------------------------------------------------------------------------------------

# http(s) URLs, or bare "www." hosts. Stops at whitespace, quotes and angle brackets;
# trailing punctuation and unbalanced closing brackets are trimmed afterwards.
_URL_RE = re.compile(r"""(?i)\b(?:https?://|www\.)[^\s<>"'`\u00a0\u3000]+""")
_TRAILING_PUNCT = ".,;:!?'\"*_~…»”’。、，！？"
_PAIRS = {")": "(", "]": "[", "}": "{", ">": "<"}


def _trim_url(u: str) -> str:
    """Trim sentence punctuation and unbalanced closing brackets from a URL match.

    Linear in the length of the match: bracket counts are taken once and updated as
    characters are trimmed (re-counting and re-slicing per character was quadratic,
    seconds of GIL time for a URL followed by thousands of ")").
    """
    counts = {c: u.count(c) for c in (*_PAIRS, *_PAIRS.values())}
    end = len(u)
    while end:
        last = u[end - 1]
        if last in _TRAILING_PUNCT:
            end -= 1
            continue
        if last in _PAIRS and counts[last] > counts[_PAIRS[last]]:
            counts[last] -= 1
            end -= 1
            continue
        break
    u = u[:end]
    # Markdown "[label](url)" where the label itself was a URL: "url](url" -> "url"
    if "](" in u:
        u = u.split("](", 1)[0]
    return u


def _iter_urls(text: str) -> Iterator[str]:
    for m in _URL_RE.finditer(text or ""):
        u = _trim_url(m.group(0))
        if not u:
            continue
        if u.lower().startswith("www."):
            if "." not in u[4:]:
                continue
            u = "https://" + u
        try:
            if not urlsplit(u).hostname:
                continue
        except ValueError:  # e.g. "http://[oops/": not a usable URL
            continue
        yield u


def find_urls(text: str) -> list[str]:
    """All http(s) URLs in ``text`` in order of appearance, without duplicates.

    Bare ``www.`` hosts get an ``https://`` scheme. URLs are trimmed but not cleaned
    (see :func:`sendtox3.extract.fetch.clean_url`).
    """
    return list(dict.fromkeys(_iter_urls(text)))  # ordered de-duplication in linear time


def first_url(text: str) -> str:
    """The first http(s) URL in ``text`` (trimmed of trailing punctuation), or ``""``."""
    return next(_iter_urls(text), "")


# --------------------------------------------------------------------------------------
# Classification (§7.1)
# --------------------------------------------------------------------------------------

# Filler that share sheets and people put around a link; a body consisting only of
# URLs and filler is "just a link".
_FILLER_RE = re.compile(
    r"(?i)\b(?:check\s+(?:this|it)\s+out|read\s+(?:this\s+)?later|shared?\s+(?:from|via)\s+\S+(?:\s+app)?|"
    r"sent\s+from\s+my\s+\S+|via|fwd?|fw|link|url|here|this|look)\b"
)


def _norm_for_match(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"[\W_]+", " ", s)
    return " ".join(s.split())


def classify(text: str, url: str = "", title: str = "") -> Literal["url", "text"]:
    """Decide whether a share is a link to fetch (``"url"``) or text to keep (``"text"``).

    Per §7.1:

    - a body that is a single URL (possibly in ``<>``/quotes, or with filler such as
      "via ..." or "Sent from my iPhone"), or an explicit ``url`` with no text -> ``url``
      (a body with several different URLs stays ``text``);
    - a URL plus short text that is part of the page ``title`` (or that is the title
      plus a few words, as apps share "Title - Site https://...") -> ``url``;
    - anything else -> ``text`` (callers keep :func:`first_url` as ``source_url``).

    ``title`` is the share-sheet title or, after fetching, the page title.
    """
    body = (text or "").strip()
    if not body:
        return "url" if (url or "").strip() else "text"
    urls = find_urls(body)
    if not urls and not (url or "").strip():
        return "text"
    if len(urls) > 1:
        return "text"  # several links: fetching one would silently drop the others
    rest = _URL_RE.sub(" ", body)
    rest_plain = _norm_for_match(rest)
    if not _norm_for_match(_FILLER_RE.sub(" ", rest)):
        return "url"
    title_n = _norm_for_match(title or "")
    if not title_n:
        return "text"
    if rest_plain in title_n:
        return "url"
    if len(title_n) >= 12 and title_n in rest_plain and len(rest_plain.split()) <= len(title_n.split()) + 8:
        return "url"
    return "text"


# --------------------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------------------

_MARKER_RE = re.compile(r"^\s*(?:#{1,6}\s+|>+\s?|[-*+•◦‣▪●–]\s+|\d{1,3}[.)]\s+|={2,6}\s*)")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])[\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9À-ÖØ-ÞА-ЯЁ])|(?<=[。！？])")


def _shorten(s: str, limit: int = TITLE_MAX_CHARS) -> str:
    """Cut ``s`` to ``limit`` characters at a word boundary, appending an ellipsis."""
    if len(s) <= limit:
        return s
    cut = s[: limit - 1]
    sp = cut.rfind(" ")
    if sp >= limit // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-–—") + "…"


def _strip_closing_equals(line: str) -> str:
    """``re.sub(r"\\s*={2,6}\\s*$", "", line)`` (a wiki heading's closing run) in linear
    time: that search is quadratic on a line with a long inner run of spaces."""
    s = line.rstrip()
    body = s.rstrip("=")
    run = len(s) - len(body)
    if run < 2:
        return line
    if run > 6:
        return s[:-6]
    return body.rstrip()


def derive_title(text: str) -> str:
    """A title for a note: its first meaningful line, or that line's first sentence.

    Lines that are only URLs or only markup are skipped; Markdown/list/quote markers are
    removed; the result is clean (see :func:`clean_text`) and at most 80 characters,
    cut at a word boundary with "…" when needed. Returns ``""`` when nothing usable.
    """
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _URL_RE.sub(" ", raw)
        line = _MARKER_RE.sub("", line)
        line = _strip_closing_equals(line)
        line = clean_text(line).strip(" -–—:|")
        if not line or not re.search(r"\w", line):
            continue
        if len(line) <= TITLE_MAX_CHARS:
            return line
        parts = _SENTENCE_END_RE.split(line, maxsplit=1)
        first = parts[0].strip()
        if 12 <= len(first) <= TITLE_MAX_CHARS:
            return first
        return _shorten(line)
    return ""


# --------------------------------------------------------------------------------------
# Language guess (used when no lang attribute / header is available)
# --------------------------------------------------------------------------------------

_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the and of to is in that it was for with as on are this be not have".split()),
    "de": frozenset("der die und das ist nicht ein eine zu den mit sich auch auf dem für von".split()),
    "fr": frozenset("le la les et des est une pas que dans pour qui sur au avec du il elle".split()),
    "es": frozenset("el la los las y que es en del por una con para se lo como más pero".split()),
    "it": frozenset("il che di la non per una sono del gli della è le con si anche come".split()),
    "pt": frozenset("o a que de não uma os para com do da em é no se mais as dos".split()),
    "nl": frozenset("de het een en van is dat op te zijn niet met voor ook maar".split()),
    "pl": frozenset("i w nie się na jest to że z do jak ale co tak od po".split()),
    "sv": frozenset("och att det som är en på inte för med av jag har till om".split()),
    "fi": frozenset("ja on ei se että oli hän mutta kun ovat joka myös kuin tai".split()),
}


def _script_counts(sample: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ch in sample:
        cp = ord(ch)
        if cp < 0x80:
            if ch.isalpha():
                counts["latin"] = counts.get("latin", 0) + 1
            continue
        if not ch.isalpha():
            continue
        if cp <= 0x024F or 0x1E00 <= cp <= 0x1EFF:
            key = "latin"
        elif 0x0370 <= cp <= 0x03FF:
            key = "greek"
        elif 0x0400 <= cp <= 0x052F:
            key = "cyrillic"
        elif 0x0590 <= cp <= 0x05FF:
            key = "hebrew"
        elif 0x0600 <= cp <= 0x06FF:
            key = "arabic"
        elif 0x0900 <= cp <= 0x097F:
            key = "devanagari"
        elif 0x0E00 <= cp <= 0x0E7F:
            key = "thai"
        elif 0x3040 <= cp <= 0x30FF:
            key = "kana"
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            key = "hangul"
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            key = "han"
        else:
            key = "other"
        counts[key] = counts.get(key, 0) + 1
    return counts


def guess_lang(text: str, default: str = "en") -> str:
    """Best-effort ISO 639-1 code for ``text``.

    Non-Latin scripts are recognised by code-point ranges (Cyrillic is split into
    ru/uk/sr by their distinctive letters). Latin-script text is matched against short
    stop-word lists for the languages CrossPoint hyphenates (plus Dutch); without a
    clear winner ``default`` is returned.
    """
    sample = (text or "")[:6000]
    counts = _script_counts(sample)
    letters = sum(counts.values())
    if letters < 20:
        return default
    script, n = max(counts.items(), key=lambda kv: kv[1])
    if script in ("han", "kana") or counts.get("kana", 0) > letters * 0.05:
        return "ja" if counts.get("kana", 0) else "zh"
    if n < letters * 0.4:
        return default
    simple = {"greek": "el", "hebrew": "he", "arabic": "ar", "devanagari": "hi", "thai": "th", "hangul": "ko"}
    if script in simple:
        return simple[script]
    if script == "cyrillic":
        low = sample.lower()
        uk = sum(low.count(c) for c in "іїєґ")
        sr = sum(low.count(c) for c in "ђћџљњј")
        ru = sum(low.count(c) for c in "ыэъё")
        if sr > max(uk, ru):
            return "sr"
        if uk > ru:
            return "uk"
        return "ru"
    if script != "latin":
        return default
    words = re.findall(r"[^\W\d_]+", sample.lower())
    if len(words) < 12:
        return default
    scores = {lang: sum(1 for w in words if w in sw) for lang, sw in _STOPWORDS.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_n = ranked[0]
    second_n = ranked[1][1]
    if best_n >= max(3, len(words) * 0.04) and best_n >= second_n * 1.3:
        return best
    return default


# --------------------------------------------------------------------------------------
# Plain text -> blocks
# --------------------------------------------------------------------------------------

_BULLET_RE = re.compile(r"^(?P<indent>[ ]*)(?P<mark>[-*+•◦‣▪●–])[ ]+(?P<text>\S.*)$")
_NUMBER_RE = re.compile(r"^(?P<indent>[ ]*)\(?(?P<num>\d{1,3})[.)][ ]+(?P<text>\S.*)$")
# Heading patterns are deliberately simple (linear): the closing "###"/"==" run is
# checked in Python. A lazy text group followed by an optional closing run, as in
# "(?P<text>.+?)(?:[ ]+#+)?[ ]*$", backtracks quadratically on a line with a long run
# of inner spaces (16 KB took 2 s, holding the GIL).
_ATX_RE = re.compile(r"^[ ]{0,3}(?P<hashes>#{1,6})[ ]+(?P<text>.*)$")
_WIKI_HEAD_RE = re.compile(r"^[ ]*(?P<eq>={2,6})(?P<rest>.*)$")
_RULE_RE = re.compile(r"^[ ]*(?:([-*_=—–•·~])[ ]?)\1(?:[ ]?\1)+[ ]*$")
_SETEXT_RE = re.compile(r"^[ ]{0,3}(=+|-+)[ ]*$")
_FENCE_RE = re.compile(r"^[ ]{0,3}(```+|~~~+)")
_QUOTE_RE = re.compile(r"^[ ]{0,3}>[ ]?(?P<text>.*)$")
_SENTENCE_FINAL = tuple(".!?:;\"'”’)»…。！？")


def _atx_heading(line: str) -> tuple[int, str] | None:
    """``(level, text)`` for a Markdown ``# heading`` line (right-stripped), else ``None``.

    A closing run of ``#`` is removed when a space precedes it ("# C#" keeps its "#").
    """
    m = _ATX_RE.match(line)
    if not m:
        return None
    text = m.group("text").rstrip(" ")
    body = text.rstrip("#")
    if body != text and body.endswith(" "):
        text = body.rstrip(" ")
    return len(m.group("hashes")), text


def _wiki_heading(line: str) -> tuple[int, str] | None:
    """``(level, text)`` for a wiki ``== heading ==`` line (right-stripped), else ``None``.

    The opening run (2-6 ``=``) must be closed by the same run at the end of the line;
    the text may be empty (the line is then dropped, not kept as a paragraph).
    """
    m = _WIKI_HEAD_RE.match(line)
    if not m:
        return None
    eq, rest = m.group("eq"), m.group("rest").rstrip(" ")
    if not rest.endswith(eq):
        return None
    mid = rest[: len(rest) - len(eq)]
    if not mid or mid[0] == "=":
        return None
    return len(eq), mid.strip(" ")


def clean_preformatted(text: str) -> str:
    """Clean code/preformatted text but keep its layout.

    Like ``clean_text(keep_newlines=True)`` (NFC, no controls/emoji, font
    substitutions, tabs -> 4 spaces, leading/trailing blank lines removed), except that
    leading indentation and runs of spaces inside a line are preserved: they carry
    the meaning of code and aligned text.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    lines: list[str] = []
    for line in text.split("\n"):
        pieces = re.split(r"( {2,})", line.rstrip())
        out = []
        for piece in pieces:
            if piece.startswith("  ") and not piece.strip():
                out.append(piece)
                continue
            lead = len(piece) - len(piece.lstrip(" "))
            trail = len(piece) - len(piece.rstrip(" "))
            body = clean_text(piece)
            out.append(" " * lead + body + (" " * trail if body else ""))
        lines.append("".join(out).rstrip())
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _normalize_newlines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2029", "\n\n").replace("\u2028", "\n").replace("\x0c", "\n\n").replace("\x0b", "\n")
    text = text.lstrip("\ufeff")
    return text.expandtabs(4)


def looks_hard_wrapped(text: str) -> bool:
    """True if lines were wrapped at a fixed column (email, Gutenberg, RFCs).

    Looks at lines that are followed by another non-blank line (the last line of a
    paragraph is naturally short): wrapped text has most of them 55-100 characters
    long and close to the same width. One-paragraph-per-line text (phone selections)
    has much longer lines; lists and poetry much shorter ones.
    """
    lines = [ln.rstrip() for ln in _normalize_newlines(text or "").split("\n")]
    body = [ln for i, ln in enumerate(lines) if ln.strip() and i + 1 < len(lines) and lines[i + 1].strip()]
    if len(body) < 3:
        return False
    lens = sorted(len(ln) for ln in body)
    p80 = lens[min(len(lens) - 1, int(len(lens) * 0.8))]
    if not 55 <= p80 <= 100:
        return False
    near = sum(1 for n in lens if n >= p80 - 15)
    return near >= 0.6 * len(lens)


def _wrap_width(lines: list[str]) -> int:
    lens = sorted(len(ln.rstrip()) for ln in lines if ln.strip())
    if not lens:
        return 0
    return lens[min(len(lens) - 1, int(len(lens) * 0.8))]


@dataclass
class _Pending:
    """A paragraph-like block being assembled from one or more lines."""

    kind: Literal["p", "li", "quote"]
    parts: list[str]
    ordered: bool = False
    index: int = 1
    depth: int = 0


def _depth(indent: str) -> int:
    return min(len(indent) // 2, 6)


def normalize_heading_levels(blocks: list[Block]) -> list[Block]:
    """Map raw heading levels (1..6, as found in the source) onto 2..3.

    Level 1 is reserved for the document title in renderers, so the shallowest
    heading level used in the body becomes 2, the next one 3, anything deeper 3.
    """
    levels = sorted({b.level for b in blocks if isinstance(b, Heading)})
    if not levels:
        return blocks
    mapping = {lvl: min(3, 2 + i) for i, lvl in enumerate(levels)}
    return [Heading(text=b.text, level=mapping[b.level]) if isinstance(b, Heading) else b for b in blocks]


def drop_empty_sections(blocks: list[Block]) -> list[Block]:
    """Remove headings with no content before the next heading of the same or higher rank.

    Works back to front, so a parent whose only sub-section was empty goes too.
    """
    kept: list[Block] = []  # reversed
    for b in reversed(blocks):
        if isinstance(b, Heading):
            nxt = kept[-1] if kept else None
            if nxt is None or (isinstance(nxt, Heading) and nxt.level <= b.level):
                continue
        kept.append(b)
    kept.reverse()
    return kept


def text_to_blocks(text: str, *, hard_wrapped: bool | None = None) -> list[Block]:
    """Split plain text into blocks (see the module docstring for the rules).

    ``hard_wrapped`` forces the wrapped/unwrapped interpretation; ``None`` detects it
    with :func:`looks_hard_wrapped`. Heading levels are raw (1..6); pass the result
    through :func:`normalize_heading_levels` before putting it in a Document.
    """
    text = _normalize_newlines(text or "")
    lines = text.split("\n")
    wrapped = looks_hard_wrapped(text) if hard_wrapped is None else hard_wrapped
    width = _wrap_width(lines) if wrapped else 0
    short_line = int(width * 0.6)

    blocks: list[Block] = []
    pending: _Pending | None = None
    counters: dict[int, int] = {}  # ordered list depth -> last index (for "1." repeated)

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        t = clean_text(" ".join(pending.parts))
        if t:
            if pending.kind == "li":
                blocks.append(ListItem(text=t, ordered=pending.ordered, index=pending.index, depth=pending.depth))
            elif pending.kind == "quote":
                blocks.append(Quote(text=t))
            else:
                blocks.append(Paragraph(text=t))
        pending = None

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            flush()
            counters_reset = i + 1 < n and not (_BULLET_RE.match(lines[i + 1]) or _NUMBER_RE.match(lines[i + 1]))
            if counters_reset:
                counters.clear()
            i += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            flush()
            marker = fence.group(1)
            j = i + 1
            code: list[str] = []
            while j < n and not lines[j].strip().startswith(marker[:3]):
                code.append(lines[j])
                j += 1
            body = clean_preformatted("\n".join(code))
            if body:
                blocks.append(Preformatted(text=body))
            i = j + 1
            continue

        head = _atx_heading(line) or _wiki_heading(line)
        if head:
            flush()
            t = clean_text(head[1])
            if t:
                blocks.append(Heading(text=t, level=head[0]))
            i += 1
            continue

        # Setext heading: a lone short line underlined with === or --- of similar length.
        if (
            pending is None
            and i + 1 < n
            and _SETEXT_RE.match(lines[i + 1])
            and len(stripped) <= TITLE_MAX_CHARS
            and not _BULLET_RE.match(line)
            and not _QUOTE_RE.match(line)
        ):
            under = lines[i + 1].strip()
            similar = 0.5 * len(stripped) <= len(under) <= 1.5 * len(stripped) + 2
            if (under.startswith("=") and len(under) >= 3) or (under.startswith("-") and len(under) >= 3 and similar):
                blocks.append(Heading(text=clean_text(stripped), level=1 if under.startswith("=") else 2))
                i += 2
                continue

        if _RULE_RE.match(line):
            flush()
            blocks.append(Rule())
            i += 1
            continue

        q = _QUOTE_RE.match(line)
        if q:
            flush()
            quoted: list[str] = []
            while i < n and (qm := _QUOTE_RE.match(lines[i].rstrip())):
                quoted.append(qm.group("text"))
                i += 1
            for b in text_to_blocks("\n".join(quoted)):
                if isinstance(b, ListItem):
                    mark = f"{b.index}." if b.ordered else "•"
                    blocks.append(Quote(text=f"{mark} {b.text}"))
                elif isinstance(b, (Paragraph, Heading, Quote)):
                    blocks.append(Quote(text=b.text))
                else:
                    blocks.append(b)
            continue

        bm = _BULLET_RE.match(line)
        nm = None if bm else _NUMBER_RE.match(line)
        mm = bm or nm
        if mm is not None:
            flush()
            depth = _depth(mm.group("indent"))
            if nm:
                num = int(nm.group("num"))
                prev = counters.get(depth)
                # "1. 1. 1." (Markdown auto-numbering) -> 1, 2, 3
                index = prev + 1 if prev is not None and num <= prev else num
                counters[depth] = index
                for d in [d for d in counters if d > depth]:
                    del counters[d]
                pending = _Pending("li", [mm.group("text")], ordered=True, index=index, depth=depth)
            else:
                pending = _Pending("li", [mm.group("text")], depth=depth)
            if not wrapped:
                flush()
            i += 1
            continue

        # Ordinary text line.
        indent = len(line) - len(line.lstrip(" "))
        if pending is not None and wrapped:
            if pending.kind == "li" and indent > 0:
                pending.parts.append(stripped)  # continuation of a wrapped list item
            elif pending.kind == "li":
                flush()
                pending = _Pending("p", [stripped])
            elif indent >= 2 and not lines[i - 1].startswith(" " * indent):
                # lib.ru / Gutenberg style: an indented line starts a new paragraph
                flush()
                pending = _Pending("p", [stripped])
            else:
                pending.parts.append(stripped)
        else:
            flush()
            pending = _Pending("p", [stripped])
        if not wrapped:
            flush()
        elif short_line and len(stripped) + indent < short_line and stripped.endswith(_SENTENCE_FINAL):
            # A short line that ends a sentence ends the paragraph in wrapped text.
            flush()
        i += 1

    flush()
    return blocks


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def text_to_document(text: str, *, title: str = "", source_url: str = "") -> Document:
    """Turn pasted/shared text into a Document.

    The title is ``title`` if given, else :func:`derive_title`. When the first block
    merely repeats the title (a heading or title line) and more content follows, it is
    dropped so renderers do not print it twice.
    """
    blocks = text_to_blocks(text or "")
    clean_title = clean_text(title)
    if not clean_title:
        first = blocks[0] if blocks else None
        if isinstance(first, Heading) and len(first.text) <= TITLE_MAX_CHARS:
            clean_title = first.text
        else:
            clean_title = derive_title(text or "")
    if len(blocks) > 1 and isinstance(blocks[0], (Heading, Paragraph)):
        if _norm_for_match(blocks[0].text) == _norm_for_match(clean_title):
            blocks = blocks[1:]
    blocks = normalize_heading_levels(blocks)
    plain = "\n".join(getattr(b, "text", "") for b in blocks)
    return Document(
        title=clean_title,
        blocks=blocks,
        site=_host(source_url),
        source_url=source_url or "",
        lang=guess_lang(plain or clean_title),
    )
