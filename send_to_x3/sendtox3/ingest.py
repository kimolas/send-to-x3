"""IngestRequest -> stored, rendered Item: classification, dedupe, titles, fallback.

Every ingest channel (HTTP API, email, HA share, admin) funnels through
:meth:`Processor.process_all` (``process``, ``process_detailed`` and ``process_many``
wrap it). Whatever goes wrong while reading a share, it is kept as a ``failed`` item
whose fallback document (title, link, error note, any shared text) is rendered and
deliverable, so nothing the user sent is lost. The one exception is the store itself
failing (disk full, database error): then :class:`StorageError` is raised and the
caller retries later.

Each request is written to a spool before it is processed and removed once its items
are stored, so a share accepted shortly before a crash or restart (an HTTP share
answered "still processing", say) is processed at the next start by
:meth:`Processor.recover_pending`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import email.utils
import hashlib
import inspect
import io
import logging
import posixpath
import re
import shutil
import sqlite3
import time
import unicodedata
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from html.entities import html5 as _HTML5_ENTITIES
from typing import AsyncIterator, Callable, Optional
from urllib.parse import unquote, urlsplit

from . import delivery
from .config import Options
from .models import IngestRequest, Item, new_item_id
from .store import SpooledRequest, Store
from .textmodel import Document, Heading, Paragraph, clean_text

log = logging.getLogger(__name__)

#: Budget for every share, from any channel. A link can turn out to be a PDF (scanned,
#: with OCR on) and a large PDF may legitimately take minutes on a Raspberry Pi; the
#: HTTP API answers "processing" after ``web.ingest_api.RESPONSE_WAIT_SECONDS`` and the
#: work continues in the background. Plain page fetches are bounded by
#: ``fetch_timeout_seconds`` anyway.
BACKGROUND_BUDGET_SECONDS = 150.0
#: Extra budget when OCR of scanned PDFs is enabled (tesseract is slow on a Pi).
OCR_EXTRA_BUDGET_SECONDS = 510.0
DEDUPE_WINDOW_SECONDS = 86_400
#: "One link plus at most this many other words" is a link share: the words are the
#: sharer's comment (kept as a note), e.g. an app's "Check out this article <url>".
COMMENT_MAX_WORDS = 30
COMMENT_MAX_CHARS = 300
#: A share of 2..this many links and little else becomes one url item per link.
MAX_LINKS_PER_SHARE = 10
#: A spooled request is tried this many times (the first attempt included) before it is
#: stored as failed: a share that crashes the app must not crash it at every start.
SPOOL_MAX_ATTEMPTS = 3
#: The in-book headline (chapter <h1>, nav label, dc:title) is capped like a Library
#: name (255 bytes); the firmware clamps OPF metadata at 512 bytes.
DISPLAY_TITLE_MAX_BYTES = 255

#: Zip-bomb guard for uploaded EPUBs: the declared size of all members (the zip reader
#: never returns more than a member declares).
_MAX_EPUB_UNCOMPRESSED = 64 * 1024 * 1024
#: Decompressed bytes of spine documents read for the .txt rendition, in all and per
#: document; past them the text copy is cut short (with a note), the EPUB is kept whole.
_MAX_EPUB_READ_BYTES = 24 * 1024 * 1024
_MAX_EPUB_CHAPTER_BYTES = 4 * 1024 * 1024
_MAX_EPUB_PACKAGE_BYTES = 4 * 1024 * 1024  # META-INF/container.xml and the OPF
#: Spine documents larger than this go through the cheap block walker
#: (:func:`_html_leaf_blocks`): the full HTML converter is several times slower and
#: runs in a worker thread that a timed-out share cannot cancel.
_EPUB_FAST_CHAPTER_BYTES = 1024 * 1024
_MAX_EPUB_TEXT_CHARS = 4_000_000  # text kept for an uploaded EPUB's .txt rendition (~700k words)
_MAX_EPUB_SPINE = 2000
_EPUB_NOTE_STOPS_EARLY = "This text copy stops early: the book is very long. The EPUB itself is complete."
_EPUB_NOTE_CHAPTER_CUT = ("This text copy leaves out the end of a very long chapter. "
                          "The EPUB itself is complete.")
_ZIP_READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError)
#: ``PDF converter timeout = remaining budget - this``: the conversion stops (and frees
#: its slot) in time to render and store what it produced, or the reason it failed.
PDF_BUDGET_MARGIN_SECONDS = 10.0
_SPOOL_INLINE_MAX = 256 * 1024  # larger file payloads are spooled in a worker thread
_MAX_FALLBACK_TEXT = 200_000  # characters of raw shared text kept in a failed item
_URL_RE = re.compile(r"""https?://[^\s<>"'`]+""", re.I)
#: Links as extract.text.find_urls sees them (bare "www." hosts too), for the remainder.
_LINK_RE = re.compile(r"""\b(?:https?://|www\.)[^\s<>"'`\u00a0\u3000]+""", re.I)
_TRAILING_PUNCT = ".,;:!?)]}'\""
_COUNTER_SUFFIX_RE = re.compile(r" \([0-9]{1,6}\)$")
#: Words share sheets put around a link ("Check out this article", "Check out @NASA's
#: post:", "Shared via Google News", "via @nytimes"): not a comment worth keeping. Complements the
#: filler extract.text.classify knows ("Check this out", "via ...", "Sent from my ...").
_SHARE_TEMPLATE_RE = re.compile(
    r"(?i)\bcheck\s+out(?:\s+(?:this|that))?(?:\s+(?:article|post|tweet|thread|story|video|link|page))?\b"
    r"|@\w+(?:['’]s)?\s+(?:post|tweet|thread)\b"
    r"|\bshared?\s+(?:from|via|with)\s+(?:\S+\s+){0,3}\S+\s*$|\bvia\s+@?\S+\s*$"
    r"|^\s*shared?\s+(?:from|via|with)\s+(?:\S+\s+){0,3}\S+?(?=\s*[:\-–—|]|\s*$)"
)
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>")


class IngestError(Exception):
    """A user-facing reason why a share could not be turned into readable text."""


class StorageError(Exception):
    """Writing to the store failed (disk full, SQLite I/O error): the share was NOT saved.

    The only exception :meth:`Processor.process` and friends raise. HTTP callers answer
    503; the IMAP and HA workers leave the share where it is and retry later. The
    request stays in the spool, so it is also retried at the next start.
    """


@dataclass
class ProcessResult:
    item: Item
    duplicate: bool = False  # True: an identical share from the last 24 h was returned


# --------------------------------------------------------------------------------------
# Dependency seams. The real implementations belong to the extract/ and render/
# components; they are imported lazily so unit tests can substitute these names.
# --------------------------------------------------------------------------------------


async def url_to_document(url: str, *, opts: Options, title_hint: str = "",
                          on_phase: Optional[Callable[[str], None]] = None) -> Document:
    """``on_phase`` is told what runs now ("converting the PDF" for a link that turns out
    to be a PDF, "fetching the page" again when that is done)."""
    from .extract.html import url_to_document as impl

    return await impl(url, opts=opts, title_hint=title_hint, on_phase=on_phase)


def html_to_document(html: str, *, url: str = "", whole: bool = False) -> Document:
    """``whole``: convert the entire HTML (an email body or attachment) instead of
    extracting an article from it."""
    from .extract.html import html_to_document as impl

    if whole and _takes_kw(impl, "whole"):
        return impl(html, url=url, whole=True)
    return impl(html, url=url)


def html_fragment_to_blocks(fragment: str, *, xml: bool = False) -> list:
    from .extract.html import html_fragment_to_blocks as impl

    return impl(fragment, xml=xml)


def text_to_document(text: str, *, title: str = "", source_url: str = "") -> Document:
    from .extract.text import text_to_document as impl

    return impl(text, title=title, source_url=source_url)


def classify(text: str, url: str = "", title: str = "") -> str:
    from .extract.text import classify as impl

    return impl(text, url, title)


def first_url(text: str) -> str:
    from .extract.text import first_url as impl

    return impl(text)


def find_urls(text: str) -> list[str]:
    from .extract.text import find_urls as impl

    return impl(text)


def derive_title(text: str) -> str:
    from .extract.text import derive_title as impl

    return impl(text)


