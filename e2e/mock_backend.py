#!/usr/bin/env python3
"""Stand-in for the Send to X3 app, used to prove the firmware e2e harness.

Standard library only. It implements the device-facing contract of
docs/ARCHITECTURE.md §7 closely enough for the real CrossPoint firmware to exercise
every path the app will serve, so the same scenarios later run unchanged against the
real app (``run_e2e.py --backend local|docker``):

- ``POST /api/v1/ingest`` (Bearer ingest token or ``?token=``): JSON, form or text/plain
  ``{url?, text?, title?}``; no fetching (a URL becomes a short note), title made
  firmware-safe (§8 subset), EPUB + TXT rendered immediately.
- ``GET /device/v1/catalog?page=P&limit=L`` (Bearer device token): newest first, offset
  ``(P-1)*(L-1)``.
- ``GET /device/v1/items/<id>.epub|.txt``: marks ``delivered.catalog``.
- ``GET /device/v1/deliver?ev=&ts=``: idempotent per ``(ev, ts)``; digest of everything
  never delivered on any channel, oldest first, <= 900 KB; ``404`` when nothing is
  pending (status configurable to reproduce firmware regressions).
- ``GET /opds[?page=N]`` + ``/opds/items/<id>.epub`` (HTTP Basic ``x3``:device token):
  OPDS 1.2 acquisition feed, 50 per page, absolute next/previous links.
- LAN push worker (``--device-host``): polls ``/api/status`` of a CrossPoint File
  Transfer server, ``/mkdir`` + ``/upload`` one file per request, `` (2)`` suffix on
  "File already exists", verifies with ``/api/files``.
- ``GET /mock/state``: items, deliveries and push log as JSON (harness only).

Every response carries Content-Length, never gzip, never 204/304, no redirects;
device endpoints answer ``Connection: close``. Every request is appended to
``--log`` as one JSON line.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import io
import json
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

DIGEST_LIMIT = 900_000
OPDS_PAGE = 50
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


# ---------------------------------------------------------------------------------------
# Items and rendering
# ---------------------------------------------------------------------------------------


@dataclass
class Item:
    id: str
    created_at: int
    kind: str
    title: str
    byline: str
    source_url: str
    paragraphs: list[str]
    delivered: dict[str, int] = field(default_factory=dict)
    epub: bytes = b""
    txt: bytes = b""

    @property
    def word_count(self) -> int:
        return sum(len(p.split()) for p in self.paragraphs)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "created_at": self.created_at, "kind": self.kind, "title": self.title,
                "byline": self.byline, "source_url": self.source_url, "word_count": self.word_count,
                "delivered": dict(self.delivered), "epub_bytes": len(self.epub), "txt_bytes": len(self.txt),
                "epub_sha256": hashlib.sha256(self.epub).hexdigest(),
                "txt_sha256": hashlib.sha256(self.txt).hexdigest()}


def new_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(10))


def clean_text(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "")
    s = s.replace("­", "").replace("​", "").replace("﻿", "")
    s = "".join(ch for ch in s if ch in "\n\t" or (unicodedata.category(ch)[0] != "C" and ord(ch) < 0x1F000))
    return s


def safe_title(title: str, max_bytes: int = 90) -> str:
    """The firmware-relevant subset of §8."""
    t = re.sub(r"\s+", " ", clean_text(title)).strip()
    t = re.sub(r"(\.{2,}|…)+", "…", t)
    for a, b in (("/", "-"), ("\\", "-"), (":", " -"), ("?", ""), ('"', "'"), ("<", ""), (">", ""),
                 ("|", "-"), ("*", ""), ("{", ""), ("}", "")):
        t = t.replace(a, b)
    t = re.sub(r"~(\d)", r"- \1", t)
    t = re.sub(r"\s+", " ", t).strip().lstrip(". ").rstrip(". ")
    raw = t.encode("utf-8")
    if len(raw) > max_bytes:
        cut = raw[:max_bytes].decode("utf-8", "ignore")
        t = (cut.rsplit(" ", 1)[0] if " " in cut[len(cut) // 2:] else cut).rstrip(". ")
    return t or time.strftime("Note %Y-%m-%d %H-%M")


def _x(s: str) -> str:
    return html.escape(s, quote=True).replace("&#x27;", "&#39;")


def render_txt(item: Item) -> bytes:
    host = urllib.parse.urlsplit(item.source_url).hostname or ""
    meta = " · ".join(x for x in (item.byline, host, time.strftime("%Y-%m-%d", time.localtime(item.created_at))) if x)
    lines = [item.title, meta, "—" * 3, ""]
    for p in item.paragraphs:
        lines += [p, ""]
    if item.source_url:
        lines += ["—" * 3, f"Source: {item.source_url}"]
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def _chapter_body(item: Item) -> str:
    host = urllib.parse.urlsplit(item.source_url).hostname or ""
    parts = [f"<h1>{_x(item.title)}</h1>"]
    if item.byline:
        parts.append(f"<p><i>{_x(item.byline)}</i></p>")
    meta = " · ".join(x for x in (host, time.strftime("%Y-%m-%d", time.localtime(item.created_at))) if x)
    parts.append(f"<p><i>{_x(meta)}</i></p>")
    parts.append("<hr/>")
    parts += [f"<p>{_x(p)}</p>" for p in item.paragraphs]
    if host:
        parts.append(f"<hr/><p><i>Source: {_x(host)}</i></p>")
    return "\n".join(parts)


def render_epub(items: list[Item], book_title: str = "") -> bytes:
    """Minimal EPUB 3 + NCX, one chapter per item (§6.2 rules)."""
    if not items:
        raise ValueError("no items")
    title = book_title or items[0].title
    creator = items[0].byline if len(items) == 1 else "Send to X3"
    ident = f"urn:uuid:{uuid.uuid4()}"
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    head = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" '
            'xml:lang="en">\n')
    chapters = {f"text/item-{i + 1:03d}.xhtml": head + f"<head><meta charset=\"utf-8\"/><title>{_x(it.title)}</title>"
                f"</head>\n<body>\n{_chapter_body(it)}\n</body>\n</html>\n" for i, it in enumerate(items)}
    names = list(chapters)
    manifest = "\n".join(f'    <item id="c{i + 1}" href="{n}" media-type="application/xhtml+xml"/>'
                         for i, n in enumerate(names))
    spine = "\n".join(f'    <itemref idref="c{i + 1}"/>' for i in range(len(names)))
    creator_xml = f"<dc:creator>{_x(creator)}</dc:creator>" if creator else ""
    src = items[0].source_url if len(items) == 1 else ""
    source_xml = f"<dc:source>{_x(src)}</dc:source>" if src else ""
    opf = f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="en">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{ident}</dc:identifier>
    <dc:title>{_x(title)}</dc:title>
    {creator_xml}{source_xml}
    <dc:language>en</dc:language>
    <dc:date>{time.strftime("%Y-%m-%d", time.localtime(items[0].created_at))}</dc:date>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
{manifest}
  </manifest>
  <spine toc="ncx">
{spine}
  </spine>
</package>
'''
    nav_items = "".join(f'<li><a href="{n}">{_x(it.title)}</a></li>' for n, it in zip(names, items))
    nav = head + (f'<head><meta charset="utf-8"/><title>{_x(title)}</title></head>\n<body>\n'
                  f'<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>{nav_items}</ol></nav>\n</body>\n</html>\n')
    points = "".join(f'<navPoint id="np{i + 1}" playOrder="{i + 1}"><navLabel><text>{_x(it.title)}</text>'
                     f'</navLabel><content src="{n}"/></navPoint>' for i, (n, it) in enumerate(zip(names, items)))
    ncx = (f'<?xml version="1.0" encoding="UTF-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
           f'version="2005-1" xml:lang="en">\n<head><meta name="dtb:uid" content="{ident}"/>'
           f'<meta name="dtb:depth" content="1"/><meta name="dtb:totalPageCount" content="0"/>'
           f'<meta name="dtb:maxPageNumber" content="0"/></head>\n<docTitle><text>{_x(title)}</text></docTitle>\n'
           f'<navMap>{points}</navMap>\n</ncx>\n')
    container = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                 '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles>\n</container>\n')
    parts = {"META-INF/container.xml": container, "OEBPS/content.opf": opf, "OEBPS/nav.xhtml": nav,
             "OEBPS/toc.ncx": ncx, **{f"OEBPS/{n}": body for n, body in chapters.items()}}
    for name, doc in parts.items():
        try:
            ET.fromstring(doc.encode("utf-8"))
        except ET.ParseError as e:  # pragma: no cover - a bug in this file
            raise AssertionError(f"malformed {name}: {e}") from e
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        zi = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, b"application/epub+zip")
        for name, doc in parts.items():
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, doc.encode("utf-8"))
    return bio.getvalue()


# ---------------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------------


class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.items: dict[str, Item] = {}
        self.deliveries: dict[tuple[str, str], tuple[list[str], bytes]] = {}
        self.push_log: list[dict[str, Any]] = []

    def add(self, *, title: str, paragraphs: list[str], kind: str, source_url: str = "", byline: str = "") -> Item:
        with self.lock:
            taken = {i.title for i in self.items.values()}
            base = safe_title(title)
            t, n = base, 2
            while t in taken:
                t = f"{base} ({n})"
                n += 1
            item = Item(id=new_id(), created_at=int(time.time()), kind=kind, title=t, byline=byline,
                        source_url=source_url, paragraphs=paragraphs or ["(empty)"])
            while item.id in self.items:
                item.id = new_id()
            item.txt = render_txt(item)
            item.epub = render_epub([item])
            self.items[item.id] = item
            return item

    def newest_first(self) -> list[Item]:
        with self.lock:
            # Stable for equal timestamps: later insertion = newer.
            order = list(self.items.values())
            return sorted(reversed(order), key=lambda i: -i.created_at)

    def mark(self, item: Item, channel: str) -> None:
        with self.lock:
            item.delivered.setdefault(channel, int(time.time()))

    def deliver(self, ev: str, ts: str) -> Optional[bytes]:
        with self.lock:
            if (ev, ts) in self.deliveries:
                return self.deliveries[(ev, ts)][1]
            pending = [i for i in reversed(self.newest_first()) if not i.delivered]
            if not pending:
                return None
            batch: list[Item] = []
            size = 0
            for it in pending:
                if batch and size + len(it.epub) > DIGEST_LIMIT:
                    break
                batch.append(it)
                size += len(it.epub)
            if len(batch) == 1:
                data = batch[0].epub
            else:
                data = render_epub(batch, f"Inbox: {len(batch)} items ({time.strftime('%Y-%m-%d %H:%M')})")
            for it in batch:
                self.mark(it, "auto")
            self.deliveries[(ev, ts)] = ([i.id for i in batch], data)
            return data

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"items": [i.public() for i in self.newest_first()],
                    "deliveries": [{"ev": ev, "ts": ts, "items": ids, "bytes": len(data),
                                    "sha256": hashlib.sha256(data).hexdigest()}
                                   for (ev, ts), (ids, data) in self.deliveries.items()],
                    "push_log": list(self.push_log)}


# ---------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------


def _paragraphs(text: str) -> list[str]:
    text = clean_text(text).replace("\r\n", "\n").replace("\r", "\n")
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    return [p for p in paras if p]


def _subtitle(item: Item) -> str:
    when = item.delivered and min(item.delivered.values())
    state = f"Read on X3 {time.strftime('%b %d', time.localtime(when))}" if when else "New"
    host = urllib.parse.urlsplit(item.source_url).hostname or ("pasted text" if item.kind == "text" else "")
    return " · ".join(x for x in (state, f"{item.word_count} words", host) if x)


def _opds_time(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SendToX3-Mock/1.0"
    sys_version = ""

    server: "MockServer"

    # -- plumbing -------------------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - quiet by default
        if self.server.verbose:
            sys.stderr.write("[mock] " + (fmt % args) + "\n")

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json", *,
              close: bool = True, extra: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._status = status
        self._resp_len = len(body)

    def _json(self, status: int, doc: Any, **kw: Any) -> None:
        self._send(status, json.dumps(doc, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   **kw)

    def _bearer(self, token: str, query: dict[str, list[str]], allow_query: bool = False) -> bool:
        got = self.headers.get("Authorization", "")
        if got.startswith("Bearer ") and hmac.compare_digest(got[7:].strip(), token):
            return True
        return allow_query and hmac.compare_digest((query.get("token") or [""])[0], token)

    def _basic(self) -> bool:
        if not self.server.opds_auth:
            return True
        got = self.headers.get("Authorization", "")
        if not got.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(got[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return user == "x3" and hmac.compare_digest(pw, self.server.device_token)

    def _handle(self) -> None:
        started = time.time()
        self._status, self._resp_len = 0, 0
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            self._route(body)
        except Exception as e:  # noqa: BLE001 - a mock must never hang the device
            if not self._status:
                self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        finally:
            self.server.log({
                "t": started, "method": self.command, "path": self.path, "version": self.request_version,
                "headers": [[k, v] for k, v in self.headers.items()],
                "body": body[:2048].decode("utf-8", "replace"), "body_bytes": len(body),
                "status": self._status, "response_bytes": self._resp_len,
            })

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle

    # -- routes ---------------------------------------------------------------------------

    def _route(self, body: bytes) -> None:
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        st = self.server.state
        path = url.path

        if path == "/healthz":
            return self._send(200, b"ok\n", "text/plain; charset=utf-8")
        if path == "/mock/state":
            return self._json(200, st.snapshot())

        if path == "/api/v1/ingest":
            if self.command != "POST":
                return self._json(405, {"ok": False, "error": "POST only"})
            if not self._bearer(self.server.ingest_token, q, allow_query=True):
                return self._json(401, {"ok": False, "error": "unauthorized"})
            fields = self._ingest_fields(body)
            if fields is None:
                return self._json(422, {"ok": False, "error": "unsupported body (mock: JSON, form or text/plain)"})
            text, url_in, title = fields.get("text", ""), fields.get("url", "").strip(), fields.get("title", "")
            if not text.strip() and not url_in:
                return self._json(422, {"ok": False, "error": "nothing to save"})
            if not url_in and re.fullmatch(r"https?://\S+", text.strip()):
                url_in, text = text.strip(), ""
            if url_in and not text.strip():
                host = urllib.parse.urlsplit(url_in).hostname or url_in
                item = st.add(title=title or host, paragraphs=[f"Saved link: {url_in}"], kind="url",
                              source_url=url_in, byline=host)
            else:
                paras = _paragraphs(text)
                item = st.add(title=title or (paras[0][:80] if paras else ""), paragraphs=paras, kind="text",
                              source_url=url_in)
            return self._json(200, {"ok": True, "id": item.id, "title": item.title, "status": "ready"})

        if path.startswith("/device/v1/"):
            if not self._bearer(self.server.device_token, q):
                return self._send(401, b'{"error":"unauthorized"}')
            if path == "/device/v1/catalog":
                try:
                    page = max(1, int((q.get("page") or ["1"])[0]))
                    limit = min(50, max(1, int((q.get("limit") or ["9"])[0])))
                except ValueError:
                    return self._send(400, b'{"error":"bad paging"}')
                rows = st.newest_first()
                off = (page - 1) * max(1, limit - 1)
                items = [{"id": i.id, "title": i.title, "subtitle": _subtitle(i)} for i in rows[off:off + limit]]
                return self._json(200, {"items": items})
            m = re.fullmatch(r"/device/v1/items/([a-z2-7]{10})\.(epub|txt)", path)
            if m:
                item = st.items.get(m.group(1))
                if not item:
                    return self._send(404, b"not found\n", "text/plain")
                st.mark(item, "catalog")
                if m.group(2) == "epub":
                    return self._send(200, item.epub, "application/epub+zip")
                return self._send(200, item.txt, "text/plain; charset=utf-8")
            if path == "/device/v1/deliver":
                ev, ts = (q.get("ev") or [""])[0], (q.get("ts") or [""])[0]
                if not ev or not ts:
                    return self._send(400, b"missing ev/ts\n", "text/plain")
                data = st.deliver(ev, ts)
                if data is None:
                    status = self.server.empty_deliver_status
                    return self._send(status, b"nothing new\n" if status != 204 else b"", "text/plain")
                return self._send(200, data, "application/epub+zip")
            return self._send(404, b'{"error":"no route"}')

        if path == "/opds" or path.startswith("/opds/"):
            if not self._basic():
                return self._send(401, b"unauthorized\n", "text/plain", extra={"WWW-Authenticate": 'Basic realm="x3"'})
            if path == "/opds":
                return self._opds_feed(q)
            m = re.fullmatch(r"/opds/items/([a-z2-7]{10})\.epub", path)
            if m and m.group(1) in st.items:
                item = st.items[m.group(1)]
                st.mark(item, "opds")
                return self._send(200, item.epub, "application/epub+zip")
            return self._send(404, b"not found\n", "text/plain")

        return self._send(404, b'{"error":"no route"}')

    def _ingest_fields(self, body: bytes) -> Optional[dict[str, str]]:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype == "application/json":
            try:
                doc = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return None
            if not isinstance(doc, dict):
                return None
            return {k: str(v) for k, v in doc.items() if isinstance(v, (str, int)) and k in ("url", "text", "title")}
        if ctype.startswith("multipart/"):
            return None
        if ctype == "application/x-www-form-urlencoded":
            q = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
            return {k: v[0] for k, v in q.items() if k in ("url", "text", "title")}
        return {"text": body.decode("utf-8", "replace")}

    def _opds_feed(self, q: dict[str, list[str]]) -> None:
        st = self.server.state
        try:
            page = max(1, int((q.get("page") or ["1"])[0]))
        except ValueError:
            page = 1
        rows = st.newest_first()
        chunk = rows[(page - 1) * OPDS_PAGE: page * OPDS_PAGE]
        base = f"http://{self.headers.get('Host', '127.0.0.1')}"
        updated = _opds_time(max([i.created_at for i in rows] or [int(time.time())]))
        links = ['<link rel="self" type="application/atom+xml;profile=opds-catalog;kind=acquisition" '
                 f'href="{base}/opds?page={page}"/>']
        if page * OPDS_PAGE < len(rows):
            links.append('<link rel="next" type="application/atom+xml;profile=opds-catalog;kind=acquisition" '
                         f'href="{base}/opds?page={page + 1}"/>')
        if page > 1:
            links.append('<link rel="previous" type="application/atom+xml;profile=opds-catalog;kind=acquisition" '
                         f'href="{base}/opds?page={page - 1}"/>')
        entries = []
        for it in chunk:
            author = it.byline or (urllib.parse.urlsplit(it.source_url).hostname or "Send to X3")
            title = it.title.encode("utf-8")[:120].decode("utf-8", "ignore")
            entries.append(
                f"<entry><title>{_x(title)}</title><author><name>{_x(author)}</name></author>"
                f"<id>urn:sendtox3:{it.id}</id><updated>{_opds_time(it.created_at)}</updated>"
                f'<link rel="http://opds-spec.org/acquisition" type="application/epub+zip" '
                f'href="/opds/items/{it.id}.epub"/></entry>')
        feed = ('<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">'
                f"<id>urn:sendtox3:inbox</id><title>Send to X3</title><updated>{updated}</updated>"
                + "".join(links) + "".join(entries) + "</feed>\n")
        self._send(200, feed.encode("utf-8"), "application/atom+xml;profile=opds-catalog;kind=acquisition")


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], *, ingest_token: str, device_token: str, log_path: Optional[str],
                 empty_deliver_status: int = 404, opds_auth: bool = True, verbose: bool = False) -> None:
        super().__init__(addr, Handler)
        self.state = State()
        self.ingest_token = ingest_token
        self.device_token = device_token
        self.log_path = log_path
        self.empty_deliver_status = empty_deliver_status
        self.opds_auth = opds_auth
        self.verbose = verbose
        self._log_lock = threading.Lock()

    def log(self, rec: dict[str, Any]) -> None:
        if not self.log_path:
            return
        with self._log_lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------------------
# LAN push (§9) against a CrossPoint File Transfer server
# ---------------------------------------------------------------------------------------


class Pusher(threading.Thread):
    def __init__(self, state: State, device_host: str, *, poll: float = 2.0, folder: str = "/Inbox",
                 fmt: str = "txt") -> None:
        super().__init__(daemon=True, name="lan-push")
        self.state, self.base, self.poll, self.folder, self.fmt = state, f"http://{device_host}", poll, folder, fmt
        self.stop_event = threading.Event()

    def log(self, **rec: Any) -> None:
        rec["t"] = time.time()
        with self.state.lock:
            self.state.push_log.append(rec)

    def _req(self, method: str, path: str, data: Optional[bytes] = None, headers: Optional[dict] = None,
             timeout: float = 10) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                status, _ = self._req("GET", "/api/status", timeout=2)
                if status == 200:
                    self.push_pending()
            except (OSError, urllib.error.URLError):
                pass
            self.stop_event.wait(self.poll)

    def _listing(self) -> dict[str, int]:
        status, body = self._req("GET", "/api/files?path=" + urllib.parse.quote(self.folder))
        if status != 200:
            return {}
        return {e["name"].lower(): int(e.get("size", -1)) for e in json.loads(body.decode("utf-8") or "[]")
                if isinstance(e, dict) and "name" in e}

    def push_pending(self) -> None:
        with self.state.lock:
            todo = [i for i in reversed(self.state.newest_first()) if "push" not in i.delivered]
        if not todo:
            return
        parent, _, name = self.folder.rstrip("/").rpartition("/")
        status, body = self._req("POST", "/mkdir", urllib.parse.urlencode({"name": name, "path": parent or "/"})
                                 .encode(), {"Content-Type": "application/x-www-form-urlencoded"})
        self.log(op="mkdir", status=status, body=body.decode("utf-8", "replace")[:200])
        for item in todo:
            data = item.txt if self.fmt == "txt" else item.epub
            existing = self._listing()
            for n in range(1, 50):
                fname = f"{item.title}.{self.fmt}" if n == 1 else f"{item.title} ({n}).{self.fmt}"
                if fname.lower() in existing:
                    continue
                status, body = self._upload(fname, data)
                text = body.decode("utf-8", "replace")
                self.log(op="upload", file=fname, status=status, body=text[:200], bytes=len(data))
                if status == 400 and "already exists" in text:
                    existing[fname.lower()] = -1
                    continue
                if status == 200 and self._listing().get(fname.lower()) == len(data):
                    self.state.mark(item, "push")
                    self.log(op="verified", file=fname, id=item.id)
                break

    def _upload(self, fname: str, data: bytes) -> tuple[int, bytes]:
        boundary = "x3" + secrets.token_hex(12)
        # filename must be the last, quoted parameter (arduino-esp32 Parsing.cpp:439-455).
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
        payload = head + data + f"\r\n--{boundary}--\r\n".encode()
        return self._req("POST", "/upload?path=" + urllib.parse.quote(self.folder), payload,
                         {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=60)


# ---------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------


def serve(*, host: str = "127.0.0.1", port: int = 0, ingest_token: str, device_token: str,
          log_path: Optional[str] = None, empty_deliver_status: int = 404, opds_auth: bool = True,
          device_host: str = "", push_poll: float = 2.0, push_format: str = "txt",
          verbose: bool = False) -> tuple[MockServer, threading.Thread, Optional[Pusher]]:
    """Start the mock in background threads (for in-process use and tests)."""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the mock backend only binds loopback")
    srv = MockServer((host, port), ingest_token=ingest_token, device_token=device_token, log_path=log_path,
                     empty_deliver_status=empty_deliver_status, opds_auth=opds_auth, verbose=verbose)
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="mock-http")
    t.start()
    pusher = None
    if device_host:
        pusher = Pusher(srv.state, device_host, poll=push_poll, fmt=push_format)
        pusher.start()
    return srv, t, pusher


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="0 = pick a free port (printed on stdout)")
    ap.add_argument("--ingest-token", default="ingest-token-0123456789abcdef0123")
    ap.add_argument("--device-token", default="device-token-0123456789abcdef0123")
    ap.add_argument("--log", help="append every request as JSON lines")
    ap.add_argument("--empty-deliver-status", type=int, default=404,
                    help="status of /device/v1/deliver when nothing is pending (spec: 404)")
    ap.add_argument("--no-opds-auth", action="store_true")
    ap.add_argument("--device-host", default="", help="host:port of a CrossPoint File Transfer server to push to")
    ap.add_argument("--push-poll", type=float, default=2.0)
    ap.add_argument("--push-format", choices=("txt", "epub"), default="txt")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    srv, t, _ = serve(host=a.host, port=a.port, ingest_token=a.ingest_token, device_token=a.device_token,
                      log_path=a.log, empty_deliver_status=a.empty_deliver_status, opds_auth=not a.no_opds_auth,
                      device_host=a.device_host, push_poll=a.push_poll, push_format=a.push_format,
                      verbose=a.verbose)
    print(f"READY http://{a.host}:{srv.server_address[1]}", flush=True)
    try:
        t.join()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
