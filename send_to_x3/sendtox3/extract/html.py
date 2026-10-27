"""HTML -> :class:`~sendtox3.textmodel.Document`, and URL -> Document.

:func:`html_to_document` runs trafilatura (article extraction with the settings from
the extraction study: ``output_format="html"`` with formatting, tables, no links,
images or comments, only *precise* ``prune_xpath`` rules) and maps its output
onto textmodel blocks. With ``whole=True`` (email bodies) the whole HTML is converted
instead: an email is the content itself, and article extraction drops newsletter
lists and sections as boilerplate. trafilatura writes inline ``<code>`` as ``<pre>`` *inside*
``<p>``, so the output is parsed with an XML parser (an HTML parser would hoist the
``<pre>`` out of the paragraph).

:func:`url_to_document` fetches a link (site APIs first, see :mod:`.sites`), routes
by content type and never raises for remote problems: blocked, paywalled, JS-only or
unreadable pages come back as a Document carrying a note, whatever text could be
read, and the URL. When nothing at all could be read, ``blocks`` is empty and
``notes[0]`` explains why.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import html as _html
import logging
import re
from collections.abc import Iterable
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urljoin, urlsplit

from lxml import etree
from lxml import html as lxml_html

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
from . import fetch as _fetch
from .text import (
    TITLE_MAX_CHARS,
    clean_preformatted,
    derive_title,
    drop_empty_sections,
    guess_lang,
    normalize_heading_levels,
    text_to_document,
)

if TYPE_CHECKING:
    from ..config import Options

__all__ = [
    "PRUNE_XPATH",
    "clean_author",
    "clean_title",
    "html_fragment_to_blocks",
    "html_to_document",
    "is_mail_chrome",
    "is_mail_chrome_link",
    "note_document",
    "url_to_document",
]

log = logging.getLogger(__name__)


def _class_rule(tag: str, cls: str) -> str:
    """XPath matching ``tag`` elements carrying the exact class token ``cls``."""
    return f"//{tag}[contains(concat(' ',normalize-space(@class),' '),' {cls} ')]"


# Only precise rules (exact class tokens of known widgets). A broad one such as
# //*[contains(@class,'newsletter')] deletes the whole Substack post (Substack wraps
# posts in "newsletter-post"): verified 4225 -> 31 words.
PRUNE_XPATH = [
    _class_rule("span", "mw-editsection"),  # Wikipedia [edit] links
    _class_rule("div", "navbox"),  # Wikipedia navboxes ("Authority control databases" ...)
    _class_rule("ol", "references"),  # MediaWiki reference lists
    _class_rule("div", "reflist"),
    _class_rule("span", "mw-cite-backlink"),
    _class_rule("div", "subscription-widget-wrap"),  # Substack "Subscribe" boxes
    _class_rule("div", "subscribe-widget"),
]

PDF_MAX_BYTES = 25_000_000
# Own limit of a linked PDF's conversion (the worker is killed after it, or as soon as the
# caller's budget cancels the conversion); OCR runs 10-20 s per scanned page on a Pi 4.
PDF_TIMEOUT_SECONDS = 120.0
PDF_OCR_TIMEOUT_SECONDS = 600.0
SHORT_WORDS = 80  # below this, explain why so little came out
TINY_WORDS = 20  # below this, also add the page description

# --------------------------------------------------------------------------------------
# HTML element tree -> blocks
# --------------------------------------------------------------------------------------

_BR = "\u2028"  # internal line-break marker (never survives into block text)
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_CONTAINERS = frozenset(
    "p div section article main header footer aside figcaption dd dt dl address center details "
    "summary caption hgroup body html fieldset legend search".split()
)
_SKIP_TAGS = frozenset(
    "script style noscript template svg math img picture video audio iframe figure button form "
    "input select textarea option head title meta link base object embed canvas map area track "
    "source dialog nav rt rp".split()
)
_CHROME_TAGS = frozenset({"header", "footer", "aside", "nav"})
_SKIP_CLASSES = frozenset(
    {
        "mw-editsection", "navbox", "reference", "noprint", "mw-jump-link", "mw-cite-backlink",
        "subscription-widget-wrap", "subscription-widget", "subscribe-widget", "button-wrapper",
        "captioned-image-container", "image-gallery-embed", "embedded-post-wrap", "anchor",
        "octicon", "visually-hidden", "sr-only", "zeroclipboard-container",
    }
)  # fmt: skip
_INLINE_PARENTS_FOR_CODE = frozenset("p li td th h1 h2 h3 h4 h5 h6 span a b strong i em".split())
_BLOCKISH = frozenset("p div h1 h2 h3 h4 h5 h6 ul ol blockquote table pre section dl".split())
_MARKER_RE = re.compile(r"^\s*\[\s*(?:\d+|[a-z]|[ivx]+|citation needed|edit|note \d+|update)\s*\]\s*$", re.I)
_SUP_MARKER_RE = re.compile(r"^\s*\[[^\[\]]{1,40}\]\s*$")
# Wikipedia inline cleanup tags, removed wherever they survive extraction.
_CLEANUP_TAG_RE = re.compile(
    r"\[(?:citation needed|clarification needed|when\?|who\?|which\?|where\?|why\?|how\?|by whom\?|"
    r"according to whom\?|dubious(?: [–-] discuss)?|failed verification|better source needed|"
    r"verification needed|original research\?|page needed|year needed|not in citation given|"
    r"citation needed to verify|unreliable source\?|non-primary source needed|disputed [–-] discuss|edit)\]",
    re.I,
)
_BOILERPLATE = frozenset(
    {
        "advertisement", "advertisements", "skip to content", "skip to main content", "share this article",
        "share", "share this", "tweet", "print", "email", "related", "related articles", "read more",
        "continue reading", "subscribe", "sign up", "loading", "loading...", "comments", "show comments",
    }
)  # fmt: skip


def _tag(el: etree._Element) -> str:
    t = el.tag
    if not isinstance(t, str):
        return ""
    if "}" in t:
        t = t.rsplit("}", 1)[1]
    return t.lower()


def _all_text(el: etree._Element) -> str:
    return "".join(str(t) for t in el.itertext())


def _classes(el: etree._Element) -> set[str]:
    return set((el.get("class") or "").split())


def _skippable(el: etree._Element, *, drop_chrome: bool) -> bool:
    tag = _tag(el)
    if not tag or tag in _SKIP_TAGS:
        return True
    if drop_chrome and tag in _CHROME_TAGS:
        return True
    if el.get("hidden") is not None or (el.get("aria-hidden") or "").lower() == "true":
        return True
    if _classes(el) & _SKIP_CLASSES:
        return True
    if tag == "sup" and _SUP_MARKER_RE.match(_all_text(el)):
        return True  # citation markers such as [1], [a], [citation needed], [when?]
    return False


def _text_of(el: etree._Element, *, drop_chrome: bool = False) -> str:
    """Visible text of an element, honouring the skip rules, line breaks as spaces."""
    parts: list[str] = []

    def rec(e: etree._Element) -> None:
        if _skippable(e, drop_chrome=drop_chrome):
            return
        if _tag(e) == "br":
            parts.append(" ")
        if e.text:
            parts.append(e.text)
        for c in e:
            rec(c)
            if c.tail:
                parts.append(c.tail)

    rec(el)
    return clean_text(_CLEANUP_TAG_RE.sub("", "".join(parts)))


def _nearest_table(el: etree._Element) -> etree._Element | None:
    p = el.getparent()
    while p is not None and _tag(p) != "table":
        p = p.getparent()
    return p


def _own_rows(table: etree._Element) -> list[etree._Element]:
    return [tr for tr in table.iter() if _tag(tr) == "tr" and _nearest_table(tr) is table]


def _cells(tr: etree._Element) -> list[etree._Element]:
    return [c for c in tr if _tag(c) in ("td", "th")]


def _is_layout_table(table: etree._Element) -> bool:
    """Layout tables (old sites, newsletters) hold prose; data tables hold values."""
    rows = _own_rows(table)
    max_cols = max((len(_cells(tr)) for tr in rows), default=0)
    if max_cols <= 1:
        return True
    for d in table.iterdescendants():
        if _tag(d) in _BLOCKISH and _nearest_table(d) is table and _tag(d) != "table":
            return True
    return any(len(_all_text(c)) > 300 for tr in rows for c in _cells(tr))


def _reflow_preformatted(text: str) -> list[str]:
    """Bare pre-wrapped prose (lib.ru, Gutenberg in <pre>) -> paragraphs.

    Blank lines separate paragraphs; an indented line or a dash/number-led line after
    text starts one (dialogue lines in Russian prose start with "- "); wrapped lines join.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    paras: list[str] = []
    cur: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            if cur:
                paras.append(" ".join(cur))
                cur = []
            continue
        if cur and re.match(r"^(?: {2,}|[-*•—–] |\d+[.)] )", line):
            paras.append(" ".join(cur))
            cur = []
        cur.append(line.strip())
    if cur:
        paras.append(" ".join(cur))
    return [t for t in (clean_text(p) for p in paras) if t]