def pdf_to_document(data: bytes, *, title_hint: str = "", source_url: str = "", ocr: bool = False) -> Document:
    """Blocking converter seam; kept for substitutes (tests). The Processor awaits
    :func:`pdf_to_document_async` unless only this one was replaced."""
    from .extract.pdf import pdf_to_document as impl

    return impl(data, title_hint=title_hint, source_url=source_url, ocr=ocr)


async def pdf_to_document_async(data: bytes, *, title_hint: str = "", source_url: str = "", ocr: bool = False,
                                timeout: Optional[float] = None) -> Document:
    """Cancellable conversion: cancelling it (the share's budget ran out) kills the
    converter's process group at once and frees its slot."""
    from .extract.pdf import pdf_to_document_async as impl

    return await impl(data, title_hint=title_hint, source_url=source_url, ocr=ocr, timeout=timeout)


_DEFAULT_PDF_SEAMS = (pdf_to_document, pdf_to_document_async)


def clean_url(url: str) -> str:
    try:
        from .extract.fetch import clean_url as impl
    except ImportError:  # only used for dedupe keys; a raw URL still works
        return url.strip()
    return impl(url)


def decode_html(body: bytes, charset_hint: str | None) -> str:
    from .extract.fetch import decode_html as impl

    return impl(body, charset_hint)


def unique_title(title: str, taken: set[str]) -> str:
    from .render.names import unique_title as impl

    return impl(title, taken)


def render_txt(doc: Document) -> bytes:
    from .render.txt import render_txt as impl

    return impl(doc)


def render_epub(docs: list[Document], *, book_title: str = "", identifier: str = "") -> bytes:
    from .render.epub import render_epub as impl

    return impl(docs, book_title=book_title, identifier=identifier)


# --------------------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------------------


def item_identifier(item_id: str) -> str:
    """Stable ``urn:uuid`` for an item's EPUB (dc:identifier)."""
    return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "sendtox3:item:" + item_id))


def url_dedupe_key(url: str) -> str:
    """``url:<host><path>?<query>`` after tracking cleanup; scheme, www., port, fragment
    and a trailing slash do not make two shares different."""
    cleaned = clean_url(url.strip())
    try:
        p = urlsplit(cleaned)
        host = (p.hostname or "").lower()
        port = p.port
    except ValueError:
        return "url:" + cleaned
    if host.startswith("www."):
        host = host[4:]
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return "url:" + netloc + path + (("?" + p.query) if p.query else "")


def text_dedupe_key(text: str) -> str:
    norm = clean_text(text).casefold()
    return "text:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()


def bytes_dedupe_key(data: bytes) -> str:
    return "file:" + hashlib.sha256(data).hexdigest()


def _urls_in(text: str) -> list[str]:
    return [u.rstrip(_TRAILING_PUNCT) for u in _URL_RE.findall(text or "")]


def _remainder(text: str) -> str:
    """The shared text without its URLs, whitespace-normalized."""
    return " ".join(_LINK_RE.sub(" ", text or "").split())


def _is_short(remainder: str) -> bool:
    return 0 < len(remainder) <= COMMENT_MAX_CHARS and len(remainder.split()) <= COMMENT_MAX_WORDS


def _short_or_empty(remainder: str) -> bool:
    return not remainder or _is_short(remainder)


#: A link without a scheme, as :func:`sendtox3.extract.fetch.clean_url` takes one
#: ("example.com/article", "www.example.com", "example.com:8080/x"), whose last host
#: label looks like a public top-level domain: letters only (as for the bare host names
#: in extract/email.py) or an IDN ``xn--`` label, not a file extension (``notes.txt``).
_BARE_HOST_URL_RE = re.compile(
    r"(?i)(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?P<tld>[a-z]{2,63}|xn--[a-z0-9-]{1,59})"
    r"(?::\d{1,5})?(?:[/?#]\S*)?"
)
_NOT_TLDS = frozenset(
    "txt pdf html htm xhtml php asp aspx jsp cgi epub azw azw3 doc docx odt rtf xls xlsx ods csv ppt pptx "
    "json xml yaml yml ini cfg conf log jpg jpeg png gif webp heic bmp tif tiff svg ico wav flac aac ogg avi "
    "mkv webm exe dmg pkg deb rpm apk iso tar gz tgz bz2 rar js css".split()
)


def _web_url(url: str) -> str:
    """``url`` (a request's ``url`` field, not free text) if it is a web link, else ``""``:
    ``mailto:``, ``tel:`` and the like are nothing to fetch or to cite as a source. A
    link without a scheme gets ``https://``: a ``www.`` host, or a host name ending in a
    public-looking top-level domain (``example.com/article``, see
    :data:`_BARE_HOST_URL_RE`)."""
    u = (url or "").strip()
    if re.match(r"(?i)https?://\S", u):
        return u
    if re.fullmatch(r"(?i)www\.[^\s/.]+\.\S+", u):
        return "https://" + u
    m = _BARE_HOST_URL_RE.fullmatch(u)
    if m and m.group("tld").lower() not in _NOT_TLDS:
        return "https://" + u
    return ""


def _links(text: str) -> list[str]:
    """Distinct web links in ``text`` in order (two spellings of one page count once)."""
    try:
        found = find_urls(text)
    except Exception:  # noqa: BLE001 - keep ingest working if the text module misbehaves
        log.exception("find_urls failed")
        found = _urls_in(text)
    out: list[str] = []
    seen: set[str] = set()
    for u in found:
        key = url_dedupe_key(u)
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def _has_link(links: list[str], url: str) -> bool:
    key = url_dedupe_key(url)
    return any(url_dedupe_key(u) == key for u in links)


def _is_link_share(text: str, url: str, title: str, site: str = "") -> bool:
    """True when ``text`` shared next to ``url`` adds nothing to it: no words, filler
    ("via ...", "Check this out") or (a part of) the page ``title`` (see
    :func:`matches_title`; ``site`` is the page's site name, if known)."""
    rem = _remainder(text)
    if not rem:
        return True
    if matches_title(rem, title, url=url, site=site):
        return True
    rest = " ".join(_SHARE_TEMPLATE_RE.sub(" ", rem).split()).strip(" .,:;!?-–—|·•")
    return not rest or _classify(f"{rest} {url}", url, title) == "url"


def _comment_of(remainder: str, url: str, title: str) -> str:
    """The sharer's own words next to a link, or ``""`` when they add nothing."""
    return "" if _is_link_share(remainder, url, title) else remainder.rstrip(" :-–—|")


def _takes_kw(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _html_document(html: str, url: str, whole: bool) -> Document:
    if whole and _takes_kw(html_to_document, "whole"):
        return html_to_document(html, url=url, whole=True)
    return html_to_document(html, url=url)


def _cap_utf8(s: str, max_bytes: int) -> str:
    if len(s.encode("utf-8")) <= max_bytes:
        return s
    cut = s.encode("utf-8")[: max(0, max_bytes - 3)].decode("utf-8", "ignore").rstrip()
    return cut + "…" if cut else ""


#: " | Site", " - Site", " – Site", " — Site", " · Site" at the end of a headline.
_DISPLAY_SUFFIX_RE = re.compile(r"^(?P<head>.*\S)\s+(?P<sep>[|\-–—·])\s+(?P<tail>\S.*?)\s*$")
_DISPLAY_PREFIX_RE = re.compile(r"^(?P<head>[^:]*?\S):\s+(?P<tail>\S.*)$")
#: Sites whose name ends many page titles whatever the page's own site name says
#: (Medium only after "|": "Steak: rare - medium" is about a steak).
_WELL_KNOWN_SITES = ("wikipedia", "github", "youtube")
_DISPLAY_MIN_HEAD = 3


def _site_key(s: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s or "").casefold())


def _site_keys(site: str, url: str = "") -> set[str]:
    """Spellings of a site's name: ``site`` itself, and for a host (``site`` or the
    host of ``url``) the host and its first label (``bun.sh`` -> ``bunsh``, ``bun``)."""
    keys = {_site_key(site)}
    hosts = [_host(url)] if url else []
    s = (site or "").strip().lower()
    if s and " " not in s and "." in s:
        hosts.append(re.sub(r"^[a-z][a-z0-9+.\-]*://", "", s).split("/")[0])
    for host in hosts:
        host = re.sub(r"^(?:www\d*|m|amp|mobile)\.", "", host or "")
        keys.add(_site_key(host))
        first = host.split(".")[0]
        if len(first) >= 3:
            keys.add(_site_key(first))
    keys.discard("")
    return keys


