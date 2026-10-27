"""What the X3 receives: zero-tap auto-deliveries and the plugin catalog pages.

Firmware facts this module is built around (CrossPoint 2.x, see docs/ARCHITECTURE.md §7.2):

- ``sleep.enter`` downloads are capped at 1 MiB, never follow redirects, and treat any
  2xx (even an empty body) as success; retries reuse the same ``(event.id, event.ts)``.
  So a repeat of a known ``(ev, ts)`` must get byte-identical output, and "nothing new"
  must be a 404 (``build_auto_delivery`` returns ``None``).
- The catalog sends ``limit = page_size + 1`` and uses the extra row to detect a next
  page, so the offset is ``(page - 1) * (limit - 1)``.

Each item's :class:`~sendtox3.textmodel.Document` is kept as ``doc.json`` next to its
rendered files so several items can be re-rendered into one digest EPUB. Items without
``doc.json`` (an uploaded EPUB kept as-is) are always delivered on their own.

A pending item can be left out of zero-tap deliveries without being marked delivered
(:meth:`Store.set_auto_skip`): one too large for the device got its "too large" note
once, and one whose stored EPUB is missing or empty is flagged failed. Both stay pending
for LAN push and show as "New" in the catalog.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

from .models import Item
from .store import DIR_MODE, Store, write_file
from .textmodel import Document, Heading, ListItem, Paragraph, Preformatted, Quote, Rule

log = logging.getLogger(__name__)

#: Total size a digest may reach ("up to 900 KB").
AUTO_CAP_BYTES = 900_000
#: A single item larger than the digest cap is still sent alone up to this size; the
#: device aborts event downloads above 1,048,576 bytes and would retry forever.
SINGLE_MAX_BYTES = 1_000_000
#: ``{limit}`` is page_size + 1 with page_size clamped to 1..16 by the firmware.
LIMIT_MIN, LIMIT_MAX = 2, 17

DOC_FILE = "doc.json"
_DOC_VERSION = 1

_delivery_lock = threading.Lock()


# --------------------------------------------------------------------------------------
# Dependency seams (other components; imported lazily so tests can substitute them)
# --------------------------------------------------------------------------------------


def render_epub(docs: list[Document], *, book_title: str = "", identifier: str = "") -> bytes:
    from .render.epub import render_epub as impl

    return impl(docs, book_title=book_title, identifier=identifier)


def safe_title(title: str, *, max_bytes: int = 90) -> str:
    from .render.names import safe_title as impl

    return impl(title, max_bytes=max_bytes)


# --------------------------------------------------------------------------------------
# Document persistence (doc.json)
# --------------------------------------------------------------------------------------

_BLOCK_TYPES: dict[str, type] = {
    cls.__name__: cls for cls in (Heading, Paragraph, ListItem, Quote, Preformatted, Rule)
}


def document_to_dict(doc: Document) -> dict[str, Any]:
    blocks = []
    for b in doc.blocks:
        d = dataclasses.asdict(b)
        d["type"] = type(b).__name__
        blocks.append(d)
    return {
        "v": _DOC_VERSION,
        "title": doc.title,
        "byline": doc.byline,
        "site": doc.site,
        "source_url": doc.source_url,
        "published": doc.published,
        "lang": doc.lang,
        "notes": list(doc.notes),
        "blocks": blocks,
    }


def document_from_dict(data: dict[str, Any]) -> Document:
    blocks = []
    for raw in data.get("blocks") or []:
        cls = _BLOCK_TYPES.get(str(raw.get("type")))
        if cls is None:
            continue
        names = {f.name for f in dataclasses.fields(cls)}
        blocks.append(cls(**{k: v for k, v in raw.items() if k in names}))
    return Document(
        title=str(data.get("title") or ""),
        blocks=blocks,
        byline=str(data.get("byline") or ""),
        site=str(data.get("site") or ""),
        source_url=str(data.get("source_url") or ""),
        published=str(data.get("published") or ""),
        lang=str(data.get("lang") or "en"),
        notes=[str(n) for n in data.get("notes") or []],
    )


def save_document(store: Store, item_id: str, doc: Document, *, item_title: str = "") -> None:
    """Write ``doc.json`` atomically into the item's folder (created if needed).

    ``doc.title`` is the headline shown inside the book; ``item_title`` (the item's
    firmware-safe title when it was stored) lets a digest tell whether the item was
    renamed since (then the new name wins).
    """
    d = store.item_dir(item_id)
    d.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    data = document_to_dict(doc)
    if item_title:
        data["item_title"] = item_title
    write_file(d / DOC_FILE, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _read_doc_json(store: Store, item_id: str) -> Optional[dict[str, Any]]:
    path = store.item_dir(item_id) / DOC_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        log.warning("item %s: unreadable %s (%s)", item_id, DOC_FILE, exc)
        return None
    return data if isinstance(data, dict) else None


def load_document(store: Store, item_id: str) -> Optional[Document]:
    """The stored Document, or None (EPUB passthrough items, or a damaged file)."""
    data = _read_doc_json(store, item_id)
    if data is None:
        return None
    try:
        return document_from_dict(data)
    except (AttributeError, ValueError, TypeError) as exc:
        log.warning("item %s: unreadable %s (%s)", item_id, DOC_FILE, exc)
        return None


def _digest_document(store: Store, item: Item) -> Optional[Document]:
    """The item's Document titled for a digest chapter: the stored headline, unless the
    item was renamed after it was stored."""
    data = _read_doc_json(store, item.id)
    if data is None:
        return None
    try:
        doc = document_from_dict(data)
    except (AttributeError, ValueError, TypeError) as exc:
        log.warning("item %s: unreadable %s (%s)", item.id, DOC_FILE, exc)
        return None
    if not (doc.title and data.get("item_title") == item.title):
        doc = dataclasses.replace(doc, title=item.title)
    return doc


# --------------------------------------------------------------------------------------
# Auto-delivery (sleep.enter)
# --------------------------------------------------------------------------------------


def build_auto_delivery(store: Store, ev: str, ts: str) -> bytes | None:
    """EPUB bytes for this device event, or None when nothing is pending (-> 404).

    A repeat of a known ``(ev, ts)`` returns the stored bytes. Otherwise every item that
    was never delivered on any channel is sent, oldest first: one item as its own EPUB,
    several as a digest of at most :data:`AUTO_CAP_BYTES`. An oldest item larger than the
    cap goes alone if it is at most :data:`SINGLE_MAX_BYTES`, else as a short note telling
    the user to open it from the catalog. Delivered items are marked ``auto``; an item
    that only got the note stays pending (LAN push and the catalog still offer it) and is
    left out of later zero-tap deliveries. An item whose EPUB is missing or empty is
    flagged failed and skipped, so it cannot block the queue.
    """
    with _delivery_lock:
        replay = store.get_delivery(ev, ts)
        if replay is not None:
            return replay[1]
        pending = store.pending_items()
        if not pending:
            return None
        skipped = store.auto_skipped()
        pending = [it for it in pending if it.id not in skipped]
        if not pending:
            return None
        batch, data, note_only = _assemble(store, pending)
        if not batch or not data:
            return None
        store.save_delivery(ev, ts, [it.id for it in batch], data)
        if note_only:
            store.set_auto_skip(batch[0].id, "too_large")
        else:
            now = int(time.time())
            for it in batch:
                store.mark_delivered(it.id, "auto", now)
        log.info("auto-delivery ev=%s ts=%s: %d item(s)%s, %d bytes", ev, ts, len(batch),
                 " (too-large note only)" if note_only else "", len(data))
        return data


def _damaged(store: Store, item: Item, what: str) -> None:
    log.warning("item %s: %s; left out of auto-delivery and flagged failed", item.id, what)
    store.mark_failed(item.id, f"The stored file is damaged ({what}). Delete this item and share it again.")
    store.set_auto_skip(item.id, "damaged")


def _assemble(store: Store, pending: list[Item]) -> tuple[list[Item], bytes | None, bool]:
    """``(items, payload, note_only)``: ``note_only`` when the payload is the "too large"
    note for ``items[0]`` rather than the item itself."""
    sized: list[tuple[Item, int]] = []
    for it in pending:
        try:
            size = store.file_path(it.id, "epub").stat().st_size
        except FileNotFoundError:
            _damaged(store, it, "its EPUB file is missing")
            continue
        except OSError as exc:
            log.warning("item %s: EPUB not readable (%s); skipped this time", it.id, exc)
            continue
        if size == 0:
            _damaged(store, it, "its EPUB file is empty")
            continue
        sized.append((it, size))
    if not sized:
        return [], None, False

    first, first_size = sized[0]
    first_doc = _digest_document(store, first) if first_size <= AUTO_CAP_BYTES else None
    if first_doc is None:
        return _single_payload(store, first, first_size)

    batch: list[tuple[Item, Document]] = [(first, first_doc)]
    total = first_size
    for it, size in sized[1:]:
        if total + size > AUTO_CAP_BYTES:
            break
        doc = _digest_document(store, it)
        if doc is None:  # passthrough EPUB: it goes alone in a later delivery
            break
        batch.append((it, doc))
        total += size

    while len(batch) > 1:
        data = _render_digest(batch)
        if len(data) <= AUTO_CAP_BYTES:
            return [it for it, _ in batch], data, False
        batch.pop()  # the estimate was too low; drop the newest and try again
    return _single_payload(store, first, first_size)


def _single_payload(store: Store, item: Item, size: int) -> tuple[list[Item], bytes | None, bool]:
    if size > SINGLE_MAX_BYTES:
        return [item], too_large_note(item, size), True
    try:
        data = store.file_bytes(item.id, "epub")
    except FileNotFoundError:
        _damaged(store, item, "its EPUB file is missing")
        return [], None, False
    except OSError as exc:
        log.warning("item %s: EPUB not readable (%s); skipped this time", item.id, exc)
        return [], None, False
    if not data:
        _damaged(store, item, "its EPUB file is empty")
        return [], None, False
    return [item], data, False


def _render_digest(batch: list[tuple[Item, Document]]) -> bytes:
    """Digest EPUB of ``(item, document)`` pairs; each document's title is its chapter
    title (see :func:`_digest_document`)."""
    docs = [doc if doc.title else dataclasses.replace(doc, title=it.title) for it, doc in batch]
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    title = f"Inbox: {len(docs)} items ({stamp})"
    ident = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "sendtox3:digest:" + "+".join(i.id for i, _ in batch)))
    return render_epub(docs, book_title=title, identifier=ident)


def too_large_note(item: Item, size: int) -> bytes:
    """A small EPUB telling the user to fetch ``item`` from the catalog instead."""
    mb = size / 1_000_000
    doc = Document(
        title=f"Too large to deliver: {item.title}",
        blocks=[
            Paragraph(
                f"“{item.title}” is {mb:.1f} MB, which is more than the X3 accepts "
                "for automatic delivery (1 MB)."
            ),
            Paragraph("Open it from the catalog instead: Home > Plugins > Send to X3, then select it."),
        ],
        site=_host(item.source_url),
        source_url=item.source_url,
    )
    ident = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "sendtox3:too-large:" + item.id))
    return render_epub([doc], book_title=doc.title, identifier=ident)


# --------------------------------------------------------------------------------------
# Catalog (plugin browse)
# --------------------------------------------------------------------------------------


def clamp_paging(page: int, limit: int) -> tuple[int, int]:
    """Clamp to the values the firmware can send: page >= 1, limit 2..17."""
    return max(1, int(page)), min(LIMIT_MAX, max(LIMIT_MIN, int(limit)))


def catalog_page(store: Store, page: int, limit: int) -> dict:
    """``{"items": [{"id", "title", "subtitle"}]}``, newest first.

    Returns up to ``limit`` items starting at ``(page - 1) * (limit - 1)``.
    """
    page, limit = clamp_paging(page, limit)
    offset = (page - 1) * (limit - 1)
    items = store.list_items(offset=offset, limit=limit, newest_first=True)
    return {"items": [catalog_entry(it) for it in items]}


def catalog_entry(item: Item) -> dict[str, str]:
    return {"id": item.id, "title": device_title(item.title), "subtitle": catalog_subtitle(item)}


def catalog_subtitle(item: Item) -> str:
    """``"<New|Read on X3 date> · <words> words · <host>"``."""
    if item.delivered:
        first = min(int(v) for v in item.delivered.values())
        parts = ["Read on X3 " + time.strftime("%Y-%m-%d", time.localtime(first))]
    else:
        parts = ["New"]
    if item.status == "failed":
        parts.append("extraction failed")
    else:
        parts.append("1 word" if item.word_count == 1 else f"{item.word_count:,} words")
    origin = origin_label(item)
    if origin:
        parts.append(origin)
    return " · ".join(parts)


def origin_label(item: Item) -> str:
    host = _host(item.source_url)
    if host:
        return host
    if item.site:
        return item.site
    return {"email": "email", "pdf": "PDF", "text": "note"}.get(item.kind, "")


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "") if url else ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# Patterns the firmware chokes on in a title used as a filename (see §8). U+2026 is the
# safe replacement names.py uses for "..", so it is allowed.
_UNSAFE_TITLE = re.compile(r"\.\.|[{}/\\:*?\"<>|\x00-\x1f\x7f]|~\d|^[\s.]|[\s.]$")


def device_title(title: str) -> str:
    """Titles are sanitized at ingest; re-sanitize only if something unsafe slipped in."""
    if title and not _UNSAFE_TITLE.search(title) and len(title.encode("utf-8")) <= 96:
        return title
    try:
        fixed = safe_title(title)
    except Exception:  # never let a renderer bug break the catalog
        log.exception("safe_title failed for %r", title)
        fixed = _UNSAFE_TITLE.sub(" ", title).strip()
    return fixed or "Untitled"