class _BlockBuilder:
    """Walks an element tree and emits textmodel blocks.

    Inline text accumulates in ``buf``; :meth:`flush` turns it into the block type the
    current context calls for (heading, list item, quote or paragraphs split at
    ``<br>``). List items collect all their inline content (including ``<p>`` children)
    until a nested list or the end of the item.
    """

    def __init__(self, *, drop_chrome: bool = False) -> None:
        self.blocks: list[Block] = []
        self.buf: list[str] = []
        self.lists: list[list[int | bool]] = []  # [ordered, last index]
        self.items: list[tuple[bool, int, int]] = []  # open <li>: (ordered, index, depth)
        self.quote = 0
        self.heading: int | None = None
        self.drop_chrome = drop_chrome

    # -- emitting ---------------------------------------------------------------------

    def add(self, s: str | None) -> None:
        if s:
            self.buf.append(_CLEANUP_TAG_RE.sub("", s.replace(_BR, " ")))

    def _emit(self, block: Block) -> None:
        self.blocks.append(block)

    def flush(self) -> None:
        raw = "".join(self.buf)
        self.buf.clear()
        if not raw.strip():
            return
        if self.heading is not None:
            t = clean_text(raw.replace(_BR, " "))
            if t and not _MARKER_RE.match(t):
                self._emit(Quote(text=t) if self.quote else Heading(text=t, level=self.heading))
            return
        if self.items:
            ordered, index, depth = self.items[-1]
            t = clean_text(raw.replace(_BR, " "))
            if not t or _MARKER_RE.match(t):
                return
            if self.quote:
                self._emit(Quote(text=f"{index}. {t}" if ordered else f"• {t}"))
            else:
                self._emit(ListItem(text=t, ordered=ordered, index=index, depth=depth))
            return
        for piece in raw.split(_BR):
            t = clean_text(piece)
            if not t or _MARKER_RE.match(t):
                continue
            self._emit(Quote(text=t) if self.quote else Paragraph(text=t))

    # -- walking ----------------------------------------------------------------------

    def walk_children(self, el: etree._Element) -> None:
        self.add(el.text)
        for child in el:
            self.walk(child)
            self.add(child.tail)

    def walk(self, el: etree._Element) -> None:
        if _skippable(el, drop_chrome=self.drop_chrome):
            return
        tag = _tag(el)
        if tag in _HEADINGS:
            self._heading(el, _HEADINGS[tag])
        elif tag in ("ul", "ol"):
            self._list(el, ordered=(tag == "ol"))
        elif tag == "li":
            self._item(el)
        elif tag == "blockquote":
            self._blockquote(el)
        elif tag == "pre":
            self._pre(el)
        elif tag == "br":
            self.buf.append(_BR)
        elif tag == "hr":
            self.flush()
            if not self.items:
                self._emit(Rule())
        elif tag == "table":
            self._table(el)
        elif "footnote" in _classes(el) and tag == "div":
            self._footnote(el)
        elif "footnote-anchor" in _classes(el):
            self.add(f"[{_text_of(el)}]")
        elif tag in _BLOCK_CONTAINERS:
            if self.items or self.heading is not None:
                self.add(" ")
                self.walk_children(el)
                self.add(" ")
            else:
                self.flush()
                self.walk_children(el)
                self.flush()
        else:  # inline: a, span, b, i, code, sub, sup, small, q, custom elements, ...
            self.walk_children(el)

    def _heading(self, el: etree._Element, level: int) -> None:
        if self.items or self.heading is not None:
            self.add(" ")
            self.walk_children(el)
            self.add(" ")
            return
        self.flush()
        self.heading = level
        self.walk_children(el)
        self.flush()
        self.heading = None

    def _list(self, el: etree._Element, *, ordered: bool) -> None:
        self.flush()
        start = 0
        if ordered:
            try:
                start = int(el.get("start", "1")) - 1
            except ValueError:
                start = 0
        self.lists.append([ordered, start])
        for child in el:
            self.walk(child)
            if child.tail and child.tail.strip():
                self.add(child.tail)
        self.flush()
        self.lists.pop()

    def _item(self, el: etree._Element) -> None:
        self.flush()
        if self.lists:
            frame = self.lists[-1]
            frame[1] = int(frame[1]) + 1
            ordered, index, depth = bool(frame[0]), int(frame[1]), len(self.lists) - 1
        else:
            ordered, index, depth = False, 1, 0
        self.items.append((ordered, index, depth))
        self.walk_children(el)
        self.flush()
        self.items.pop()

    def _blockquote(self, el: etree._Element) -> None:
        self.flush()
        self.quote += 1
        saved_items, self.items = self.items, []
        self.walk_children(el)
        self.flush()
        self.items = saved_items
        self.quote -= 1

    def _pre(self, el: etree._Element) -> None:
        code = _all_text(el)
        parent = el.getparent()
        if parent is not None and _tag(parent) in _INLINE_PARENTS_FOR_CODE and "\n" not in code.strip("\n"):
            self.add(code)  # trafilatura's rendering of inline <code>
            return
        self.flush()
        if code.startswith("\n"):
            code = code[1:]
        body = clean_preformatted(code)
        if body:
            self._emit(Preformatted(text=body))

    def _table(self, el: etree._Element) -> None:
        self.flush()
        if _is_layout_table(el):
            for d in el.iter():
                if _tag(d) in ("td", "th") and _nearest_table(d) is el:
                    self.walk_children(d)
                    self.flush()
            return
        for d in el:
            if _tag(d) == "caption":
                cap = _text_of(d)
                if cap:
                    self._emit(Quote(text=cap) if self.quote else Paragraph(text=cap))
        for tr in _own_rows(el):
            cells = [_text_of(c) for c in _cells(tr)]
            row = " | ".join(c for c in cells if c)
            if row:
                self._emit(Quote(text=row) if self.quote else Paragraph(text=row))

    def _footnote(self, el: etree._Element) -> None:
        """Substack-style footnote: number + content as one paragraph "[n] text"."""
        self.flush()
        num_el = next((d for d in el.iter() if "footnote-number" in _classes(d)), None)
        num = _text_of(num_el) if num_el is not None else ""
        parts = [_text_of(c) for c in el if c is not num_el and "footnote-number" not in _classes(c)]
        body = " ".join(p for p in parts if p)
        if body:
            self._emit(Paragraph(text=f"[{num}] {body}" if num else body))

    # -- roots ------------------------------------------------------------------------

    def walk_root(self, body: etree._Element) -> None:
        """Walk a <body>; long bare text with line structure (pre-wrapped prose) is reflowed."""
        self._root_text(body.text)
        for child in body:
            self.walk(child)
            self._root_text(child.tail)
        self.flush()

    def _root_text(self, text: str | None) -> None:
        if not text or not text.strip():
            return
        if "\n" in text.strip() and len(text.strip()) > 200:
            self.flush()
            for p in _reflow_preformatted(text):
                if not _MARKER_RE.match(p):
                    self._emit(Paragraph(text=p))
        else:
            self.add(text)