def _is_site_tail(tail: str, sep: str, keys: set[str]) -> bool:
    key = _site_key(tail)
    if not key:
        return False
    if key in keys or (sep == "|" and key == "medium"):
        return True
    # "Python - Wikipedia", "... - Wikipedia, the free encyclopedia"
    return any(key.startswith(w) for w in _WELL_KNOWN_SITES) and len(tail.split()) <= 6


def strip_display_affixes(title: str, site: str = "", url: str = "") -> str:
    """``title`` without a trailing site name (``Story | The Guardian``, ``Bun 1.2 -
    bun.sh``, ``Python - Wikipedia``) and, only when what remains is a real headline of
    4+ words, a leading ``Site: `` whose name is exactly ``site``. Leading words are
    otherwise kept: ``Python: the language`` on python.org is the headline itself."""
    t = title.strip()
    keys = _site_keys(site, url)
    for _ in range(2):  # "Story | Section | Site" keeps its section
        m = _DISPLAY_SUFFIX_RE.match(t)
        if not (m and len(m.group("head")) >= _DISPLAY_MIN_HEAD
                and _is_site_tail(m.group("tail"), m.group("sep"), keys)):
            break
        t = m.group("head")
    m = _DISPLAY_PREFIX_RE.match(t)
    if (m and site.strip() and " ".join(m.group("head").split()).casefold() == " ".join(site.split()).casefold()
            and len(m.group("tail").split()) >= 4):
        t = m.group("tail")
    elif _host(url) == "github.com" and re.match(r"(?i)github\s+[-–—]\s+\S", t):
        t = re.sub(r"(?i)^github\s+[-–—]\s+", "", t)  # "GitHub - owner/repo: about"
    return t.strip()


def display_title(raw: str, safe: str, *, site: str = "", url: str = "") -> str:
    """The headline shown inside the book (chapter ``<h1>``, nav label, ``dc:title``,
    the .txt header): the cleaned original title, not the file-name-safe ``safe`` one,
    which loses ``?``, ``:``, quotes and everything past 90 bytes.

    A trailing site name is dropped (see :func:`strip_display_affixes`). Capped at
    :data:`DISPLAY_TITLE_MAX_BYTES`. A `` (n)`` counter ``safe`` got for uniqueness is
    kept, so two books with the same headline stay distinguishable in the Library
    (which shows the metadata title). Item titles, file names and the catalog and OPDS
    titles keep using ``safe``.
    """
    t = " ".join(clean_text(raw or "").split())
    if t:
        try:
            t = strip_display_affixes(t, site, url) or t
        except Exception:  # noqa: BLE001 - cosmetic only
            log.exception("strip_display_affixes failed for %r", t)
    if not t:
        return safe
    suffix = ""
    m = _COUNTER_SUFFIX_RE.search(safe)
    if m:
        try:
            base = unique_title(raw, set())
        except Exception:  # noqa: BLE001
            base = _basic_title(raw)
        if base != safe:
            suffix = m.group(0)
            if _COUNTER_SUFFIX_RE.search(base):  # "Foo (2)" shared again became "Foo (3)"
                t = _COUNTER_SUFFIX_RE.sub("", t)
    head = _cap_utf8(t, DISPLAY_TITLE_MAX_BYTES - len(suffix.encode("utf-8")))
    return (head + suffix) if head else safe


def _storage_message(exc: BaseException) -> str:
    detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else _describe(exc)
    return f"The server could not save the share ({detail}); nothing was stored"


def _norm_title(s: str) -> str:
    s = clean_text(s).casefold()
    s = re.sub(r"[\s\-‐-―|:·•]+", " ", s)
    return s.strip(" .,;!?\"'‘’“”()[]")


#: Where a title and a site affix meet: "Title | Site", "Site: Title", ...
_TITLE_AFFIX_SPLIT_RE = re.compile(r"\s+[|\-–—·]\s+|:\s+")
_TITLE_AFFIX_MAX_WORDS = 5


def _affix_of(text: str, title_norm: str) -> bool:
    """Whether ``text`` is the (normalised) title plus a short affix split off at a
    separator: Safari's ``"Bun 1.2 | Bun Blog"`` for a page titled ``"Bun 1.2"``."""
    for m in _TITLE_AFFIX_SPLIT_RE.finditer(text):
        head, tail = text[: m.start()], text[m.end():]
        for same, affix in ((head, tail), (tail, head)):
            if _norm_title(same) == title_norm and 0 < len(_norm_title(affix).split()) <= _TITLE_AFFIX_MAX_WORDS:
                return True
    return False


def matches_title(remainder: str, title: str, *, url: str = "", site: str = "") -> bool:
    """Short shared text that is (a part of) the page title, e.g. Android's
    ``"Title - Site https://..."`` share or Safari's title in the iOS share box: the
    share is just the link.

    After normalising both (case, separators, outer punctuation) the text matches when
    it is the title or a part of it; when one is the other plus a site affix of at most
    :data:`_TITLE_AFFIX_MAX_WORDS` words split off at `` | ``, `` - ``, `` – ``,
    `` — ``, `` · `` or ``: ``; when it is the title plus only the site's name or host
    (``url``, ``site``), however short the title; or when it holds a title of 12+
    characters with at most 60 more.
    """
    r, t = _norm_title(remainder), _norm_title(title)
    if not r or not t:
        return False
    if r == t or r in t:
        return True
    if _affix_of(remainder, t) or _affix_of(title, r):
        return True
    if t not in r:
        return False
    keys = _site_keys(site, url)
    if keys:
        for m in re.finditer(rf"(?<!\w){re.escape(t)}(?!\w)", r):
            rest = r[: m.start()] + " " + r[m.end():]
            if _site_key(rest) in keys:
                return True
    return len(t) >= 12 and len(r) <= len(t) + 60


