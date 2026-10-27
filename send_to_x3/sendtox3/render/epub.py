"""Document(s) -> minimal, valid EPUB 3 (+ EPUB 2 NCX) bytes (ARCHITECTURE §6.2).

The output is shaped by CrossPoint's EPUB pipeline, which parses chapters with expat
(strict XML, case-sensitive tag names, no named entities without a DTD), ignores
``<pre>`` and ``white-space``, glues text that sits directly before a block tag onto the
next block, and labels the status bar with the TOC entry of the current chapter:

- lowercase tags only (``h1 h2 h3 p ul ol li blockquote hr i``), every text run inside a
  block element, raw UTF-8 text with only ``&amp; &lt; &gt; &quot; &#39;`` escapes;
- Preformatted -> one ``<p>`` per line, leading spaces as U+00A0;
- nested lists inside the parent ``<li>``;
- no long URLs in the body: CrossPoint breaks a hyphenated "word" only at its hyphens,
  so a URL slug overflows the line. Long links become ``[host]`` (the document's own
  URL is in ``dc:source``, and the TXT rendition keeps every link in full);
- a chapter with nothing left to read says so in one paragraph, and never shows two
  ``<hr/>`` in a row;
- nav (``epub:type="toc"``) and NCX both list every chapter, the nav is not in the spine;
- ``mimetype`` first and stored, other entries deflated, fixed timestamps, no ZIP64.

Output is deterministic: the same input gives byte-identical EPUBs (needed for
auto-delivery replays), so nothing here reads the clock unless ``modified`` says so.
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
import zipfile
from collections import Counter
from datetime import date, datetime, timezone
from typing import Sequence, Union
from urllib.parse import parse_qsl, unquote, urlsplit

from lxml import etree

from ..textmodel import Document, Heading, ListItem, Paragraph, Preformatted, Quote, Rule
from .txt import MIDDOT_SEP, clean_url_text, host_of, inline_text, iso_date, meta_parts, pre_lines

__all__ = ["render_epub", "digest_title", "MAX_TITLE_BYTES", "DIGEST_CREATOR"]

MAX_TITLE_BYTES = 500  # dc:title cap (firmware clamps metadata at 512 bytes)
DIGEST_CREATOR = "Send to X3"
STYLE_CSS = "blockquote{margin-left:1.5em;margin-right:1.5em}\n"
_UNTITLED = "Untitled"
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_DEFAULT_MODIFIED = datetime(1980, 1, 1, tzinfo=timezone.utc)
_NBSP = "\u00a0"

Modified = Union[datetime, int, float, None]

# --------------------------------------------------------------------------------------
# Escaping and small helpers
# --------------------------------------------------------------------------------------

_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})


def _x(s: str) -> str:
    """XML-escape text/attribute content. Only XML's predefined entities and numeric
    references are produced, never HTML named entities."""
    return s.translate(_ESCAPES)


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _cap(s: str, max_bytes: int) -> str:
    if _utf8_len(s) <= max_bytes:
        return s
    out = s.encode("utf-8")[: max_bytes - 3].decode("utf-8", "ignore").rstrip()
    return out + "\u2026"


def _lang(tag: str) -> str:
    """A well-formed BCP 47 tag (language[-Script][-REGION]) or ``en``."""
    parts = [p for p in re.split(r"[-_]", (tag or "").strip()) if p]
    if not parts or not re.fullmatch(r"[A-Za-z]{2,3}", parts[0]):
        return "en"
    out = [parts[0].lower()]
    rest = parts[1:]
    if rest and re.fullmatch(r"[A-Za-z]{4}", rest[0]):
        out.append(rest.pop(0).title())
    if rest and re.fullmatch(r"[A-Za-z]{2}|[0-9]{3}", rest[0]):
        out.append(rest.pop(0).upper())
    return "-".join(out)


def _w3c_date(published: str) -> str:
    """``YYYY[-MM[-DD]]`` that is a real calendar date (epubcheck checks dc:date)."""
    d = iso_date(published)
    if len(d) == 10:
        try:
            date.fromisoformat(d)
        except ValueError:
            d = d[:7]
    return d


def _modified_utc(modified: Modified, docs: Sequence[Document]) -> datetime:
    if modified is None:
        best = ""
        for doc in docs:
            d = _w3c_date(doc.published)
            if d > best:
                best = d
        if not best:
            return _DEFAULT_MODIFIED
        y, m, dd = (best.split("-") + ["1", "1"])[:3]
        return datetime(int(y), int(m), int(dd), tzinfo=timezone.utc)
    if isinstance(modified, bool):
        raise TypeError("modified must be a datetime or unix seconds")
    if isinstance(modified, (int, float)):
        return datetime.fromtimestamp(int(modified), timezone.utc)
    if isinstance(modified, datetime):
        if modified.tzinfo is None:
            modified = modified.astimezone()  # naive = local time
        return modified.astimezone(timezone.utc).replace(microsecond=0)
    raise TypeError("modified must be a datetime or unix seconds")


def digest_title(n: int, when: Modified = None) -> str:
    """``Inbox: <n> items (<YYYY-MM-DD HH:MM>)`` in local time; without ``when`` the
    time is omitted (the renderer never reads the clock itself)."""
    label = f"Inbox: {n} item{'s' if n != 1 else ''}"
    if when is None:
        return label
    local = _modified_utc(when, ()).astimezone()
    return f"{label} ({local:%Y-%m-%d %H:%M})"


def _doc_key(doc: Document) -> str:
    url = clean_url_text(doc.source_url)
    if url:
        return "url:" + url
    body = hashlib.sha256(doc.plain_text().encode("utf-8", "replace")).hexdigest()
    return f"text:{inline_text(doc.title)}\n{body}"


def _identifier(identifier: str, docs: Sequence[Document]) -> str:
    ident = inline_text(identifier).replace(" ", "")
    if ident:
        try:
            return f"urn:uuid:{uuid.UUID(ident)}"
        except ValueError:
            return ident if ":" in ident else f"urn:sendtox3:{ident}"
    key = _doc_key(docs[0]) if len(docs) == 1 else "digest:" + "\n".join(_doc_key(d) for d in docs)
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, key)}"


# --------------------------------------------------------------------------------------
# Chapter XHTML
# --------------------------------------------------------------------------------------


#: Characters that end a link in running text: nobody writes them inside a URL, and
#: they usually start the text that follows it (an em dash, an ellipsis, curly quotes,
#: guillemets, CJK punctuation).
_URL_STOP = ("\u2014\u2013\u2026\u201c\u201d\u2018\u00ab\u00bb"
             "\u3001\u3002\u300c\u300d\u300e\u300f\uff01\uff08\uff09\uff0c\uff1a\uff1b\uff1f")
#: An English clitic attached to a link (".../slug's page", ".../slug's."; also with a
#: curly apostrophe): it ends the link unless the URL goes on after it, as in
#: ``/wiki/Ender's_Game`` or ``/Bob's.html``.
_CLITIC = r"['\u2019](?:s|d|ll|re|ve|m)(?![\w/%=&#~+\-]|[.?]\w)"
#: Links in running text. The X3 cannot follow them, and CrossPoint breaks a word that
#: contains a hyphen only at its hyphens, so a hyphen-free stretch wider than the line
#: (``https://www.theguardian.com/world/2026/oct/25/why-...``) runs past the margin.
#: A link may follow a non-Latin letter directly (Chinese text, say).
_URL_RE = re.compile(rf"(?<![A-Za-z0-9_])(?:https?://|www\.)(?:(?!{_CLITIC})[^\s<>\"{_URL_STOP}])+", re.I)
_URL_TRAIL = ".,;:!?'\"\u2019\u201d\u00bb)]}"
MAX_INLINE_URL_CHARS = 30  # longer links are shown as [host]


def _split_trail(url: str) -> tuple[str, str]:
    """``(url, trailing punctuation)``; a closing parenthesis stays when balanced."""
    end = len(url)
    while end and url[end - 1] in _URL_TRAIL:
        if url[end - 1] == ")" and url[:end].count("(") >= url[:end].count(")"):
            break
        end -= 1
    return url[:end], url[end:]


def _short_url(m: re.Match) -> str:
    url, trail = _split_trail(m.group(0))
    if len(url) <= MAX_INLINE_URL_CHARS:
        return m.group(0)
    host = host_of(url if "://" in url else "http://" + url)
    return f"[{host or 'link'}]{trail}"


def _body_text(text: str) -> str:
    """Running text for the EPUB body: links longer than :data:`MAX_INLINE_URL_CHARS`
    shortened to ``[host]``."""
    return _URL_RE.sub(_short_url, text)


def _without_tracking(url: str) -> str:
    """``url`` without tracking parameters, by the same rules as the ingest's URL
    cleaning (:func:`sendtox3.extract.fetch.clean_url`, imported late so the renderer
    does not need the fetcher); without it, :func:`_page_key` still drops ``utm_*``."""
    try:
        from ..extract.fetch import clean_url

        return clean_url(url)
    except Exception:  # noqa: BLE001 - rendering must not depend on the fetcher
        return url


def _page_key(url: str) -> str:
    """Which page ``url`` names: host (without ``www.``), path, the query string without
    tracking parameters and in sorted order, and a ``#/...`` or ``#!...`` route. The
    scheme, a trailing slash and other fragments do not matter."""
    try:
        parts = urlsplit(_without_tracking(url if "://" in url else "http://" + url))
        host = (parts.hostname or "").removeprefix("www.")
    except ValueError:
        return ""
    if not host:
        return ""
    key = host + unquote(parts.path).rstrip("/")
    query = sorted(kv for kv in parse_qsl(parts.query, keep_blank_values=True)
                   if not kv[0].lower().startswith("utm_"))
    if query:
        key += "?" + "&".join(f"{k}={v}" for k, v in query)
    if parts.fragment.startswith(("/", "!")):
        key += "#" + unquote(parts.fragment)
    return key


def _is_source_link(text: str, source_url: str) -> bool:
    """A paragraph that is only the document's own URL (the Source line already says it).
    A link to another page of the same site (``item?id=2``, ``watch?v=...``) is kept."""
    m = _URL_RE.fullmatch(text)
    if not m or not source_url:
        return False
    key = _page_key(_split_trail(m.group(0))[0])
    return bool(key) and key == _page_key(clean_url_text(source_url))


def _pre_line_xhtml(line: str) -> str:
    """Leading spaces -> NBSP; inner runs of n spaces -> n-1 NBSP + one breakable space."""
    stripped = line.lstrip(" ")
    lead = _NBSP * (len(line) - len(stripped))
    inner = re.sub(r" {2,}", lambda m: _NBSP * (len(m.group(0)) - 1) + " ", stripped)
    return _x(lead + inner) if (lead + inner) else _NBSP


class _ListWriter:
    """Emits ``ul``/``ol`` trees with nested lists inside the parent ``li``."""

    def __init__(self, out: list[str]):
        self.out = out
        self.stack: list[list] = []  # [tag, li_open]

    def close(self, keep: int = 0) -> None:
        while len(self.stack) > keep:
            tag, li_open = self.stack.pop()
            if li_open:
                self.out.append("</li>")
            self.out.append(f"</{tag}>")

    def item(self, item: ListItem, text: str) -> None:
        want = "ol" if item.ordered else "ul"
        # Never open more than one level at a time: a jump from depth 0 to 2 nests one
        # level, so every nested list sits inside a real, already-open parent <li>.
        depth = min(max(0, int(item.depth)), len(self.stack))
        self.close(depth + 1)
        if len(self.stack) == depth + 1 and self.stack[-1][0] != want:
            self.close(depth)
        if len(self.stack) < depth + 1:
            start = ""
            if want == "ol" and isinstance(item.index, int) and not isinstance(item.index, bool):
                if item.index != 1 and 0 <= item.index <= 1_000_000:
                    start = f' start="{item.index}"'
            self.out.append(f"<{want}{start}>")
            self.stack.append([want, False])
        top = self.stack[-1]
        if top[1]:
            self.out.append("</li>")
        self.out.append(f"<li>{_x(text)}")
        top[1] = True


#: A document whose text exceeds this is split into several spine items (chapters) so
#: the X3 opens it quickly and the TOC/status bar show real section names.
SPLIT_THRESHOLD_BYTES = 60_000
_MIN_PART_BYTES = 8_000  # do not start a new chapter at a heading before this much text
_MAX_PART_BYTES = 90_000  # force a split at a paragraph boundary beyond this


def _block_bytes(block) -> int:
    return _utf8_len(getattr(block, "text", "")) + 8


def _split_blocks(doc: Document, title: str) -> list[tuple[str, list]]:
    """``[(label, blocks), ...]``: one part for normal documents; for long ones, parts
    starting at level-2 headings (or at paragraph boundaries when a section is huge)."""
    blocks = list(doc.blocks)
    if sum(_block_bytes(b) for b in blocks) <= SPLIT_THRESHOLD_BYTES:
        return [(title, blocks)]
    parts: list[tuple[str, list]] = []
    cur: list = []
    cur_label = title
    size = 0
    cont = 1
    for block in blocks:
        at_heading = isinstance(block, Heading) and block.level <= 2 and inline_text(block.text)
        forced = size >= _MAX_PART_BYTES and isinstance(block, (Paragraph, Heading, Rule))
        if cur and ((at_heading and size >= _MIN_PART_BYTES) or forced):
            parts.append((cur_label, cur))
            if at_heading:
                cur_label = _cap(inline_text(block.text), MAX_TITLE_BYTES)
                cont = 1
            else:
                cont += 1
                base = parts[-1][0]
                base = re.sub(r" \(continued(?: \d+)?\)$", "", base)
                cur_label = f"{base} (continued{'' if cont == 2 else f' {cont - 1}'})"
            cur, size = [], 0
        cur.append(block)
        size += _block_bytes(block)
    if cur:
        parts.append((cur_label, cur))
    return parts


def _empty_body_text(host: str) -> str:
    """What a chapter says when nothing readable is left (only its own link, say)."""
    if host:
        return f"There is no text here, only a link to {host}. Open it on another device to read it."
    return "There is no text here."


def _chapter_body(doc: Document, title: str, blocks: list | None = None,
                  first: bool = True, last: bool = True) -> str:
    out: list[str] = []
    content = 0  # body blocks with text written so far

    def rule() -> None:  # never two <hr/> in a row, nor one at the top of a chapter
        if out and out[-1] != "<hr/>":
            out.append("<hr/>")

    if first:
        out.append(f"<h1>{_x(title)}</h1>")
        byline, site, when = meta_parts(doc)
        if byline:
            out.append(f"<p><i>{_x(byline)}</i></p>")
        site_date = MIDDOT_SEP.join(p for p in (site, when) if p)
        if site_date:
            out.append(f"<p><i>{_x(site_date)}</i></p>")
        for note in doc.notes:
            text = _body_text(inline_text(note))
            if text:
                out.append(f"<p><i>Note: {_x(text)}</i></p>")
        rule()

    lists = _ListWriter(out)
    in_quote = False
    for block in (doc.blocks if blocks is None else blocks):
        if isinstance(block, ListItem):
            text = _body_text(inline_text(block.text))
            if not text:
                continue
            if in_quote:
                out.append("</blockquote>")
                in_quote = False
            lists.item(block, text)
            content += 1
            continue
        lists.close()
        if isinstance(block, Quote):
            text = _body_text(inline_text(block.text))
            if not text:
                continue
            if not in_quote:
                out.append("<blockquote>")
                in_quote = True
            out.append(f"<p>{_x(text)}</p>")
            content += 1
            continue
        if in_quote:
            out.append("</blockquote>")
            in_quote = False
        if isinstance(block, Heading):
            text = _body_text(inline_text(block.text))
            if text:
                tag = "h3" if block.level >= 3 else "h2"  # the title owns the only <h1>
                out.append(f"<{tag}>{_x(text)}</{tag}>")
                content += 1
        elif isinstance(block, Paragraph):
            text = inline_text(block.text)
            if text and not _is_source_link(text, doc.source_url):
                out.append(f"<p>{_x(_body_text(text))}</p>")
                content += 1
        elif isinstance(block, Preformatted):
            lines = [f"<p>{_pre_line_xhtml(line)}</p>" for line in pre_lines(block.text)]
            out.extend(lines)
            content += len(lines)
        elif isinstance(block, Rule):
            rule()
    lists.close()
    if in_quote:
        out.append("</blockquote>")

    host = host_of(doc.source_url)
    if first and last and not content:
        # e.g. the only block was the document's own URL, dropped above
        out.append(f"<p>{_x(_empty_body_text(host))}</p>")
    if host and last:
        rule()
        out.append(f"<p><i>Source: {_x(host)}</i></p>")
    if not out:
        out.append(f"<p>{_NBSP}</p>")
    return "\n".join(out)


_XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'


def _chapter_xhtml(doc: Document, title: str, lang: str, blocks: list | None = None,
                   first: bool = True, last: bool = True) -> str:
    return (
        _XML_DECL
        + "<!DOCTYPE html>\n"
        + f'<html xmlns="http://www.w3.org/1999/xhtml" lang="{lang}" xml:lang="{lang}">\n'
        + "<head>\n"
        + '<meta charset="utf-8"/>\n'
        + f"<title>{_x(title)}</title>\n"
        + '<link rel="stylesheet" type="text/css" href="../style.css"/>\n'
        + "</head>\n"
        + "<body>\n"
        + _chapter_body(doc, title, blocks, first, last)
        + "\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------------------
# Package documents
# --------------------------------------------------------------------------------------

_CONTAINER = (
    _XML_DECL
    + '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    + '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
    + "</container>\n"
)


def _opf(
    *,
    ident: str,
    title: str,
    creator: str,
    lang: str,
    pub_date: str,
    publisher: str,
    sources: list[str],
    modified: datetime,
    chapters: list[tuple[str, str, str, str]],
) -> str:
    meta = [
        f'    <dc:identifier id="bookid">{_x(ident)}</dc:identifier>',
        f"    <dc:title>{_x(title)}</dc:title>",
    ]
    if creator:
        meta.append(f"    <dc:creator>{_x(creator)}</dc:creator>")
    meta.append(f"    <dc:language>{lang}</dc:language>")
    if pub_date:
        meta.append(f"    <dc:date>{pub_date}</dc:date>")
    if publisher:
        meta.append(f"    <dc:publisher>{_x(publisher)}</dc:publisher>")
    meta.extend(f"    <dc:source>{_x(src)}</dc:source>" for src in sources)
    meta.append(f'    <meta property="dcterms:modified">{modified:%Y-%m-%dT%H:%M:%SZ}</meta>')
    manifest = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '    <item id="css" href="style.css" media-type="text/css"/>',
    ]
    manifest.extend(
        f'    <item id="{cid}" href="{href}" media-type="application/xhtml+xml"/>' for cid, href, _, _ in chapters
    )
    spine = [f'    <itemref idref="{cid}"/>' for cid, _, _, _ in chapters]
    return (
        _XML_DECL
        + '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid"'
        + f' xml:lang="{lang}">\n'
        + '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        + "\n".join(meta)
        + "\n  </metadata>\n  <manifest>\n"
        + "\n".join(manifest)
        + '\n  </manifest>\n  <spine toc="ncx">\n'
        + "\n".join(spine)
        + "\n  </spine>\n</package>\n"
    )


def _nav(title: str, lang: str, chapters: list[tuple[str, str, str, str]]) -> str:
    items = "\n".join(f'<li><a href="{href}">{_x(label)}</a></li>' for _, href, label, _ in chapters)
    return (
        _XML_DECL
        + "<!DOCTYPE html>\n"
        + '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
        + f' lang="{lang}" xml:lang="{lang}">\n'
        + f'<head>\n<meta charset="utf-8"/>\n<title>{_x(title)}</title>\n</head>\n<body>\n'
        + '<nav epub:type="toc" id="toc">\n<h1>Contents</h1>\n<ol>\n'
        + items
        + "\n</ol>\n</nav>\n</body>\n</html>\n"
    )


def _ncx(ident: str, title: str, lang: str, chapters: list[tuple[str, str, str, str]]) -> str:
    points = "\n".join(
        f'    <navPoint id="np-{n}" playOrder="{n}"><navLabel><text>{_x(label)}</text></navLabel>'
        f'<content src="{href}"/></navPoint>'
        for n, (_, href, label, _) in enumerate(chapters, 1)
    )
    return (
        _XML_DECL
        + f'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="{lang}">\n'
        + "  <head>\n"
        + f'    <meta name="dtb:uid" content="{_x(ident)}"/>\n'
        + '    <meta name="dtb:depth" content="1"/>\n'
        + '    <meta name="dtb:totalPageCount" content="0"/>\n'
        + '    <meta name="dtb:maxPageNumber" content="0"/>\n'
        + "  </head>\n"
        + f"  <docTitle><text>{_x(title)}</text></docTitle>\n"
        + "  <navMap>\n"
        + points
        + "\n  </navMap>\n</ncx>\n"
    )


# --------------------------------------------------------------------------------------
# Validation and packaging
# --------------------------------------------------------------------------------------

_NAMED_ENTITY_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|#[0-9]+|#x[0-9a-fA-F]+);)")


def _check_xml(name: str, text: str) -> bytes:
    """Fail here, not on the device: well-formed XML, no stray/named entities."""
    data = text.encode("utf-8")
    if _NAMED_ENTITY_RE.search(text):
        raise ValueError(f"{name}: unescaped '&' or named entity")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True)
    try:
        etree.fromstring(data, parser)
    except etree.XMLSyntaxError as e:  # pragma: no cover - would be a renderer bug
        raise ValueError(f"{name}: not well-formed XML: {e}") from e
    return data


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", allowZip64=False) as z:
        zi = zipfile.ZipInfo("mimetype", date_time=_ZIP_EPOCH)
        zi.compress_type = zipfile.ZIP_STORED
        zi.create_system = 3
        zi.external_attr = 0o100644 << 16
        z.writestr(zi, b"application/epub+zip")
        for name, data in entries:
            zi = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3
            zi.external_attr = 0o100644 << 16
            z.writestr(zi, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return bio.getvalue()


def _book_lang(docs: Sequence[Document]) -> str:
    langs = [_lang(d.lang) for d in docs]
    counts = Counter(langs)
    best = max(counts.values())
    return next(lang for lang in langs if counts[lang] == best)


def render_epub(
    docs: list[Document],
    *,
    book_title: str = "",
    identifier: str = "",
    modified: Modified = None,
) -> bytes:
    """Render one document (title = its title) or a digest of several (one spine item
    per document; nav and NCX list them all) as EPUB bytes.

    ``book_title`` overrides the package title (digest default: ``Inbox: <n> items
    (<YYYY-MM-DD HH:MM>)``, the time taken from ``modified``). ``identifier`` becomes
    ``dc:identifier`` (a UUID -> ``urn:uuid:...``, a bare id -> ``urn:sendtox3:<id>``);
    default is a uuid5 of the source URL(s) or title and text. ``modified`` (datetime or
    unix seconds) sets ``dcterms:modified``; default is derived from the documents'
    publication dates, so identical input always gives identical bytes.
    """
    docs = list(docs)
    if not docs:
        raise ValueError("render_epub needs at least one document")
    when = _modified_utc(modified, docs)
    book_lang = _book_lang(docs)
    chapter_titles = [_cap(inline_text(d.title) or _UNTITLED, MAX_TITLE_BYTES) for d in docs]

    if len(docs) == 1:
        doc = docs[0]
        title = inline_text(book_title) or chapter_titles[0]
        byline, site, _ = meta_parts(doc)
        creator = byline or site
        publisher = inline_text(doc.site)
        pub_date = _w3c_date(doc.published)
    else:
        title = inline_text(book_title) or digest_title(len(docs), modified)
        creator = DIGEST_CREATOR
        publisher = ""
        pub_date = f"{when.astimezone():%Y-%m-%d}" if modified is not None else ""
    title = _cap(title, MAX_TITLE_BYTES)
    creator = _cap(creator, MAX_TITLE_BYTES)
    publisher = _cap(publisher, MAX_TITLE_BYTES)
    ident = _identifier(identifier, docs)
    sources = []
    for d in docs:
        url = clean_url_text(d.source_url)
        if url and url not in sources:
            sources.append(url)

    width = max(3, len(str(len(docs))))
    chapters: list[tuple[str, str, str, str]] = []  # (id, href, label, xhtml)
    for n, (doc, label) in enumerate(zip(docs, chapter_titles), 1):
        parts = _split_blocks(doc, label)
        for k, (part_label, blocks) in enumerate(parts):
            cid = f"item-{n:0{width}d}" if k == 0 else f"item-{n:0{width}d}-{k + 1:03d}"
            xhtml = _chapter_xhtml(doc, label, _lang(doc.lang), blocks, first=k == 0, last=k == len(parts) - 1)
            chapters.append((cid, f"text/{cid}.xhtml", part_label, xhtml))

    parts: list[tuple[str, str]] = [
        ("META-INF/container.xml", _CONTAINER),
        (
            "OEBPS/content.opf",
            _opf(
                ident=ident,
                title=title,
                creator=creator,
                lang=book_lang,
                pub_date=pub_date,
                publisher=publisher,
                sources=sources,
                modified=when,
                chapters=chapters,
            ),
        ),
        ("OEBPS/nav.xhtml", _nav(title, book_lang, chapters)),
        ("OEBPS/toc.ncx", _ncx(ident, title, book_lang, chapters)),
    ]
    entries = [(name, _check_xml(name, text)) for name, text in parts]
    entries.append(("OEBPS/style.css", STYLE_CSS.encode("ascii")))
    entries.extend(
        (f"OEBPS/{href}", _check_xml(href, xhtml)) for _, href, _, xhtml in chapters
    )
    return _zip(entries)