def _find_body(root: etree._Element) -> etree._Element:
    if _tag(root) == "body":
        return root
    for el in root.iter():
        if _tag(el) == "body":
            return el
    return root


def _elements_to_blocks(root: etree._Element, *, drop_chrome: bool = False) -> list[Block]:
    b = _BlockBuilder(drop_chrome=drop_chrome)
    body = _find_body(root)
    try:
        b.walk_root(body)
    except RecursionError:
        # Pathologically deep markup: keep the text, lose the structure.
        log.warning("HTML nested too deeply; extracting plain text")
        text = "\n\n".join(str(t).strip() for t in body.itertext() if str(t).strip())
        return [Paragraph(text=p) for p in (clean_text(x) for x in text.split("\n\n")) if p]
    return _postprocess(b.blocks)


def _postprocess(blocks: Iterable[Block]) -> list[Block]:
    """Drop boilerplate one-liners, leading/trailing/duplicate rules."""
    out: list[Block] = []
    for b in blocks:
        if isinstance(b, Paragraph) and re.sub(r"[^\w .]+", "", b.text).strip().lower() in _BOILERPLATE:
            continue
        if isinstance(b, Rule) and (not out or isinstance(out[-1], Rule)):
            continue
        out.append(b)
    while out and isinstance(out[-1], Rule):
        out.pop()
    return out


def _parse_xml(fragment: str) -> etree._Element | None:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_comments=True)
    try:
        return etree.fromstring(fragment.encode("utf-8"), parser)
    except (etree.XMLSyntaxError, ValueError):
        return None


def _parse_html(fragment: str) -> etree._Element | None:
    # huge_tree: libxml2 otherwise silently drops everything nested deeper than 255
    parser = lxml_html.HTMLParser(huge_tree=True, remove_comments=True)
    try:
        return lxml_html.document_fromstring(fragment, parser=parser)
    except (etree.ParserError, ValueError):
        return None


def html_fragment_to_blocks(
    fragment: str, *, xml: bool = False, drop_chrome: bool = False, normalize: bool = True
) -> list[Block]:
    """Clean/simple HTML (API bodies, trafilatura output, email bodies) -> blocks.

    ``xml=True`` parses as XML first (needed for trafilatura output), falling back to
    the HTML parser. ``drop_chrome`` also skips header/footer/aside/nav. Heading levels
    are normalised to 2..3 (see :func:`~.text.normalize_heading_levels`) unless
    ``normalize=False``, which keeps the source levels 1..6 (for callers that still
    remove a title heading first).
    """
    if not fragment or not fragment.strip():
        return []
    root = _parse_xml(fragment) if xml else None
    if root is None:
        root = _parse_html(fragment)
    if root is None:
        return []
    blocks = _elements_to_blocks(root, drop_chrome=drop_chrome)
    return normalize_heading_levels(blocks) if normalize else blocks


# --------------------------------------------------------------------------------------
# Metadata helpers
# --------------------------------------------------------------------------------------

# The rest of a start tag: quoted values may hold "<" or ">" (content="Why 1 < 2 matters"),
# anything else stops at "<". Linear on any input, even with findall retrying from every
# "<meta": the quantifiers are possessive, and each character moves a scan between the
# states outside / in "..." / in '...' one-to-one, so two scans alive at the same place
# are always in different states (one of them outside, which a later "<" stops): at most
# three scans ever cover a character. (Only "[^>]*" rescanned the page for every
# unclosed "<meta": quadratic, seconds holding the GIL.)
_TAG_REST = r"""(?:[^<>"']++|"[^"]*+"|'[^']*+')*+>"""
_META_TAG_RE = re.compile(r"<meta\b" + _TAG_REST, re.I)
_HTML_TAG_RE = re.compile(r"<html\b" + _TAG_REST, re.I)
_TITLE_OPEN_RE = re.compile(r"<title\b" + _TAG_REST, re.I)
# One attribute. A name only starts where no name character precedes it, so a long run
# of letters is one attempt, not one per letter (each of which rescanned the run: 10 s on
# "<meta " plus 40k letters); possessive quantifiers never give back what they matched.
_ATTR_RE = re.compile(
    r"""(?<![-a-zA-Z0-9_:.])([a-zA-Z_:][-a-zA-Z0-9_:.]*+)\s*+=\s*+(?:"([^"]*+)"|'([^']*+)'|([^\s"'>]++))"""
)
_TITLE_CLOSE_RE = re.compile(r"</title>", re.I)
_PAYWALL_RE = re.compile(r""""isAccessibleForFree"\s*:\s*(?:false|"false"|"False"|"no")""")
_JS_HINT_RE = re.compile(
    r"(?i)(?:enable javascript|javascript is (?:required|disabled|not enabled)|"
    r"requires javascript|turn on javascript|need to enable js|<noscript)"
)
_DOCUMENT_TAG_RE = re.compile(r"(?i)<(?:html|head|body)\b")
_HEAD_END_RE = re.compile(r"(?i)</head\s*>")
_NON_LATIN_LANGS = frozenset({"ru", "uk", "sr", "bg", "el", "ja", "zh", "ko", "ar", "he", "th", "hi", "fa"})