def _host(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _sender_name(sender: str) -> str:
    """Display name for a byline: "Ann <a@x.org>" -> "Ann"; bare names stay whole."""
    sender = sender or ""
    if "@" not in sender and "<" not in sender:
        return clean_text(sender)  # e.g. an HA device name: parseaddr would keep one word
    name, addr = email.utils.parseaddr(sender)
    return clean_text(name or addr or sender)


def _filename_stem(filename: str) -> str:
    base = posixpath.basename((filename or "").replace("\\", "/"))
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return clean_text(stem.replace("_", " "))


def _describe(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)  # extract.fetch.FetchError carries a user-facing reason
    if isinstance(reason, str) and reason:
        return reason
    if isinstance(exc, IngestError):
        return str(exc)
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def decode_text_bytes(data: bytes, charset_hint: str | None = None) -> str:
    """Bytes of a .txt file -> str: BOM, declared charset, UTF-8, detection, cp1252."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(enc, "replace")
    if charset_hint:
        try:
            return data.decode(charset_hint)
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception:  # pragma: no cover - detection is best effort
        pass
    return data.decode("cp1252", "replace")


def sniff_file_type(filename: str, content_type: str, data: bytes) -> str:
    """``pdf`` | ``epub`` | ``html`` | ``txt`` | ``unknown`` from magic bytes, name, type."""
    name = (filename or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    ct = (content_type or "").split(";")[0].strip().lower()
    head = data[:1024]
    if b"%PDF-" in head:
        return "pdf"
    if data[:4] == b"PK\x03\x04" and b"application/epub+zip" in data[:200]:
        return "epub"
    if ext == "pdf" or ct == "application/pdf":
        return "pdf"
    if ext == "epub" or ct == "application/epub+zip":
        return "epub"
    if ext in ("html", "htm", "xhtml") or ct in ("text/html", "application/xhtml+xml"):
        return "html"
    sniff = head.lstrip().lower()
    if sniff.startswith((b"<!doctype html", b"<html")):
        return "html"
    if ext in ("txt", "text", "md", "markdown") or ct.startswith("text/"):
        return "txt"
    if b"\x00" not in head:
        try:
            data[:65536].decode("utf-8")
            return "txt"
        except UnicodeDecodeError:
            pass
    return "unknown"


def epub_to_document(data: bytes, *, deadline: float = 0.0) -> Document:
    """Text of an uploaded EPUB (title/creator/language + block text of the spine).

    Used for the item's .txt rendition and preview; the EPUB itself is kept unchanged.
    Raises IngestError for anything that is not a readable EPUB.

    Bounded however the archive is built (it runs in a worker thread that a timed-out
    share cannot cancel): each spine document is read once however often the spine
    lists it; at most :data:`_MAX_EPUB_CHAPTER_BYTES` of one document and
    :data:`_MAX_EPUB_READ_BYTES` in all are decompressed, documents over
    :data:`_EPUB_FAST_CHAPTER_BYTES` go through the cheap block walker, text stops after
    :data:`_MAX_EPUB_TEXT_CHARS` characters (each cut with a note), and past
    ``deadline`` (a ``time.monotonic()`` value; 0 = none) it gives up.
    """
    from lxml import etree

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise IngestError("The file is not a valid EPUB (not a ZIP archive)") from exc
    with zf:
        if sum(i.file_size for i in zf.infolist()) > _MAX_EPUB_UNCOMPRESSED:
            raise IngestError("The EPUB is too large to read")

        def read(name: str, limit: int) -> tuple[bytes, bool]:
            """``(at most limit bytes of the member, whether it is longer)``."""
            with zf.open(name) as f:  # KeyError if missing
                raw = f.read(limit + 1)
            return raw[:limit], len(raw) > limit

        def package(name: str):
            raw, cut = read(name, _MAX_EPUB_PACKAGE_BYTES)
            if cut:
                raise IngestError("The EPUB is too large to read")
            return etree.fromstring(raw, parser)

        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
        try:
            container = package("META-INF/container.xml")
            rootfile = container.xpath("//*[local-name()='rootfile']/@full-path")[0]
            opf = package(rootfile)
        except (KeyError, IndexError, etree.XMLSyntaxError, ValueError, *_ZIP_READ_ERRORS) as exc:
            raise IngestError("The EPUB has no readable package document") from exc

        def meta(name: str) -> str:
            vals = opf.xpath(f"//*[local-name()='metadata']/*[local-name()='{name}']/text()")
            return clean_text(vals[0]) if vals else ""

        base = posixpath.dirname(rootfile)
        manifest = {
            el.get("id"): el.get("href")
            for el in opf.xpath("//*[local-name()='manifest']/*[local-name()='item']")
            if el.get("id") and el.get("href")
        }
        blocks: list = []
        notes: list[str] = []
        chars = 0
        left = _MAX_EPUB_READ_BYTES
        seen: set[str] = set()
        for ref in opf.xpath("//*[local-name()='spine']/*[local-name()='itemref']/@idref")[:_MAX_EPUB_SPINE]:
            if deadline and time.monotonic() > deadline:
                raise IngestError("Timed out while reading the EPUB")
            href = manifest.get(ref)
            if not href:
                continue
            path = posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0])))
            if path in seen:
                continue
            seen.add(path)
            if left <= 0:
                notes.append(_EPUB_NOTE_STOPS_EARLY)
                break
            limit = min(_MAX_EPUB_CHAPTER_BYTES, left)
            try:
                raw, cut = read(path, limit)
            except KeyError:
                continue
            except _ZIP_READ_ERRORS as exc:  # a damaged member: the text copy goes without it
                log.warning("EPUB upload: could not read %s (%s)", path, exc)
                continue
            left -= len(raw)
            stop = False
            if cut:
                raw = _utf8_prefix(raw)
                if limit < _MAX_EPUB_CHAPTER_BYTES:
                    stop = True  # the total is used up: nothing after this is read
                elif _EPUB_NOTE_CHAPTER_CUT not in notes:
                    notes.append(_EPUB_NOTE_CHAPTER_CUT)
            for block in _spine_blocks(raw):
                chars += len(getattr(block, "text", ""))
                if chars > _MAX_EPUB_TEXT_CHARS:
                    stop = True
                    break
                blocks.append(block)
            if stop:
                notes.append(_EPUB_NOTE_STOPS_EARLY)
                break
    return Document(title=meta("title"), blocks=blocks, byline=meta("creator"), lang=meta("language") or "en",
                    notes=notes)


def _utf8_prefix(raw: bytes) -> bytes:
    """``raw`` cut at an arbitrary byte, without a UTF-8 sequence the cut split (it
    would make the charset detection give up on UTF-8)."""
    end = len(raw)
    i = end - 1
    while i >= 0 and end - i <= 4 and raw[i] & 0xC0 == 0x80:
        i -= 1  # continuation bytes
    if i < 0 or raw[i] < 0xC0:
        return raw  # ASCII or no lead byte: nothing split
    need = 2 if raw[i] < 0xE0 else 3 if raw[i] < 0xF0 else 4
    return raw if end - i >= need else raw[:i]


_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]{1,31});")
_XML_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})


def _numeric_entities(markup: str) -> str:
    """HTML named character references (``&nbsp;``, ``&mdash;``) as numeric ones, which
    XML knows without a DTD. An XHTML chapter is parsed as XML, and with a DOCTYPE
    (XHTML 1.1's, say) the XML parser silently drops the entities it cannot resolve:
    ``Hello&nbsp;world`` became ``Helloworld``."""
    if "&" not in markup:
        return markup

    def numeric(m: re.Match) -> str:
        name = m.group(1)
        value = None if name in _XML_ENTITIES else _HTML5_ENTITIES.get(name + ";")
        return "".join(f"&#x{ord(c):x};" for c in value) if value else m.group(0)

    return _ENTITY_RE.sub(numeric, markup)


def _spine_blocks(raw: bytes) -> list:
    """Blocks of one spine document, including text outside ``<p>`` (directly in
    ``<body>``, ``<div>`` or ``<section>``, or between ``<br/>``s).

    Documents up to :data:`_EPUB_FAST_CHAPTER_BYTES` go through the full HTML
    converter (parsed as XML, so XHTML's ``<a id="x"/>`` and ``<title/>`` mean what they
    say); larger ones through the cheap linear walker."""
    markup = _XML_DECL_RE.sub("", decode_html(raw, None).lstrip("\ufeff"), count=1)
    if len(raw) <= _EPUB_FAST_CHAPTER_BYTES:
        try:
            return html_fragment_to_blocks(_numeric_entities(markup), xml=True)
        except Exception:  # noqa: BLE001 - keep the upload; fall back to the simple walker
            log.exception("html_fragment_to_blocks failed on an EPUB chapter")
    import lxml.html
    from lxml import etree

    try:
        root = lxml.html.document_fromstring(markup, parser=lxml.html.HTMLParser(huge_tree=True,
                                                                                 remove_comments=True))
    except (etree.ParserError, ValueError):
        return []
    return _html_leaf_blocks(root)


_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "div", "td", "dd", "dt",
                         "th", "caption", "figcaption", "address"})
#: Elements that break a run of loose text (text not inside a leaf block).
_BREAK_TAGS = _BLOCK_TAGS | {"br", "hr", "body", "section", "article", "main", "header", "footer", "aside", "nav",
                             "figure", "ul", "ol", "dl", "table", "tr", "tbody", "thead", "tfoot", "center"}
_SKIP_TAGS = frozenset({"head", "script", "style", "title", "noscript", "template", "svg", "math"})


def _html_leaf_blocks(root) -> list:
    """Cheap linear walk of a parsed document: each block element without block
    descendants becomes one block (its whole text), and text outside such blocks
    (directly in ``<body>``, ``<div>`` or ``<section>``, between ``<br/>``s) becomes
    paragraphs too."""
    from lxml import etree

    # Elements containing a block: one walk up from each block, stopping at an
    # ancestor already marked (so every element is marked at most once).
    has_block: set = set()
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag in _BLOCK_TAGS:
            parent = el.getparent()
            while parent is not None and parent not in has_block:
                has_block.add(parent)
                parent = parent.getparent()

    out: list = []
    loose: list[str] = []

    def add(tag: str, raw: str) -> None:
        text = clean_text(raw)
        if not text:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            out.append(Heading(text, min(3, max(2, int(tag[1])))))
        else:
            out.append(Paragraph(text))

    def flush() -> None:
        if loose:
            add("p", "".join(loose))
            loose.clear()

    walker = etree.iterwalk(root, events=("start", "end"))
    for event, el in walker:
        tag = el.tag if isinstance(el.tag, str) else ""
        if event == "start":
            if not tag:
                continue  # comment or processing instruction: only its tail is text
            if tag in _SKIP_TAGS:
                walker.skip_subtree()
            elif tag in _BLOCK_TAGS and el not in has_block:
                flush()
                add(tag, el.text_content())
                walker.skip_subtree()
            else:
                if tag in _BREAK_TAGS:
                    flush()
                if el.text:
                    loose.append(el.text)
        else:
            if tag in _BREAK_TAGS:
                flush()
            if el.tail and el is not root:
                loose.append(el.tail)
    flush()
    return out


def _basic_title(title: str) -> str:
    """Last-resort firmware-safe title if render.names is unavailable or failing."""
    t = clean_text(title)
    t = re.sub(r"\.{2,}|…", "…", t)
    t = re.sub(r'[{}*?"<>]', "", t)
    t = re.sub(r"[/\\|:]", "-", t)
    t = re.sub(r"~(\d)", r"- \1", t)
    t = t.lstrip(" .").rstrip(" .")
    raw = t.encode("utf-8")[:90]
    t = raw.decode("utf-8", "ignore").rstrip(" .")
    return t or time.strftime("Note %Y-%m-%d %H%M")


# --------------------------------------------------------------------------------------
# Processor
# --------------------------------------------------------------------------------------


@dataclass
class _Context:
    """What is known about a request so far; used to build the fallback on failure."""

    url: str = ""
    dedupe_key: str = ""
    kind: str = ""
    comment: str = ""  # the sharer's remark next to a link (kept in a failed item too)
    deadline: float = 0.0  # time.monotonic() when the budget runs out
    phase: str = ""  # what is running now, for the budget's timeout message ("converting the PDF")


class Processor:
    """Classifies, extracts, renders and stores shares. One instance per process."""

    def __init__(self, store: Store, opts: Options):
        self.store = store
        self.opts = opts
        self.background_budget = BACKGROUND_BUDGET_SECONDS
        self._key_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._reserved_titles: set[str] = set()
        # Spool entries written from this second on belong to this run (in flight).
        self._started = int(time.time())

    # ------------------------------------------------------------------ public API
    async def process(self, req: IngestRequest) -> Item:
        """Turn one request into stored Item(s) and return the first. Raises only
        :class:`StorageError`."""
        return (await self.process_detailed(req)).item

    async def process_many(self, reqs: list[IngestRequest]) -> list[Item]:
        """Process requests one after another (keeps the Pi's load predictable). Raises
        only :class:`StorageError` (the requests before it are stored)."""
        return [await self.process(r) for r in reqs]

    async def process_detailed(self, req: IngestRequest) -> ProcessResult:
        """Like :meth:`process` but also reports whether a duplicate was returned."""
        return (await self.process_all(req))[0]

    async def process_all(self, req: IngestRequest) -> list[ProcessResult]:
        """One result per stored (or duplicate) item, never empty: a share of several
        bare links gives one url item per link, anything else one item.

        The request is spooled first and removed from the spool once its items are
        stored; if processing is interrupted (restart, crash) or the store fails, it
        stays there for :meth:`recover_pending`. Raises only :class:`StorageError`.
        """
        spool_id = await self._spool(req)
        results = await self._process_request(req)
        self._unspool(spool_id)
        return results

    async def recover_pending(self) -> int:
        """Process the requests a crash or restart left in the spool (call once at
        startup; it may run while new shares arrive). Returns how many were handled.

        A request already tried :data:`SPOOL_MAX_ATTEMPTS` times is stored as a failed
        item instead (a share that crashes the app must not do so at every start).
        Stops at the first :class:`StorageError`: the rest stay spooled for next time.
        """
        try:
            entries: list[SpooledRequest] = await asyncio.to_thread(self.store.spooled_requests)
        except Exception:  # noqa: BLE001
            log.exception("could not read the spool")
            return 0
        done = 0
        for entry in entries:
            if entry.created_at >= self._started:
                continue  # spooled by this run: still being processed
            req = entry.request
            try:
                if entry.data_lost:
                    self._store_failed(req, _Context(kind=_guess_kind(req)),
                                       "The file was lost when Send to X3 restarted; please send it again")
                elif entry.attempts >= SPOOL_MAX_ATTEMPTS:
                    log.error("spooled share %s failed %d times; storing it as failed", entry.id, entry.attempts)
                    self._store_failed(req, _Context(kind=_guess_kind(req)),
                                       f"Send to X3 stopped {entry.attempts} times while processing this share, "
                                       "so it was not processed")
                else:
                    log.info("recovering a share received before the restart (%s, attempt %d)",
                             entry.id, entry.attempts + 1)
                    self.store.spool_attempt(entry.id)
                    await self._process_request(req)
            except StorageError as exc:
                log.error("recovering spooled shares stopped: %s", exc)
                break
            except Exception:  # noqa: BLE001 - e.g. the database is gone; try again next start
                log.exception("recovering spooled share %s failed", entry.id)
                break
            self._unspool(entry.id)
            done += 1
        return done

    def budget_for(self, req: IngestRequest) -> float:
        """Every share gets the background budget: a link may be a large or scanned PDF,
        and nobody waits on the result (the HTTP API answers "still processing" after
        ``RESPONSE_WAIT_SECONDS``). Page fetches are bounded by their own timeout."""
        return self.background_budget + (OCR_EXTRA_BUDGET_SECONDS if self.opts.pdf_ocr else 0.0)

    # ------------------------------------------------------------------- driving
    async def _process_request(self, req: IngestRequest) -> list[ProcessResult]:
        subs, comment = self._split_links(req)
        return [await self._process_one(r, comment=comment) for r in subs]

    async def _process_one(self, req: IngestRequest, *, comment: str = "") -> ProcessResult:
        budget = self.budget_for(req)
        started = time.monotonic()
        ctx = _Context(comment=comment, deadline=started + budget)
        try:
            return await asyncio.wait_for(self._run(req, ctx), timeout=budget)
        except asyncio.CancelledError:
            raise
        except StorageError:
            raise
        except TimeoutError as exc:
            if time.monotonic() - started < budget * 0.95:  # an inner timeout, not the budget
                return self._store_failed(req, ctx, _describe(exc) if str(exc) else "Timed out")
            what = ctx.phase or "processing the share"
            return self._store_failed(req, ctx, f"Timed out after {budget:g} s while {what}")
        except Exception as exc:  # noqa: BLE001 - only StorageError may escape
            if not isinstance(exc, IngestError):
                log.exception("processing failed (source=%s, kind=%s)", req.source, req.kind)
            return self._store_failed(req, ctx, _describe(exc))

    def _split_links(self, req: IngestRequest) -> tuple[list[IngestRequest], str]:
        """A share of several bare links (2..:data:`MAX_LINKS_PER_SHARE`, at most
        :data:`COMMENT_MAX_WORDS` other words) -> one url request per link, plus the
        sharer's remark to keep on each. Anything else -> ``([req], "")``."""
        try:
            if req.data is not None or (req.kind or "auto").strip().lower() != "auto":
                return [req], ""
            text = req.text or ""
            if not text.strip():
                return [req], ""
            links = _links(text)
            link = _web_url(req.url)
            if link and not _has_link(links, link):
                return [req], ""  # text selected on the page at req.url
            remainder = _remainder(text)
            if not 2 <= len(links) <= MAX_LINKS_PER_SHARE or not _short_or_empty(remainder):
                return [req], ""
            comment = _comment_of(remainder, links[0], "")
            # One share title cannot name several pages: each page brings its own.
            return [dataclasses.replace(req, kind="url", url=u, text="", html="", title="") for u in links], comment
        except Exception:  # noqa: BLE001 - classify as a single share instead
            log.exception("splitting a share into links failed")
            return [req], ""

    async def _spool(self, req: IngestRequest) -> str:
        try:
            if req.data is not None and len(req.data) > _SPOOL_INLINE_MAX:
                return await asyncio.to_thread(self.store.spool_add, req)
            return self.store.spool_add(req)
        except (OSError, sqlite3.Error) as exc:
            log.error("could not spool a share: %s", exc)
            raise StorageError(_storage_message(exc)) from exc
        except Exception:  # noqa: BLE001 - a spool bug must not lose the share: process it anyway
            log.exception("could not spool a share")
            return ""

    def _unspool(self, spool_id: str) -> None:
        if not spool_id:
            return
        try:
            self.store.spool_remove(spool_id)
        except Exception:  # noqa: BLE001 - stored already; a leftover entry is a duplicate later
            log.exception("could not remove spool entry %s", spool_id)

    # ------------------------------------------------------------- classification
    async def _run(self, req: IngestRequest, ctx: _Context) -> ProcessResult:
        if req.data is not None:
            return await self._run_file(req, ctx)
        text = req.text or ""
        url = (req.url or "").strip()
        kind = (req.kind or "auto").strip().lower()

        if kind == "html" or (kind == "auto" and req.html.strip() and not text.strip()):
            return await self._run_html(req, ctx, url)
        if kind == "pdf":  # a PDF without bytes can only be a link to one
            kind = "url"
        if kind == "url":
            url = url or _first_url(text)
            if not url:
                raise IngestError("No link was found in the shared text")
            return await self._run_url(req, ctx, url, comment=ctx.comment)
        if kind == "text":
            if not text.strip():
                raise IngestError("The shared text is empty")
            # An email body's links are often sponsors or trackers: its source is the
            # link the email parser chose (if any), never just the first one in the body.
            source_url = _web_url(url) if req.source == "email" else (_web_url(url) or _first_url(text))
            return await self._run_text(req, ctx, text, source_url)
        if kind != "auto":
            raise IngestError(f"Unknown kind {kind!r} (use auto, url, text, html or pdf)")
        return await self._run_auto(req, ctx, text, url)

    async def _run_auto(self, req: IngestRequest, ctx: _Context, text: str, url: str) -> ProcessResult:
        """§7.1 classification (several bare links were split off by :meth:`_split_links`):

        - an explicit ``url`` whose page the text does not mention: the text was selected
          on that page -> a text item citing it, unless the text is (part of) the page
          title or filler -> url item;
        - one link plus at most :data:`COMMENT_MAX_WORDS` other words -> url item (the
          fetched article), with the words kept as a ``Comment:`` note unless they are
          filler or the page title;
        - anything else -> text item, its first web link kept as ``source_url``.
        """
        link = _web_url(url)
        if not text.strip():
            if link:
                return await self._run_url(req, ctx, link)
            if url:  # mailto:, tel:, ...: nothing to fetch; keep what was shared
                return await self._run_text(req, ctx, url, "")
            raise IngestError("Nothing to read: the share had no text, link or file")
        links = _links(text)
        if link and not _has_link(links, link):
            if _is_link_share(text, link, req.title):
                return await self._run_url(req, ctx, link)
            if not req.title.strip() and _is_short(_remainder(text)):
                # Only the page can tell whether a short selection is its title: fetch
                # it once and reuse the document if it is.
                result = await self._run_url(req, ctx, link, selection=text)
                if result is not None:
                    return result
            return await self._run_text(req, ctx, text, link, sender_is_author=False)
        remainder = _remainder(text)
        if len(links) == 1 and _short_or_empty(remainder):
            target = link or links[0]
            return await self._run_url(req, ctx, target, comment=_comment_of(remainder, target, req.title))
        source_url = link if req.source == "email" else (link or (links[0] if links else ""))
        return await self._run_text(req, ctx, text, source_url)

    # ------------------------------------------------------------------- flows
    async def _run_url(self, req: IngestRequest, ctx: _Context, url: str, *, comment: str = "",
                       selection: Optional[str] = None) -> Optional[ProcessResult]:
        """A link share: fetch, extract, store a ``url`` item.

        ``comment``: the sharer's words next to the link, kept as a ``Comment: ...`` note.
        The dedupe key then includes them (a new remark on a link already in the queue is
        a new item), unless the page title shows they were just its title.
        ``selection``: text selected on the page. The share is a link share only if the
        text is (part of) the page title; otherwise None is returned and the caller keeps
        the text.
        """
        ctx.url, ctx.kind, ctx.comment = url, "url", comment
        plain = url_dedupe_key(url)
        key = f"{plain}#{text_dedupe_key(comment)}" if comment else plain
        ctx.dedupe_key = key
        probe = selection if selection is not None else comment
        if probe:
            cached = self._recent(plain)
            if cached is not None and cached.status == "ready":
                if _is_link_share(probe, url, cached.title, cached.site):
                    return ProcessResult(cached, True)
                if selection is not None:  # a note on a link already in the queue
                    self._not_a_link(ctx)
                    return None
        async with self._key_lock(key):
            existing = self._recent(key)
            if existing is not None and existing.status == "ready":
                return ProcessResult(existing, True)
            doc = await self._fetch_document(url, req, ctx)
            if probe and _is_link_share(probe, url, doc.title, doc.site):
                if comment:  # the "comment" was the page title after all: a plain link share
                    comment = ctx.comment = ""
                    key = ctx.dedupe_key = plain
                    existing = self._recent(plain)
                    if existing is not None and existing.status == "ready":
                        return ProcessResult(existing, True)
            elif selection is not None:
                self._not_a_link(ctx)
                return None
            if not doc.source_url:
                doc = dataclasses.replace(doc, source_url=url)
            return await self._commit(req, doc, "url", key, superseded=existing, comment=comment)

    async def _fetch_document(self, url: str, req: IngestRequest, ctx: _Context) -> Document:
        """:func:`url_to_document`, keeping ``ctx.phase`` current for the budget's timeout
        message: "fetching the page", or "converting the PDF" while a linked PDF converts
        (substitutes without ``on_phase`` just say "fetching the page")."""

        def set_phase(what: str) -> None:
            ctx.phase = what

        ctx.phase = "fetching the page"
        title_hint = clean_text(req.title)
        if _takes_kw(url_to_document, "on_phase"):
            doc = await url_to_document(url, opts=self.opts, title_hint=title_hint, on_phase=set_phase)
        else:
            doc = await url_to_document(url, opts=self.opts, title_hint=title_hint)
        ctx.phase = ""
        return doc

    @staticmethod
    def _not_a_link(ctx: _Context) -> None:
        ctx.kind, ctx.dedupe_key, ctx.comment = "", "", ""

    async def _run_text(self, req: IngestRequest, ctx: _Context, text: str, source_url: str, *,
                        sender_is_author: bool = True) -> ProcessResult:
        kind = "email" if req.source == "email" else "text"
        ctx.kind, ctx.url = kind, source_url
        key = text_dedupe_key(text)
        ctx.dedupe_key = key
        async with self._key_lock(key):
            existing = self._recent(key)
            if existing is not None and existing.status == "ready":
                return ProcessResult(existing, True)
            doc = await asyncio.to_thread(
                text_to_document, text, title=clean_text(req.title), source_url=source_url
            )
            return await self._commit(req, doc, kind, key, superseded=existing, sender_is_author=sender_is_author)

    async def _run_html(self, req: IngestRequest, ctx: _Context, url: str) -> ProcessResult:
        from_email = req.source == "email"
        kind = "email" if from_email else ("url" if url else "text")
        ctx.kind, ctx.url = kind, url
        key = url_dedupe_key(url) if url else "html:" + hashlib.sha256(req.html.encode("utf-8")).hexdigest()
        ctx.dedupe_key = key
        async with self._key_lock(key):
            existing = self._recent(key)
            if existing is not None and existing.status == "ready":
                return ProcessResult(existing, True)
            # An email body is the content itself: converted whole, not article-extracted.
            doc = await asyncio.to_thread(_html_document, req.html, url, from_email)
            if from_email:
                # Its subject is the title (a newsletter's <h1> is often less specific),
                # and short bodies are normal, not a sign of a JavaScript page or a paywall.
                notes = [n for n in doc.notes if "JavaScript" not in n and "words could be" not in n]
                doc = dataclasses.replace(doc, notes=notes, title=clean_text(req.title) or doc.title)
            elif req.title.strip() and not doc.title:
                doc = dataclasses.replace(doc, title=clean_text(req.title))
            if url and not doc.source_url:
                doc = dataclasses.replace(doc, source_url=url)
            return await self._commit(req, doc, kind, key, superseded=existing, sender_is_author=from_email)

    async def _run_file(self, req: IngestRequest, ctx: _Context) -> ProcessResult:
        data = req.data or b""
        if not data:
            raise IngestError("The uploaded file is empty")
        ftype = sniff_file_type(req.filename, req.content_type, data)
        url = (req.url or "").strip()
        title_hint = clean_text(req.title) or _filename_stem(req.filename)
        from_email = req.source == "email"
        ctx.url = url
        ctx.kind = "pdf" if ftype == "pdf" else ("email" if from_email else ("url" if url and ftype == "html" else "text"))
        key = bytes_dedupe_key(data)
        ctx.dedupe_key = key
        if ftype == "unknown":
            raise IngestError(f"Unsupported file type ({req.filename or req.content_type or 'unknown'}): "
                              "send a PDF, TXT, HTML or EPUB file")
        if ftype == "pdf" and not self.opts.pdf_enabled:
            raise IngestError("PDF import is turned off in the app options (pdf_enabled)")
        async with self._key_lock(key):
            existing = self._recent(key)
            if existing is not None and existing.status == "ready":
                return ProcessResult(existing, True)
            if ftype == "epub":
                ctx.phase = "reading the EPUB"
                return await self._commit_epub(req, data, key, title_hint, superseded=existing,
                                               deadline=ctx.deadline)
            sender_is_author = False  # documents (PDF, HTML) have their own authors
            if ftype == "pdf":
                ctx.phase = "converting the PDF"
                doc = await self._convert_pdf(data, title_hint=title_hint, source_url=url, deadline=ctx.deadline)
                ctx.phase = ""
                kind = "pdf"
            elif ftype == "html":
                charset = _charset_param(req.content_type)
                html = decode_html(data, charset)
                doc = await asyncio.to_thread(_html_document, html, url, from_email)
                kind = "email" if from_email else ("url" if url else "text")
            else:
                text = decode_text_bytes(data, _charset_param(req.content_type))
                if not text.strip():
                    raise IngestError("The text file is empty")
                doc = await asyncio.to_thread(text_to_document, text, title=title_hint, source_url=url)
                kind = "email" if from_email else "text"
                sender_is_author = True
            if title_hint and not doc.title:
                doc = dataclasses.replace(doc, title=title_hint)
            return await self._commit(req, doc, kind, key, superseded=existing, sender_is_author=sender_is_author)

    async def _convert_pdf(self, data: bytes, *, title_hint: str, source_url: str, deadline: float) -> Document:
        """Convert in the converter's own process, cancellably: when the share's budget
        runs out, the ``wait_for`` around it cancels this, which kills the worker and
        frees its slot. The converter's own timeout is the remaining budget minus
        :data:`PDF_BUDGET_MARGIN_SECONDS`, so it normally stops first and its reason
        (and any OCR pages done) is kept."""
        ocr = self.opts.pdf_ocr
        if pdf_to_document is not _DEFAULT_PDF_SEAMS[0] and pdf_to_document_async is _DEFAULT_PDF_SEAMS[1]:
            # only the blocking seam was substituted (tests): run it as before
            return await asyncio.to_thread(pdf_to_document, data, title_hint=title_hint, source_url=source_url,
                                           ocr=ocr)
        timeout = max(1.0, deadline - time.monotonic() - PDF_BUDGET_MARGIN_SECONDS) if deadline else None
        return await pdf_to_document_async(data, title_hint=title_hint, source_url=source_url, ocr=ocr,
                                           timeout=timeout)

    # ---------------------------------------------------------------- committing
    async def _commit(self, req: IngestRequest, doc: Document, kind: str, key: str,
                      *, superseded: Optional[Item] = None, comment: str = "",
                      sender_is_author: bool = False) -> ProcessResult:
        doc = self._finish(req, doc, sender_is_author)
        status, error = "ready", ""
        words = doc.word_count()
        if words == 0:
            status = "failed"
            reasons = [clean_text(n) for n in doc.notes if clean_text(n)] or ["No readable text was found."]
            error = reasons[0]
            # Make the reason visible in the delivered file itself.
            doc = dataclasses.replace(doc, blocks=[Paragraph(r) for r in reasons] + list(doc.blocks), notes=[])
        if comment:
            doc = dataclasses.replace(doc, notes=list(doc.notes) + [f"Comment: {comment}"])
        raw_title = doc.title or clean_text(req.title) or _host(doc.source_url) or ""
        title = self._reserve_title(raw_title)
        try:
            doc = dataclasses.replace(doc, title=display_title(raw_title, title, site=doc.site, url=doc.source_url))
            item_id = self._new_id()
            epub, txt = await asyncio.to_thread(self._render_pair, doc, item_id)
            item = Item(
                id=item_id, created_at=int(time.time()), kind=kind, source=req.source or "http", title=title,
                byline=doc.byline, site=doc.site, source_url=doc.source_url, status=status, error=error,
                word_count=words, dedupe_key=key,
            )
            # Synchronous from here on: a timeout can no longer interrupt half-way.
            self._save(item, doc, epub, txt)
        finally:
            self._reserved_titles.discard(title)
        if superseded is not None and status == "ready" and not superseded.delivered:
            self._drop(superseded)  # an undelivered failed attempt at the same thing
        log.info("stored %s item %s %r (%s, %d words)", kind, item.id, title, status, item.word_count)
        return ProcessResult(item, False)

    async def _commit_epub(self, req: IngestRequest, data: bytes, key: str, title_hint: str,
                           *, superseded: Optional[Item], deadline: float = 0.0) -> ProcessResult:
        """Uploaded EPUBs are kept byte for byte (the X3 reads EPUB natively); only the
        text is extracted for the .txt rendition and preview. No doc.json is written, so
        auto-delivery sends such an item on its own."""
        doc = await asyncio.to_thread(epub_to_document, data, deadline=deadline)
        raw_title = doc.title or title_hint
        title = self._reserve_title(raw_title)
        try:
            source_url = doc.source_url or (req.url or "").strip()
            doc = dataclasses.replace(doc, title=display_title(raw_title, title, site=doc.site, url=source_url),
                                      source_url=source_url)
            item_id = self._new_id()
            txt = await asyncio.to_thread(render_txt, doc)
            item = Item(
                id=item_id, created_at=int(time.time()), kind="email" if req.source == "email" else "text",
                source=req.source or "http", title=title, byline=doc.byline, site=doc.site,
                source_url=doc.source_url, status="ready", word_count=doc.word_count(), dedupe_key=key,
            )
            self._save(item, None, data, txt)
        finally:
            self._reserved_titles.discard(title)
        if superseded is not None and not superseded.delivered:
            self._drop(superseded)
        return ProcessResult(item, False)

    @staticmethod
    def _finish(req: IngestRequest, doc: Document, sender_is_author: bool) -> Document:
        """The sender (email From, HA device name) is the byline only of what they wrote:
        notes, email bodies, text files. A fetched article or a PDF without an author
        keeps an empty byline (the EPUB creator then falls back to the site)."""
        if doc.byline or not sender_is_author:
            return doc
        byline = _sender_name(req.sender)
        return dataclasses.replace(doc, byline=byline) if byline else doc

    @staticmethod
    def _render_pair(doc: Document, item_id: str) -> tuple[bytes, bytes]:
        epub = render_epub([doc], book_title=doc.title, identifier=item_identifier(item_id))
        txt = render_txt(doc)
        if not epub or not txt:  # the device treats a 0-byte download as a failure
            raise RuntimeError("renderer returned an empty file")
        return epub, txt

    def _save(self, item: Item, doc: Optional[Document], epub: bytes, txt: bytes) -> None:
        try:
            if doc is not None:
                delivery.save_document(self.store, item.id, doc, item_title=item.title)
            self.store.add_item(item, epub=epub, txt=txt)
        except BaseException as exc:
            shutil.rmtree(self.store.item_dir(item.id), ignore_errors=True)
            if isinstance(exc, (OSError, sqlite3.Error)):
                log.error("could not store item %s: %s", item.id, exc)
                raise StorageError(_storage_message(exc)) from exc
            raise

    def _drop(self, item: Item) -> None:
        try:
            self.store.delete(item.id)
        except Exception:  # noqa: BLE001 - the new item is stored; the old one just stays
            log.exception("could not delete superseded item %s", item.id)

    def _new_id(self) -> str:
        for _ in range(8):
            item_id = new_item_id()
            if self.store.get(item_id) is None and not self.store.item_dir(item_id).exists():
                return item_id
        raise RuntimeError("could not allocate an item id")

    def _reserve_title(self, raw: str) -> str:
        """Unique, firmware-safe title; reserved until the item is stored so concurrent
        requests cannot pick the same one. Titles of deleted or pruned items that were
        downloaded as ``<title>.epub`` stay taken (the file is still on the X3)."""
        taken = self.store.titles_in_use() | self.store.issued_titles() | self._reserved_titles
        try:
            title = unique_title(raw, taken)
        except Exception:  # noqa: BLE001
            log.exception("unique_title failed for %r", raw)
            base = _basic_title(raw)
            keys = {t.casefold() for t in taken}
            title, n = base, 2
            while title.casefold() in keys:
                title = f"{base} ({n})"
                n += 1
        self._reserved_titles.add(title)
        return title

    def _recent(self, key: str) -> Optional[Item]:
        return self.store.find_duplicate(key, DEDUPE_WINDOW_SECONDS) if key else None

    @contextlib.asynccontextmanager
    async def _key_lock(self, key: str) -> AsyncIterator[None]:
        """Serialize work on one dedupe key (a double-tapped share becomes one item)."""
        lock, users = self._key_locks.get(key, (asyncio.Lock(), 0))
        self._key_locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._key_locks[key]
            if users <= 1:
                del self._key_locks[key]
            else:
                self._key_locks[key] = (lock, users - 1)

    # ------------------------------------------------------------------ fallback
    def _store_failed(self, req: IngestRequest, ctx: _Context, message: str) -> ProcessResult:
        """Store a failed item with a fallback document. Raises only StorageError (the
        store itself failed, so the share could not be kept)."""
        log.warning("share stored as failed (source=%s): %s", req.source, message)
        try:
            existing = self._recent(ctx.dedupe_key)
            if existing is not None and existing.status == "ready":
                return ProcessResult(existing, True)
            doc = _fallback_document(req, ctx, message)
            title = self._reserve_title(doc.title)
            try:
                doc = dataclasses.replace(doc, title=display_title(doc.title, title, site=doc.site,
                                                                   url=doc.source_url))
                item_id = self._new_id()
                try:
                    epub, txt = self._render_pair(doc, item_id)
                except Exception:  # noqa: BLE001
                    log.exception("rendering the fallback document failed; using the minimal renderer")
                    epub, txt = minimal_epub(doc, item_identifier(item_id)), minimal_txt(doc)
                item = Item(
                    id=item_id, created_at=int(time.time()), kind=ctx.kind or _guess_kind(req),
                    source=req.source or "http", title=title, byline=doc.byline, source_url=doc.source_url,
                    status="failed", error=message, word_count=doc.word_count(), dedupe_key=ctx.dedupe_key,
                )
                self._save(item, doc, epub, txt)
            finally:
                self._reserved_titles.discard(title)
            return ProcessResult(item, False)
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 - database gone, ...
            log.exception("could not store the failed share")
            raise StorageError(_storage_message(exc)) from exc


def _first_url(text: str) -> str:
    try:
        return first_url(text)
    except Exception:  # noqa: BLE001 - keep ingest working if the text module misbehaves
        log.exception("first_url failed")
        return next(iter(_urls_in(text)), "")


def _classify(text: str, url: str, title: str) -> str:
    try:
        return classify(text, url, title)
    except Exception:  # noqa: BLE001
        log.exception("classify failed")
        return "text" if _remainder(text) else "url"


def _charset_param(content_type: str) -> str | None:
    m = re.search(r"charset=\"?([\w.:-]+)", content_type or "", re.I)
    return m.group(1) if m else None


def _guess_kind(req: IngestRequest) -> str:
    if req.data is not None and sniff_file_type(req.filename, req.content_type, req.data) == "pdf":
        return "pdf"
    if req.source == "email":
        return "email"
    return "url" if (req.url or "").strip() else "text"


def _fallback_document(req: IngestRequest, ctx: _Context, message: str) -> Document:
    """Title, link, error note and whatever text was shared: nothing is lost."""
    text = req.text or ""
    url = ctx.url or (req.url or "").strip()
    if not url and req.source != "email":  # an email body's first link is often a tracker
        url = next(iter(_urls_in(text)), "")
    title = clean_text(req.title)
    if not title and text.strip() and _remainder(text):
        try:
            title = clean_text(derive_title(text))
        except Exception:  # noqa: BLE001
            title = clean_text(_remainder(text))[:80]
    if not title:
        title = _filename_stem(req.filename)
    if not title and url:
        p = urlsplit(url)
        title = clean_text(f"{_host(url)}{p.path if p.path not in ('', '/') else ''}")
    blocks: list = [Paragraph(clean_text(f"Send to X3 could not process this share. {message}"))]
    if url and _host(url):  # long URLs overflow e-reader lines; the full one is in the .txt and metadata
        blocks.append(Paragraph(clean_text(f"Link: {_host(url)}")))
    if ctx.comment and ctx.comment not in _remainder(text):
        blocks.append(Paragraph(clean_text(f"Comment: {ctx.comment}")))
    body = text.strip()
    if body and _remainder(body):
        for para in re.split(r"\n\s*\n|\n", body[:_MAX_FALLBACK_TEXT]):
            cleaned = clean_text(para)
            if cleaned:
                blocks.append(Paragraph(cleaned))
    elif req.html.strip():
        try:
            import lxml.html

            for line in lxml.html.document_fromstring(req.html).text_content().splitlines():
                cleaned = clean_text(line)
                if cleaned:
                    blocks.append(Paragraph(cleaned))
        except Exception:  # noqa: BLE001
            pass
    # The sender is the author only of what they wrote (see Processor._finish).
    byline = _sender_name(req.sender) if (ctx.kind or _guess_kind(req)) in ("text", "email") else ""
    return Document(title=title or "Shared item", blocks=blocks, byline=byline, site=_host(url), source_url=url)


# --------------------------------------------------------------------------------------
# Minimal renderers: used only if the real renderers fail on a fallback document.
# --------------------------------------------------------------------------------------


def _xml(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def minimal_txt(doc: Document) -> bytes:
    lines = [doc.title, "———", ""]
    for b in doc.blocks:
        t = getattr(b, "text", "")
        if t:
            lines += [t.replace("\n", " "), ""]
    if doc.source_url:
        lines += ["———", f"Source: {doc.source_url}"]
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def minimal_epub(doc: Document, identifier: str) -> bytes:
    """A tiny valid EPUB 3 with one chapter (same structure rules as render/epub.py)."""
    title = _xml(doc.title or "Shared item")
    lang = _xml(doc.lang or "en")
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = "".join(f"<p>{_xml(getattr(b, 'text', ''))}</p>\n" for b in doc.blocks if getattr(b, "text", ""))
    head = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" '
            f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">\n')
    parts = {
        "META-INF/container.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>\n<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile '
            'full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>\n'),
        "OEBPS/content.opf": (
            '<?xml version="1.0" encoding="UTF-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            f'unique-identifier="bookid" xml:lang="{lang}"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:identifier id="bookid">{_xml(identifier)}</dc:identifier><dc:title>{title}</dc:title>'
            f'<dc:language>{lang}</dc:language><meta property="dcterms:modified">{modified}</meta></metadata>'
            '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            '<item id="c1" href="text/item-001.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine toc="ncx"><itemref idref="c1"/></spine></package>\n'),
        "OEBPS/nav.xhtml": head + (
            f'<head><meta charset="utf-8"/><title>{title}</title></head><body><nav epub:type="toc" id="toc">'
            f'<ol><li><a href="text/item-001.xhtml">{title}</a></li></ol></nav></body></html>\n'),
        "OEBPS/toc.ncx": (
            '<?xml version="1.0" encoding="UTF-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
            f'version="2005-1"><head><meta name="dtb:uid" content="{_xml(identifier)}"/></head><docTitle><text>'
            f'{title}</text></docTitle><navMap><navPoint id="np1" playOrder="1"><navLabel><text>{title}</text>'
            '</navLabel><content src="text/item-001.xhtml"/></navPoint></navMap></ncx>\n'),
        "OEBPS/text/item-001.xhtml": head + (
            f'<head><meta charset="utf-8"/><title>{title}</title></head><body>\n<h1>{title}</h1>\n{body}'
            '</body></html>\n'),
    }
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        zi = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, b"application/epub+zip")
        for name, content in parts.items():
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, content.encode("utf-8"))
    return bio.getvalue()
