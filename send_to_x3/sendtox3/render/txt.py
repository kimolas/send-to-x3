"""Document -> ``.txt`` bytes laid out for CrossPoint's TXT reader (ARCHITECTURE §6.1).

CrossPoint turns every ``\\n`` into a hard line break, has no paragraph spacing, wraps
only at ASCII spaces, draws tabs/CR/BOM as replacement boxes, and its wrap cost grows
with the square of the line length. So: one paragraph per line, a blank line between
blocks, plain UTF-8 without BOM, ``\\n`` only, and no line longer than 900 bytes.

The small text helpers at the bottom (``inline_text``, ``pre_lines``, ``host_of``,
``iso_date``, ``meta_parts``) are shared with :mod:`sendtox3.render.epub`.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from ..textmodel import Document, Heading, ListItem, Paragraph, Preformatted, Quote, Rule, clean_text

__all__ = [
    "MAX_LINE_BYTES",
    "render_txt",
    "split_long_line",
    "inline_text",
    "pre_lines",
    "clean_url_text",
    "host_of",
    "iso_date",
    "meta_parts",
]

MAX_LINE_BYTES = 900
MAX_LIST_DEPTH = 8  # deeper items are indented like depth 8 (16 spaces)
RULE = "\u2014\u2014\u2014"  # three em dashes
MIDDOT_SEP = " \u00b7 "
_UNTITLED = "Untitled"

# A sentence ends at . ! ? or an ellipsis, optionally followed by closing quotes or
# brackets, then whitespace. CJK full stops need no whitespace after them.
_SENTENCE_END_RE = re.compile(
    "(?:[.!?\u2026]+[\"'\u2019\u201d\u00bb)\\]]*\\s+)|(?:[\u3002\uff01\uff1f]+[\u300d\u300f\uff09]*\\s*)"
)


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


# --------------------------------------------------------------------------------------
# Shared text helpers
# --------------------------------------------------------------------------------------


def _scrub(s: str) -> str:
    """Remove code points that cannot be encoded or are invalid in XML 1.0 (lone
    surrogates, U+FFFE/U+FFFF and other noncharacters)."""
    if not s:
        return s
    return "".join(
        ch
        for ch in s
        if not (0xD800 <= ord(ch) <= 0xDFFF or (ord(ch) & 0xFFFE) == 0xFFFE or 0xFDD0 <= ord(ch) <= 0xFDEF)
    )


def inline_text(s: str) -> str:
    """Run text: clean (idempotent on already-clean text), one line, single spaces."""
    return _scrub(clean_text(s or ""))


def pre_lines(s: str) -> list[str]:
    """Lines of a :class:`Preformatted` block with their spacing kept.

    Tabs become 4 spaces; invisible and control characters, emoji and unsupported
    symbols are handled per segment by :func:`clean_text`, which would otherwise
    collapse the indentation. Leading/trailing blank lines are dropped.
    """
    text = unicodedata.normalize("NFC", s or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in text.split("\n"):
        raw = raw.replace("\t", "    ")
        parts = re.split(r"( +)", raw)
        out = [p if p.startswith(" ") else clean_text(p) for p in parts]
        lines.append(_scrub("".join(out).rstrip()))
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def clean_url_text(url: str) -> str:
    """``url`` as printable text: whitespace, controls and invisible characters removed."""
    url = _scrub(unicodedata.normalize("NFC", url or ""))
    return "".join(ch for ch in url if not (ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf")))


def host_of(url: str) -> str:
    """Display host of ``url`` without ``www.``, IDNA-decoded; "" when there is none."""
    if not url:
        return ""
    try:
        host = urlsplit(url.strip()).hostname or ""
    except ValueError:
        return ""
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if "xn--" in host:
        try:
            host = host.encode("ascii").decode("idna")
        except (UnicodeError, ValueError):
            pass
    return inline_text(host)


_DATE_RE = re.compile(r"^\s*(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")


def iso_date(published: str) -> str:
    """``YYYY-MM-DD`` (or ``YYYY-MM`` / ``YYYY``) prefix of ``published``; "" if invalid."""
    m = _DATE_RE.match(published or "")
    if not m:
        return ""
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if int(y) < 1:
        return ""
    if mo and not 1 <= int(mo) <= 12:
        return y
    if d and not 1 <= int(d) <= 31:
        return f"{y}-{mo}"
    return "-".join(p for p in (y, mo, d) if p)


def meta_parts(doc: Document) -> tuple[str, str, str]:
    """(byline, site, date) for headers. ``site`` falls back to the URL host and is
    dropped when it repeats the byline."""
    byline = inline_text(doc.byline)
    site = inline_text(doc.site) or host_of(doc.source_url)
    if site and byline and site.casefold() == byline.casefold():
        site = ""
    return byline, site, iso_date(doc.published)


# --------------------------------------------------------------------------------------
# Long-line splitting
# --------------------------------------------------------------------------------------


def _prefix_fitting(s: str, budget: int) -> int:
    """Largest index i such that s[:i] fits in ``budget`` bytes, not ending between a
    character and its combining marks (falls back to a plain cut if that leaves 0)."""
    used = 0
    i = 0
    for ch in s:
        n = _utf8_len(ch)
        if used + n > budget:
            break
        used += n
        i += 1
    j = i
    while 0 < j < len(s) and unicodedata.combining(s[j]):
        j -= 1
    return j or i or 1


def split_long_line(text: str, limit: int = MAX_LINE_BYTES) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` UTF-8 bytes.

    Cuts after the last sentence end that leaves the chunk at least a third full, else at
    the last space, else on a character boundary. Chunks are stripped of edge spaces.
    """
    if limit < 4:
        raise ValueError("limit too small")
    text = text.strip(" ")
    if _utf8_len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while _utf8_len(rest) > limit:
        end = _prefix_fitting(rest, limit)
        window = rest[:end]
        cut = 0
        min_len = _utf8_len(window) // 3
        for m in _SENTENCE_END_RE.finditer(window):
            if _utf8_len(window[: m.end()]) >= min_len and m.end() < len(rest):
                cut = m.end()
        if not cut:
            space = window.rfind(" ")
            cut = space + 1 if space > 0 else end
        head = rest[:cut].rstrip(" ")
        if head:
            chunks.append(head)
        rest = rest[cut:].lstrip(" ")
    if rest:
        chunks.append(rest)
    return chunks