def _tag_attrs(tag: str) -> dict[str, str]:
    """Attributes of one start tag, names lowercased, values unescaped."""
    return {
        a.group(1).lower(): _html.unescape(a.group(2) or a.group(3) or a.group(4) or "")
        for a in _ATTR_RE.finditer(tag)
    }


def _html_lang(html: str) -> str:
    """Primary language subtag of ``<html lang>`` (or ``xml:lang``), lowercased, or ``""``."""
    m = _HTML_TAG_RE.search(html, 0, 20_000)
    if not m:
        return ""
    attrs = _tag_attrs(m.group(0))
    return _norm_lang(attrs.get("lang") or attrs.get("xml:lang") or "")


def _meta_tags(html: str) -> dict[str, str]:
    """``<meta>`` values from the document head keyed by lowercased property/name."""
    m = _HEAD_END_RE.search(html, 0, 600_000)
    head = html[: m.start() if m else 300_000]
    out: dict[str, str] = {}
    for tag in _META_TAG_RE.findall(head):
        attrs = _tag_attrs(tag)
        key = (
            attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or attrs.get("http-equiv") or ""
        ).lower()
        if key and "content" in attrs and key not in out:
            out[key] = attrs["content"].strip()
    return out


def _title_tag(html: str) -> str:
    """Text of the first ``<title>...</title>`` (linear: a lazy ``(.*?)</title>`` search
    rescans the page for every unclosed ``<title>``, 55 s on a crafted 300 KB page)."""
    m = _TITLE_OPEN_RE.search(html)
    end = _TITLE_CLOSE_RE.search(html, m.end()) if m else None
    return html[m.end() : end.start()] if m and end else ""


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            return host[len(prefix) :]
    return host


def _site_label(host: str) -> str:
    """'theguardian' for theguardian.com, 'bbc' for bbc.co.uk."""
    labels = host.split(".")
    if len(labels) >= 3 and re.fullmatch(r"(?:co|com|org|net|ac|gov|edu|ne|or)", labels[-2]) and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else host


def _squash(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.casefold())


_TITLE_SEPS = (" | ", " - ", " – ", " — ", " :: ", " · ", " • ", " » ", " / ", ": ")


def _is_site_segment(seg: str, site: str, host: str, *, loose: bool = False) -> bool:
    """True if ``seg`` names the site: equal to ``site``, the host's name or the host.

    ``loose`` also accepts a short segment that merely starts with the host's name
    ("Simon Willison's Weblog" on simonwillison.net). Used for a *trailing* segment
    only: a leading one is usually the title itself ("Nature's 10: ..." on nature.com,
    "Python: the language" on python.org), see :func:`_is_leading_site`.
    """
    sq = _squash(seg)
    if not sq:
        return False
    if site and sq == _squash(site):
        return True
    if not host:
        return False
    label = _squash(_site_label(host))
    if sq in (label, _squash(host)):
        return True
    # short labels must match exactly
    return loose and len(label) >= 5 and sq.startswith(label) and len(seg.split()) <= 4


_LEADING_SITE_MIN_WORDS = 4  # what a leading "Site: " leaves must be a real headline


def _is_leading_site(first: str, sep: str, rest: str, site: str) -> bool:
    """Whether ``first`` (before the first ``sep``) is a site prefix to remove: it must
    be the site name itself (``og:site_name``), not just the host's name ("Python: the
    language" on python.org, "Wired for sound: ..." on wired.com are headlines), and
    after "Site: " a headline of :data:`_LEADING_SITE_MIN_WORDS`+ words must remain.
    The same rule as :func:`sendtox3.ingest.strip_display_affixes`."""
    if not site.strip() or " ".join(first.split()).casefold() != " ".join(site.split()).casefold():
        return False
    if sep == ": ":
        return len(rest.split()) >= _LEADING_SITE_MIN_WORDS
    return len(rest.strip()) >= 3


def clean_title(title: str, *, site: str = "", host: str = "") -> str:
    """Strip site affixes from a page title ("X - Wikipedia", "GitHub - x", "X | Site").

    A trailing segment that names the site (equal to ``site`` or the host's name, or
    just starting with it, e.g. "Simon Willison's Weblog" on simonwillison.net) is
    removed unless fewer than 3 characters would remain. A leading one only when it is
    exactly ``site`` (see :func:`_is_leading_site`): "Site: " also needs 4+ words after it.
    """
    t = clean_text(title)
    if not t:
        return ""
    t = re.sub(r"\s+[-–—|]\s+Wikipedia(?:, the free encyclopedia)?$", "", t)
    if host == "github.com" or host.endswith(".github.com") or _squash(site) == "github":
        t = re.sub(r"^GitHub\s+[-–—]\s+", "", t)
    for _ in range(2):
        changed = False
        for sep in _TITLE_SEPS:
            if sep not in t:
                continue
            head, _, last = t.rpartition(sep)
            if sep != ": " and _is_site_segment(last, site, host, loose=True) and len(head.strip()) >= 3:
                t, changed = head.strip(), True
                break
            first, _, rest = t.partition(sep)
            if _is_leading_site(first, sep, rest, site):
                t, changed = rest.strip(), True
                break
        if not changed:
            break
    return t


def clean_author(author: str) -> str:
    """Sanity-check an extracted author: drop navbox garbage, URLs and over-long values."""
    a = clean_text(author or "")
    a = re.sub(r"^(?:by|von|par|por|di)\s+", "", a, flags=re.I)
    a = ", ".join(p.strip() for p in a.split(";") if p.strip())
    if not a or len(a) > 80:
        return ""
    if re.search(r"(?i)authority control|contributors|databases|https?://|www\.|@\w+\.\w+", a):
        return ""
    return a


