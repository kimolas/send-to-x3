"""OPDS 1.2 acquisition feed for CrossPoint 1.6.5+ (``GET /opds``).

CrossPoint's OPDS client (research report u-opds.md, OpdsParser.cpp):

- 1.6.5 sends HTTP Basic up front whenever user *and* password are set; 2.x sends it
  only after a ``401`` with a ``WWW-Authenticate: Basic`` challenge, which this feed
  answers. Password = device token (any user; the setup page says "x3").
- Its URL joiner is not RFC 3986: only absolute or root-relative hrefs resolve correctly.
- An entry is a book only if ``rel`` contains ``opds-spec.org/acquisition`` and
  ``type`` is exactly ``application/epub+zip``; feed-level ``rel="next"``/``"previous"``
  become paging rows; at most 62 entries are kept.
- Titles are not trimmed (no whitespace around them) and become ``Author - Title.epub``
  cut to 100 bytes, so the author is omitted when it would push the title out.
- expat without DTD entities: only the five XML escapes are used.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Iterable

from aiohttp import web
from lxml import etree

from ..models import Item
from .. import delivery
from .common import basic_password, bearer_token, bytes_response, get_state, json_error, parse_int, tokens_equal

ATOM_NS = "http://www.w3.org/2005/Atom"
ACQUISITION_FEED = "application/atom+xml;profile=opds-catalog;kind=acquisition"
FEED_CONTENT_TYPE = ACQUISITION_FEED + "; charset=utf-8"
ACQUISITION_REL = "http://opds-spec.org/acquisition"
EPUB_TYPE = "application/epub+zip"
#: Entries per page. The spec allows up to 50; 25 keeps the ESP32-C3's heap comfortable
#: (the parser keeps every entry in memory) while still showing a week of reading.
PAGE_SIZE = 25
MAX_TITLE_BYTES = 120
_FILENAME_BYTES = 100  # CrossPoint cuts "Author - Title" to 100 bytes
#: Everything that is not a character lxml/expat accept in XML 1.0 text (C0 controls,
#: lone surrogates, U+FFFE/U+FFFF), plus DEL, C1 controls and the U+FDD0-U+FDEF
#: noncharacters. A site name with U+FFFF otherwise made lxml raise and /opds answer 500.
_CTRL = re.compile(r"[^\t\n\r\x20-\x7e\xa0-\ud7ff\ue000-\ufdcf\ufdf0-\ufffd\U00010000-\U0010ffff]")


def _xml_text(s: str) -> str:
    """Single-line text safe for an XML text node."""
    return " ".join(_CTRL.sub(" ", s or "").split())


def _cut_bytes(s: str, limit: int) -> str:
    raw = s.encode("utf-8")
    if len(raw) <= limit:
        return s
    return raw[:limit].decode("utf-8", "ignore").rstrip()


def entry_title(title: str) -> str:
    t = _xml_text(delivery.device_title(title))
    return _cut_bytes(t, MAX_TITLE_BYTES) or "Untitled"


def entry_author(item: Item, title: str) -> str:
    """Short origin label, only if ``author - title`` still fits the 100-byte filename."""
    author = _xml_text(delivery.origin_label(item))
    if not author:
        return ""
    if len(author.encode()) + 3 + len(title.encode()) > _FILENAME_BYTES:
        return ""
    return author


def _iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _page_href(page: int) -> str:
    return "/opds" if page <= 1 else f"/opds?page={page}"


def build_feed(items: Iterable[Item], *, page: int, has_next: bool, now: int | None = None) -> bytes:
    """Serialize one page of the feed (UTF-8, no pretty-printing inside text nodes)."""
    items = list(items)
    now = int(now or time.time())
    updated = max([it.created_at for it in items], default=now)

    def sub(parent, tag: str, text: str | None = None, **attrs: str):
        el = etree.SubElement(parent, f"{{{ATOM_NS}}}{tag}", attrs)
        if text is not None:
            el.text = text
        return el

    feed = etree.Element(f"{{{ATOM_NS}}}feed", nsmap={None: ATOM_NS})
    sub(feed, "id", "urn:sendtox3:feed" if page <= 1 else f"urn:sendtox3:feed:page:{page}")
    sub(feed, "title", "Send to X3")
    sub(feed, "updated", _iso(updated))
    author = sub(feed, "author")
    sub(author, "name", "Send to X3")
    sub(feed, "link", rel="self", type=ACQUISITION_FEED, href=_page_href(page))
    sub(feed, "link", rel="start", type=ACQUISITION_FEED, href="/opds")
    if page > 1:
        sub(feed, "link", rel="previous", type=ACQUISITION_FEED, href=_page_href(page - 1))
    if has_next:
        sub(feed, "link", rel="next", type=ACQUISITION_FEED, href=_page_href(page + 1))
    for it in items:
        entry = sub(feed, "entry")
        title = entry_title(it.title)
        sub(entry, "title", title)
        sub(entry, "id", f"urn:sendtox3:{it.id}")
        sub(entry, "updated", _iso(it.created_at))
        name = entry_author(it, title)
        if name:
            a = sub(entry, "author")
            sub(a, "name", name)
        sub(entry, "summary", _xml_text(delivery.catalog_subtitle(it)), type="text")
        sub(entry, "link", rel=ACQUISITION_REL, type=EPUB_TYPE, href=f"/opds/items/{it.id}.epub")
    return etree.tostring(feed, xml_declaration=True, encoding="UTF-8")


def _authorized(request: web.Request) -> bool:
    expected = get_state(request).opts.device_token
    password = basic_password(request)
    if password is not None:
        return tokens_equal(password, expected)
    return tokens_equal(bearer_token(request), expected)


def _unauthorized() -> web.Response:
    return json_error(401, "OPDS needs a user name (any, e.g. x3) and the device token as password",
                      headers={"WWW-Authenticate": 'Basic realm="Send to X3", charset="UTF-8"'})


async def feed(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _unauthorized()
    state = get_state(request)
    page = max(1, parse_int(request.query.get("page"), 1))
    rows = state.store.list_items(offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE + 1, newest_first=True)
    body = build_feed(rows[:PAGE_SIZE], page=page, has_next=len(rows) > PAGE_SIZE)
    state.set_status("opds", "ok", f"feed page {page}")
    return bytes_response(body, FEED_CONTENT_TYPE)


async def download(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _unauthorized()
    state = get_state(request)
    item_id = request.match_info["item_id"]
    item = state.store.get(item_id)
    if item is None:
        return json_error(404, "no such item")
    try:
        data = await asyncio.to_thread(state.store.file_bytes, item_id, "epub")
    except FileNotFoundError:
        return json_error(404, "item file missing")
    state.store.mark_delivered(item_id, "opds")
    state.set_status("opds", "ok", f"downloaded {item.title!r}")
    return bytes_response(data, EPUB_TYPE)


def add_routes(app: web.Application) -> None:
    app.router.add_get("/opds", feed, allow_head=False)
    app.router.add_get("/opds/", feed, allow_head=False)
    app.router.add_get("/opds/items/{item_id:[a-z2-7]{10}}.epub", download, allow_head=False)