# --------------------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------------------


def _list_lines(item: ListItem) -> list[str]:
    text = inline_text(item.text)
    if not text:
        return []
    depth = min(max(0, int(item.depth)), MAX_LIST_DEPTH)
    indent = "  " * depth
    marker = f"{max(0, int(item.index))}. " if item.ordered else "\u2022 "
    first_prefix = indent + marker
    cont_prefix = indent + " " * len(marker)
    chunks = split_long_line(text, MAX_LINE_BYTES - _utf8_len(first_prefix))
    return [(first_prefix if i == 0 else cont_prefix) + c for i, c in enumerate(chunks)]


def _heading_line(h: Heading) -> str:
    text = inline_text(h.text)
    if h.level <= 2:
        text = unicodedata.normalize("NFC", text.upper())
    return text


def _blocks_to_groups(doc: Document) -> list[list[str]]:
    """Each group is a run of lines; groups are separated by one blank line."""
    groups: list[list[str]] = []
    for note in doc.notes:
        text = inline_text(note)
        if text:
            groups.extend([c] for c in split_long_line("Note: " + text))
    list_group: list[str] | None = None
    top_ordered: bool | None = None
    for block in doc.blocks:
        if isinstance(block, ListItem):
            lines = _list_lines(block)
            if lines:
                if block.depth <= 0:
                    if top_ordered is not None and top_ordered != block.ordered:
                        list_group = None  # a bullet list followed by a numbered one: two lists
                    top_ordered = block.ordered
                if list_group is None:
                    list_group = []
                    groups.append(list_group)
                list_group.extend(lines)
            continue
        list_group = None
        top_ordered = None
        if isinstance(block, Heading):
            text = _heading_line(block)
            if text:
                groups.extend([c] for c in split_long_line(text))
        elif isinstance(block, Paragraph):
            groups.extend([c] for c in split_long_line(inline_text(block.text)))
        elif isinstance(block, Quote):
            groups.extend(["> " + c] for c in split_long_line(inline_text(block.text), MAX_LINE_BYTES - 2))
        elif isinstance(block, Preformatted):
            lines: list[str] = []
            for ln in pre_lines(block.text):
                if _utf8_len(ln) <= MAX_LINE_BYTES:
                    lines.append(ln)
                else:
                    lead = ln[: len(ln) - len(ln.lstrip(" "))][: MAX_LINE_BYTES // 2]
                    lines.extend(lead + c for c in split_long_line(ln, MAX_LINE_BYTES - len(lead)))
            if lines:
                groups.append(lines)
        elif isinstance(block, Rule):
            groups.append(["* * *"])
    return groups


def render_txt(doc: Document) -> bytes:
    """Render ``doc`` as UTF-8 text for CrossPoint (layout in ARCHITECTURE §6.1)."""
    title = inline_text(doc.title) or _UNTITLED
    header = split_long_line(title)
    meta = MIDDOT_SEP.join(p for p in meta_parts(doc) if p)
    if meta:
        header.extend(split_long_line(meta))
    header.append(RULE)

    out: list[str] = header + [""]
    for group in _blocks_to_groups(doc):
        out.extend(group)
        out.append("")
    url = clean_url_text(doc.source_url)
    if url:
        out.append(RULE)
        out.extend(split_long_line("Source: " + url))
    while out and out[-1] == "":
        out.pop()
    text = unicodedata.normalize("NFC", "\n".join(out) + "\n")
    # Already guaranteed by the cleaning above; kept as a cheap last line of defence
    # because the TXT reader draws these as replacement boxes.
    text = text.replace("\t", "    ").replace("\r", "").replace("\x00", "")
    return text.encode("utf-8")