def _iso_date(value: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", (value or "").strip())
    if not m:
        return ""
    try:
        _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return ""
    return m.group(0)


def _norm_lang(value: str) -> str:
    m = re.match(r"^\s*([A-Za-z]{2,3})(?:[-_]|$)", value or "")
    return m.group(1).lower() if m else ""


def _pick_lang(declared: str, text: str) -> str:
    """Declared language unless the script clearly says otherwise (templates often say "en")."""
    detected = guess_lang(text, default="")
    if declared and detected in _NON_LATIN_LANGS and detected != declared:
        return detected
    return declared or detected or "en"


def _word_count(blocks: Iterable[Block]) -> int:
    return sum(len(getattr(b, "text", "").split()) for b in blocks)


def _drop_title_repeat(blocks: list[Block], title: str) -> list[Block]:
    """Drop a first heading/paragraph that repeats the title (unless it is all there is)."""
    if (
        len(blocks) > 1
        and title
        and isinstance(blocks[0], (Heading, Paragraph))
        and _squash(blocks[0].text) == _squash(title)
    ):
        return blocks[1:]
    return blocks


# --------------------------------------------------------------------------------------
# Whole-HTML conversion (email bodies)
# --------------------------------------------------------------------------------------

# Inline styles that hide an element (email preheaders, mobile-only duplicates). No two
# whitespace runs are adjacent: "\s*(?:!important)?\s*" backtracks quadratically on a
# style attribute with a long run of spaces.
_HIDDEN_STYLE_RE = re.compile(
    r"(?i)(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all|"
    r"max-height\s*:\s*0(?:px)?\s*(?:!important\s*)?(?:;|$)|opacity\s*:\s*0(?:\.0+)?\s*(?:!important\s*)?(?:;|$))"
)
# Mailing-list chrome that is never content, in strict forms only, so that a sentence
# which merely mentions unsubscribing, receiving or a browser is never touched:
#   - a block that is nothing but footer links ("Unsubscribe", "View this email in your
#     browser", "Manage preferences", "Forward to a friend", joined by "|", "·", "or" ...,
#     each optionally followed by its URL), optionally after a short lead-in question
#     ("Trouble viewing this email? View it in your browser.") or "if you no longer wish
#     to receive ..." clause ("..., you can unsubscribe here.");
#   - a block that starts "You are receiving this email because ..." or "This email was
#     sent to <address>".
# extract/email.py cleans an email's own HTML and plain text with the same rules
# (:func:`is_mail_chrome`, :func:`is_mail_chrome_link`).
_MAIL_CHROME_LINK = r"""
    (?:(?:click|tap)\s+here\s+to\s+)?unsubscribe
        (?:\s+(?:here|now|instantly|from\s+(?:this|these|our|all)\s+(?:lists?|e-?mails?|newsletters?|mailings?)))?
  | (?:view|read|open)\s+(?:this\s+(?:e-?mail|message|newsletter|issue|post)\s+|it\s+|this\s+)?
        (?:online|on\s+the\s+web|as\s+a\s+web\s*page|in\s+(?:your\s+|a\s+|the\s+)?(?:web\s*)?(?:browser|app))
  | (?:manage|update|change)\s+(?:your\s+)?(?:e-?mail\s+|subscription\s+|mailing\s+)?(?:preferences|settings)
  | (?:manage|update|change)\s+(?:your\s+)?(?:e-?mail\s+)?subscriptions?
  | (?:e-?mail\s+)?preferences
  | forward\s+(?:this\s+(?:e-?mail\s+|newsletter\s+|issue\s+)?)?to\s+a\s+friend
  | add\s+us\s+to\s+your\s+address\s+book
"""
# Its URL, as plain-text emails write it: "Unsubscribe: https://...", "<https://...>",
# "[https://...]" or "(https://...)".
_MAIL_CHROME_URL = (
    r"(?:\s*:?\s*(?:<\s*https?://[^\s<>]{1,300}+\s*>|\[\s*https?://[^\s\[\]]{1,300}+\s*\]"
    r"|\(\s*https?://[^\s()]{1,300}+\s*\)|https?://[^\s<>\[\]()]{1,300}+))?"
)
_MAIL_CHROME_LINKS_RE = re.compile(
    rf"""(?ix)
    [^\w?]{{0,10}}
    (?:[^?]{{0,60}}\b(?:view(?:ing)?|see(?:ing)?|display(?:ing|ed)?|read(?:ing)?|images?|receiv(?:e|ing)|get(?:ting)?)\b
       [^?]{{0,40}}\?\s*                          # lead-in question
     | (?:if\s+you\s+(?:no\s+longer\s+(?:wish|want)\s+to|would\s+no\s+longer\s+like\s+to|would\s+rather\s+not
                       |(?:do\s+not|don't|don’t)\s+(?:wish|want)\s+to)\s+receive
        | to\s+stop\s+receiving)\b[^,.:;?!]{{0,60}}[,.:;]\s*   # "if you no longer wish to receive these,"
    )?
    (?:you\s+(?:can|may)\s+|please\s+|simply\s+)?
    (?:{_MAIL_CHROME_LINK}){_MAIL_CHROME_URL}
    (?:[\s|·•/,:;–—-]+(?:or\s+|and\s+)?(?:{_MAIL_CHROME_LINK}){_MAIL_CHROME_URL})*
    [\s.!|·•]*
    """
)
_MAIL_CHROME_LINK_RE = re.compile(rf"(?ix)[^\w]{{0,3}}(?:{_MAIL_CHROME_LINK})[\s.!:»›→>]*")
_MAIL_CHROME_START_RE = re.compile(
    r"""(?ix)[^\w]{0,10}(?:
        you(?:'re|’re|\s+are)?\s+receiv(?:ed|ing)\s+this\s+(?:e-?mail|message|newsletter|mail)\s+because\b
      | this\s+(?:e-?mail|message|newsletter)\s+was\s+sent\s+to\s+[^\s@]+@[^\s@]+\.\w)"""
)
_MAIL_CHROME_MIN_BLOCKS = 13  # shorter emails are left alone: every block is near an edge
_MAIL_CHROME_MAX_WORDS = 25
_MAIL_CHROME_MAX_CHARS = 400
_MAIL_CHROME_HEAD, _MAIL_CHROME_TAIL = 3, 8  # how many blocks at each end are looked at
MAIL_CHROME_EDGE = (_MAIL_CHROME_HEAD, _MAIL_CHROME_TAIL)


def is_mail_chrome(
    text: str, *, max_words: int = _MAIL_CHROME_MAX_WORDS, max_chars: int = _MAIL_CHROME_MAX_CHARS
) -> bool:
    """Whether ``text`` (one block of an email) is mailing-list chrome in one of the
    strict forms above. A sentence that merely mentions unsubscribing, receiving, a
    subscription or a browser is not ("Please update your subscription by Friday")."""
    t = text.strip()
    if not t or len(t) > max_chars or len(t.split()) > max_words:
        return False
    return bool(_MAIL_CHROME_LINKS_RE.fullmatch(t) or _MAIL_CHROME_START_RE.match(t))


def is_mail_chrome_link(text: str) -> bool:
    """Whether ``text`` (a link's text) is one footer link: "Unsubscribe", "View in browser"..."""
    t = text.strip()
    return bool(t) and len(t) <= 80 and bool(_MAIL_CHROME_LINK_RE.fullmatch(t))


def _remove_hidden(root: etree._Element) -> None:
    """Drop elements styled invisible, keeping the text that follows them."""
    for el in list(root.iter()):
        if not isinstance(el.tag, str) or _tag(el) in ("html", "body") or el.getparent() is None:
            continue
        if _HIDDEN_STYLE_RE.search(el.get("style") or ""):
            el.drop_tree()  # lxml.html: removes the element, keeps its tail


def _strip_mail_chrome(blocks: list[Block]) -> list[Block]:
    """Remove "Unsubscribe" / "View in browser" blocks near the start or end of a long
    email (see :data:`_MAIL_CHROME_LINKS_RE`); a short email is kept as it is."""
    if len(blocks) < _MAIL_CHROME_MIN_BLOCKS:
        return blocks

    def chrome(i: int, b: Block) -> bool:
        if not (i < _MAIL_CHROME_HEAD or i >= len(blocks) - _MAIL_CHROME_TAIL):
            return False
        return is_mail_chrome(getattr(b, "text", "") or "")

    kept = [b for i, b in enumerate(blocks) if not chrome(i, b)]
    if len(kept) == len(blocks) or not any(getattr(b, "text", "") for b in kept):
        return blocks  # nothing to strip, or it was all there is
    return _postprocess(kept)


def _whole_html_blocks(html: str) -> list[Block]:
    """Every visible block of an HTML document or fragment, without article extraction.

    Scripts, styles, the head, images (tracking pixels), hidden elements and
    header/footer/nav/aside chrome are dropped, as are obvious mailing-list chrome lines
    at either end; everything else (lists, sections, tables) is kept.
    """
    root = _parse_html(html) if html.strip() else None
    if root is None:
        return []
    _remove_hidden(root)
    return _strip_mail_chrome(_elements_to_blocks(root, drop_chrome=True))


# --------------------------------------------------------------------------------------
# HTML -> Document
# --------------------------------------------------------------------------------------


_TRAFILATURA_META = ("title", "author", "sitename", "date", "description")


def _trafilatura_options() -> dict[str, Any]:
    from trafilatura.deduplication import LRUCache

    return dict(
        output_format="html",
        include_formatting=True,
        include_tables=True,
        include_links=False,
        include_images=False,
        include_comments=False,
        # A fresh cache per call: the default is a process-wide LRU that would start
        # discarding an article's paragraphs (and finally the whole document) once the
        # same page has been extracted three times in this long-running process.
        deduplicate=LRUCache(maxsize=4096),
        with_metadata=True,
        prune_xpath=PRUNE_XPATH,
        date_extraction_params={
            "original_date": True,
            "extensive_search": False,  # extensive search guesses dates from copyright lines
            "max_date": _dt.date.today().isoformat(),
        },
    )


def _trafilatura_extract(html: str, url: str) -> tuple[etree._Element | None, dict[str, str]]:
    """Run trafilatura; return its simplified-HTML tree and its metadata.

    This is exactly what ``trafilatura.extract(output_format="html")`` produces, taken
    as a tree instead of a string, with one fix: trafilatura's HTML conversion turns
    every list into ``<ul>`` and clears attributes, so lists it marked ``rend="ol"`` are
    renamed to ``<ol>`` first and stay numbered. If trafilatura's internals ever move,
    the plain string output is used (parsed with an XML parser: trafilatura writes
    inline ``<code>`` as ``<pre>`` inside ``<p>``, which an HTML parser would hoist).
    """
    import trafilatura

    convert_to_html: Callable[[etree._Element], etree._Element] | None
    try:
        from trafilatura.htmlprocessing import convert_to_html
    except ImportError:  # pragma: no cover - depends on the installed trafilatura
        convert_to_html = None
    if convert_to_html is not None:
        result = trafilatura.bare_extraction(html, url=url or None, **_trafilatura_options())
        body = getattr(result, "body", None)
        if body is None:
            return None, {}
        meta = {k: str(getattr(result, k, "") or "") for k in _TRAFILATURA_META}
        for el in body.iter("list"):
            if el.get("rend") == "ol":
                el.tag = "ol"
        return convert_to_html(body), meta
    out = trafilatura.extract(html, url=url or None, **_trafilatura_options())  # pragma: no cover
    if not out:  # pragma: no cover
        return None, {}
    root = _parse_xml(out)  # pragma: no cover
    if root is None:  # pragma: no cover
        root = _parse_html(out)
    meta = {}  # pragma: no cover
    if root is not None:  # pragma: no cover
        meta = {el.get("name", ""): el.get("content", "") for el in root.iter() if _tag(el) == "meta"}
    return root, meta  # pragma: no cover


def _trafilatura_metadata(html: str, url: str) -> dict[str, str]:
    from trafilatura.metadata import extract_metadata

    try:
        m = extract_metadata(html, default_url=url or None, extensive=False)
    except Exception:  # pragma: no cover - metadata is best effort
        return {}
    return {k: str(getattr(m, k, "") or "") for k in ("title", "author", "sitename", "date", "description")}


def html_to_document(html: str, *, url: str = "", lang_hint: str = "", whole: bool = False) -> Document:
    """Extract the article from an HTML page.

    Title, author, site, date and language come from trafilatura and the page's
    ``<meta>`` tags (site affixes stripped, garbage authors dropped). Subscriber-only
    pages (JSON-LD ``isAccessibleForFree: false``) and pages with fewer than 80 words
    get a note; under 20 words the page description is appended. ``lang_hint`` (e.g.
    a Content-Language header) is used when the page declares no language.

    ``whole=True`` is for HTML that is the content itself (an email body or an HTML
    attachment from an email): the entire HTML is converted (see
    :func:`_whole_html_blocks`) without trafilatura's article extraction, whose
    boilerplate pruning drops newsletter lists and sections, and no notes are added
    (a short email is normal, not a sign of a JavaScript page or a paywall).
    """
    html = html or ""
    url = url or ""
    meta = _meta_tags(html)
    tmeta: dict[str, str] = {}
    if whole:
        blocks = _whole_html_blocks(html)
        if html.strip():
            tmeta = _trafilatura_metadata(html, url)  # title/author/date only, as before
    else:
        root: etree._Element | None = None
        if html.strip():
            try:
                root, tmeta = _trafilatura_extract(html, url)
            except Exception:  # pragma: no cover - trafilatura bugs must not lose the item
                log.exception("trafilatura failed on %s", url)
        blocks = _elements_to_blocks(root) if root is not None else []
        if root is None and html.strip():
            tmeta = _trafilatura_metadata(html, url)

        # trafilatura finds nothing in very short inputs, and drops parts of HTML
        # *fragments* (shared selections), which carry no boilerplate anyway: then the
        # whole fragment is used.
        words = _word_count(blocks)
        is_fragment = not _DOCUMENT_TAG_RE.search(html[:4096])
        if html.strip() and (words == 0 or (is_fragment and len(html) < 200_000)):
            fallback_root = _parse_html(html)
            if fallback_root is not None:
                everything = _elements_to_blocks(fallback_root, drop_chrome=True)
                if _word_count(everything) > words:
                    blocks = everything

    host = _host(url)
    site = clean_text(meta.get("og:site_name") or tmeta.get("sitename") or "")
    if re.search(r"(?i)wikimedia foundation", site) or host.endswith("wikipedia.org"):
        site = "Wikipedia"
    raw_title = tmeta.get("title") or meta.get("og:title") or meta.get("twitter:title") or ""
    if not raw_title:
        raw_title = _html.unescape(_title_tag(html[:300_000]))
    title = clean_title(raw_title, site=site, host=host)
    blocks = _drop_title_repeat(blocks, title)
    blocks = normalize_heading_levels(drop_empty_sections(blocks))
    if not title:
        first = next((b for b in blocks if isinstance(b, Heading)), None)
        if first is not None and len(first.text) <= TITLE_MAX_CHARS:
            title = first.text
            blocks = _drop_title_repeat(blocks, title)
        else:  # derived from the body text, which stays in the body
            title = derive_title("\n".join(getattr(b, "text", "") for b in blocks[:3]))

    author = clean_author(tmeta.get("author") or meta.get("author") or meta.get("article:author") or "")
    meta_author = clean_author(meta.get("author") or "")
    if meta_author and _squash(meta_author) == _squash(author):
        author = meta_author  # trafilatura drops punctuation ("R. Smith" -> "R Smith")
    published = (
        "" if site == "Wikipedia" else _iso_date(tmeta.get("date", "") or meta.get("article:published_time", ""))
    )
    declared = (
        _html_lang(html)
        or _norm_lang(meta.get("og:locale", ""))
        or _norm_lang(meta.get("content-language", ""))
        or _norm_lang(lang_hint)
    )
    plain = "\n".join(getattr(b, "text", "") for b in blocks)
    doc = Document(
        title=title,
        blocks=blocks,
        byline=author,
        site=site or host,
        source_url=url,
        published=published,
        lang=_pick_lang(declared, plain or title),
    )
    if not whole:
        _add_quality_notes(doc, html, meta, tmeta)
    return doc


def _add_quality_notes(doc: Document, html: str, meta: dict[str, str], tmeta: dict[str, str]) -> None:
    if doc.word_count() < TINY_WORDS:
        # "You need to enable JavaScript to run this app." is not content
        doc.blocks = [b for b in doc.blocks if not _JS_HINT_RE.search(getattr(b, "text", ""))]
    words = doc.word_count()
    if _PAYWALL_RE.search(html):
        doc.notes.append("Marked subscriber-only by the publisher: this may be only the free preview.")
    if words < SHORT_WORDS:
        if _JS_HINT_RE.search(html) and words < TINY_WORDS:
            doc.notes.append(f"This page needs JavaScript to show its content, so only {words} words could be read.")
        else:
            doc.notes.append(
                f"Only {words} words could be extracted; the page may need JavaScript, a login or a subscription."
            )
        if words < TINY_WORDS:
            desc = clean_text(meta.get("og:description") or meta.get("description") or tmeta.get("description") or "")
            if desc and desc not in doc.plain_text():
                doc.blocks.append(Paragraph(text=desc))


# --------------------------------------------------------------------------------------
# URL -> Document
# --------------------------------------------------------------------------------------


def _title_from_url(url: str) -> str:
    try:
        p = urlsplit(url)
    except ValueError:
        return clean_text(url)
    seg = unquote(p.path.rstrip("/").rsplit("/", 1)[-1]) if p.path.strip("/") else ""
    seg = re.sub(r"\.(?:html?|php|aspx?|pdf|txt|epub)$", "", seg, flags=re.I)
    seg = re.sub(r"[-_+]+", " ", seg).strip()
    host = _host(url)
    if seg and re.search(r"[A-Za-z]{3}", seg):
        return clean_text(seg)[:TITLE_MAX_CHARS]
    return host or clean_text(url)[:TITLE_MAX_CHARS]


def note_document(url: str, note: str, *, title: str = "") -> Document:
    """A Document that only explains what went wrong (no blocks), keeping the link."""
    return Document(
        title=clean_text(title) or _title_from_url(url),
        blocks=[],
        site=_host(url),
        source_url=url,
        notes=[note],
    )


_HTTP_EXPLANATIONS = {
    401: "The page needs a login",
    402: "The page needs a subscription",
    403: "The site refused access",
    404: "The page was not found",
    410: "The page no longer exists",
    429: "The site is rate-limiting requests",
    451: "The page is unavailable for legal reasons",
}


def _blocked_note(reason: str) -> str:
    m = re.fullmatch(r"HTTP (\d{3})", reason)
    if m:
        code = int(m.group(1))
        what = _HTTP_EXPLANATIONS.get(code) or (
            "The site had a server error" if code >= 500 else "The site answered with an error"
        )
        return f"{what} ({reason}). Open the link on your phone instead."
    return f"The site blocked the download ({reason}). Nothing was bypassed; open the link on your phone instead."


#: What :func:`url_to_document` reports to its ``on_phase`` callback.
PHASE_FETCHING = "fetching the page"
PHASE_CONVERTING_PDF = "converting the PDF"

PhaseCallback = Callable[[str], None]


def _report_phase(on_phase: PhaseCallback | None, what: str) -> None:
    if on_phase is None:
        return
    try:
        on_phase(what)
    except Exception:  # pragma: no cover - a caller's bookkeeping must not fail the fetch
        log.exception("on_phase callback failed")


async def _pdf_document(
    data: bytes, *, url: str, title_hint: str, opts: Options, on_phase: PhaseCallback | None = None
) -> Document:
    if not opts.pdf_enabled:
        return note_document(
            url, "This link is a PDF, and PDF support is turned off in the app options.", title=title_hint
        )
    try:
        from .pdf import pdf_to_document_async
    except ImportError:
        return note_document(url, "This link is a PDF, and PDF support is not available.", title=title_hint)
    ocr = bool(getattr(opts, "pdf_ocr", False))
    # Cancelled while converting (the caller's budget ran out), the phase stays
    # "converting the PDF", which is what the caller's timeout message then says.
    _report_phase(on_phase, PHASE_CONVERTING_PDF)
    try:
        # Not asyncio.to_thread(pdf_to_document): a cancelled thread cannot be stopped, so
        # when the caller's budget ran out the worker kept converting and held one of the
        # converter slots. The async call kills the worker when it is cancelled.
        doc = await pdf_to_document_async(
            data,
            title_hint=title_hint,
            source_url=url,
            ocr=ocr,
            timeout=PDF_OCR_TIMEOUT_SECONDS if ocr else PDF_TIMEOUT_SECONDS,
        )
    except Exception:
        log.exception("PDF extraction failed for %s", url)
        doc = note_document(url, "This link is a PDF that could not be read.", title=title_hint)
    _report_phase(on_phase, PHASE_FETCHING)
    return doc


_REFRESH_URL_KEY_RE = re.compile(r"(?i)url\s*=")
# apple.news pages are stubs: og:title/og:description plus this script call.
_APPLE_NEWS_RE = re.compile(r"""redirectToUrlAfterTimeout\(\s*["'](https?://[^"'\s<>]+)["']""")
_LINK_TAG_RE = re.compile(r"<link\b" + _TAG_REST, re.I)
_FOLLOW_MIN_SECONDS = 2.0  # budget left below which a stub's target is not fetched


def _refresh_url(content: str) -> str:
    """The URL in a meta refresh ("0; url=https://...", "0;URL='/x'", "0;https://...");
    ``""`` for a bare delay ("300" only reloads the page)."""
    rest = content.strip().lstrip("0123456789.").lstrip()
    if rest[:1] in (";", ","):
        rest = rest[1:].lstrip()
    elif not _REFRESH_URL_KEY_RE.match(rest):
        return ""  # nothing but a delay
    key = _REFRESH_URL_KEY_RE.match(rest)
    if key:
        rest = rest[key.end() :].lstrip()
    rest = rest[1:] if rest[:1] in ("'", '"') else rest
    return re.split(r"""['"\s]""", rest, maxsplit=1)[0]


def _canonical_link(html: str) -> str:
    m = _HEAD_END_RE.search(html, 0, 600_000)
    for tag in _LINK_TAG_RE.findall(html[: m.start() if m else 300_000]):
        attrs = _tag_attrs(tag)
        if "canonical" in (attrs.get("rel") or "").lower().split() and attrs.get("href"):
            return attrs["href"].strip()
    return ""


def _stub_target(html: str, url: str, meta: dict[str, str]) -> str:
    """Where a redirect stub sends the reader, or ``""``.

    Browsers follow a ``<meta http-equiv="refresh">`` URL and Apple News' (apple.news)
    ``redirectToUrlAfterTimeout("...")`` script; a ``rel=canonical`` link or ``og:url``
    naming *another* site points at the real article too. Only http(s) targets other
    than ``url`` itself count.
    """
    here = _fetch.clean_url(url)
    candidates: list[tuple[str, bool]] = []  # (raw URL, only when on another host)
    refresh = _refresh_url(meta.get("refresh", ""))
    if refresh:
        candidates.append((refresh, False))
    m = _APPLE_NEWS_RE.search(html, 0, 300_000)
    if m:
        candidates.append((m.group(1), False))
    candidates += [(c, True) for c in (_canonical_link(html), meta.get("og:url", "")) if c]
    for raw, other_host_only in candidates:
        try:
            target = _fetch.clean_url(urljoin(url, raw.strip()))
            scheme = urlsplit(target).scheme
        except ValueError:
            continue
        if scheme not in ("http", "https") or target == here:
            continue
        if other_host_only and _host(target) == _host(url):
            continue
        return target
    return ""


def _html_result_to_document(res: _fetch.FetchResult) -> tuple[Document, str]:
    """The page's Document, and where to go next if it is only a redirect stub."""
    text = _fetch.decode_html(res.body, res.charset)
    doc = html_to_document(text, url=res.url, lang_hint=res.headers.get("content-language", ""))
    follow = _stub_target(text, res.url, _meta_tags(text)) if doc.word_count() < SHORT_WORDS else ""
    return doc, follow


def _text_result_to_document(res: _fetch.FetchResult, title_hint: str) -> Document:
    text = _fetch.decode_bytes(res.body, res.charset, html=False)
    return text_to_document(text, title=title_hint, source_url=res.url)


async def _document_from_result(
    res: _fetch.FetchResult, *, opts: Options, title_hint: str, on_phase: PhaseCallback | None = None
) -> tuple[Document, str]:
    """Route a fetched response by content kind; also return a redirect stub's target
    URL (or ``""``). Decoding and extraction are CPU work (charset detection alone can
    take seconds on a big body), so they run in a thread."""
    if res.blocked:
        return note_document(res.url, _blocked_note(res.blocked), title=title_hint), ""
    kind = res.kind
    if kind == "html":
        doc, follow = await asyncio.to_thread(_html_result_to_document, res)
        if not doc.title:
            doc.title = clean_text(title_hint) or _title_from_url(res.url)
        return doc, follow
    if kind == "text":
        doc = await asyncio.to_thread(_text_result_to_document, res, title_hint)
        if not doc.title:
            doc.title = _title_from_url(res.url)
        if not doc.blocks:
            doc.notes.append("The linked text file is empty.")
        return doc, ""
    if kind == "pdf":
        return await _pdf_document(res.body, url=res.url, title_hint=title_hint, opts=opts, on_phase=on_phase), ""
    if kind == "epub":
        note = "This link is an EPUB book. Books are not converted; copy the file to the X3 directly."
        return note_document(res.url, note, title=title_hint), ""
    what = {"image": "an image", "media": "an audio or video file"}.get(
        kind, f"a file of type {res.content_type or 'unknown'}"
    )
    return note_document(res.url, f"This link points to {what}, which has no text to send.", title=title_hint), ""


def _finish(doc: Document, *, url: str, title_hint: str) -> Document:
    if not doc.source_url:
        doc.source_url = url
    if not doc.title:
        doc.title = clean_text(title_hint) or _title_from_url(doc.source_url or url)
    if not doc.site:
        doc.site = _host(doc.source_url or url)
    return doc


async def url_to_document(
    url: str, *, opts: Options, title_hint: str = "", on_phase: PhaseCallback | None = None
) -> Document:
    """Fetch ``url`` and turn it into a Document. Never raises for remote problems.

    Known sites go through their APIs first (GitHub README, Wikipedia, Substack,
    Bluesky, X); if that fails the page itself is fetched. All network work shares one
    budget of ``opts.fetch_timeout_seconds``. HTML is extracted in a worker thread;
    PDFs go to :func:`sendtox3.extract.pdf.pdf_to_document_async`; plain text to
    :func:`~.text.text_to_document`; EPUB, images and media yield a note. A short page
    that is only a redirect stub (meta refresh, apple.news, a canonical link to another
    site) is followed once, through the same checks, and replaced by its target when
    that has more text.

    ``on_phase`` (optional) is called with :data:`PHASE_CONVERTING_PDF` when a linked
    PDF starts converting and with :data:`PHASE_FETCHING` when that is done, so that a
    caller whose own budget runs out can say which step it ran out in.
    """
    target = _fetch.clean_url(url)
    try:
        scheme = urlsplit(target).scheme
    except ValueError:
        scheme = ""
    if scheme not in ("http", "https"):
        return note_document(target or url, "Only http and https links can be fetched.", title=title_hint)

    budget = float(opts.fetch_timeout_seconds or 20)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget

    def remaining() -> float:
        return max(1.0, deadline - loop.time())

    from . import sites  # imported here: sites uses this module's HTML helpers

    async def api_fetch(api_url: str, *, headers: dict[str, str] | None = None) -> _fetch.FetchResult:
        return await _fetch.fetch_url(
            api_url, timeout=remaining(), allow_private=opts.allow_private_urls, headers=headers
        )

    async def from_link(link: str, *, follow_stubs: bool) -> Document:
        if sites.match_site(link) is not None:
            api_doc: Document | None = None
            try:
                async with asyncio.timeout(max(1.0, min(remaining(), budget * 0.6))):
                    api_doc = await sites.site_document(link, fetch=api_fetch)
            except TimeoutError:
                log.info("site API timed out for %s", link)
            except Exception:
                log.exception("site API failed for %s", link)
            if api_doc is not None and api_doc.blocks:
                return _finish(api_doc, url=link, title_hint=title_hint)

        try:
            res = await _fetch.fetch_url(
                link, timeout=remaining(), allow_private=opts.allow_private_urls, max_pdf_bytes=PDF_MAX_BYTES
            )
        except _fetch.FetchError as exc:
            return note_document(link, f"Could not download the link: {exc.reason}", title=title_hint)
        try:
            doc, stub_target = await _document_from_result(res, opts=opts, title_hint=title_hint, on_phase=on_phase)
        except Exception:
            log.exception("extraction failed for %s", res.url)
            return note_document(res.url, "The page could not be converted to text.", title=title_hint)
        doc = _finish(doc, url=res.url, title_hint=title_hint)
        if follow_stubs and stub_target and deadline - loop.time() >= _FOLLOW_MIN_SECONDS:
            log.info("%s is a redirect stub; following it to %s", res.url, stub_target)
            target_doc = await from_link(stub_target, follow_stubs=False)  # once, never in a loop
            if target_doc.word_count() > doc.word_count():
                return target_doc
        return doc

    return await from_link(target, follow_stubs=True)
