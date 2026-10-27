"""What the X3 receives: "Get N new" bundles, the plugin catalog pages and zero-tap
auto-deliveries.

Firmware facts this module is built around (CrossPoint 2.x, see docs/ARCHITECTURE.md §7.2):

- ``sleep.enter`` downloads are capped at 1 MiB, never follow redirects, and treat any
  2xx (even an empty body) as success; retries reuse the same ``(event.id, event.ts)``.
  So a repeat of a known ``(ev, ts)`` must get byte-identical output, and "nothing new"
  must be a 404 (``build_auto_delivery`` returns ``None``).
- The catalog sends ``limit = page_size + 1`` and uses the extra row to detect a next
  page, so the offset is ``(page - 1) * (limit - 1)``.
- Catalog downloads resume with Range requests after a dropped connection, so a "Get N
  new" bundle is built once, stored, and served byte-identical until it is replaced.

Each item's :class:`~sendtox3.textmodel.Document` is kept as ``doc.json`` next to its
rendered files so several items can be re-rendered into one digest EPUB. Items without
``doc.json`` (an uploaded EPUB kept as-is) are always delivered on their own.

A pending item can be left out of zero-tap deliveries without being marked delivered
(:meth:`Store.set_auto_skip`): one too large for the device got its "too large" note
once, and one whose stored EPUB is missing or empty is flagged failed. Both stay pending
for LAN push and show as "New" in the catalog.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

from .models import Item
from .store import (DIR_MODE, FETCH_NAMES_PREFIX, FETCH_RETIRED_PREFIX, FETCH_SENT_PREFIX, FETCH_TITLE_PREFIX,
                    Store, title_key, write_file)
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
        waiting = awaiting_confirmation(store)
        pending = [it for it in pending if it.id not in skipped and it.id not in waiting]
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


def _assemble(store: Store, pending: list[Item], *,
              single_max: Optional[int] = SINGLE_MAX_BYTES) -> tuple[list[Item], bytes | None, bool]:
    """``(items, payload, note_only)``: ``note_only`` when the payload is the "too large"
    note for ``items[0]`` rather than the item itself (never when ``single_max`` is None)."""
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
        return _single_payload(store, first, first_size, single_max)

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
    return _single_payload(store, first, first_size, single_max)


def _single_payload(store: Store, item: Item, size: int,
                    single_max: Optional[int] = SINGLE_MAX_BYTES) -> tuple[list[Item], bytes | None, bool]:
    if single_max is not None and size > single_max:
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
# "Get N new": everything new in one download (the catalog's first row, OPDS first entry)
# --------------------------------------------------------------------------------------

#: Id of the "Get N new" row: "all-" plus 10 base32 characters derived from the new
#: items, so the same new items always give the same id, title and stored bytes.
FETCH_ID_RE = r"all-[a-z2-7]{10}"
#: Id of the row shown when nothing is new (or the bundle failed); downloading it is a 404.
NOTHING_NEW_ID = "none"
NOTHING_NEW_TITLE = "Nothing new right now"
NOTHING_NEW_SUBTITLE = "Share something to Send to X3, then open this list again."
FETCH_FAILED_TITLE = "New reading could not be prepared"
FETCH_FAILED_SUBTITLE = "See the Send to X3 log in Home Assistant. The items below still download one by one."
#: ``deliveries.ev`` of stored bundles is ``fetch-<fmt>``; ``ts`` is the id's key part.
FETCH_EV_PREFIX = "fetch-"
FETCH_FORMATS = ("epub", "txt")
_FETCH_KEY_VERSION = "2"
#: A bundle the X3 fully received waits this long for its report (``book.downloaded``,
#: sent at the X3's next Wi-Fi join) before its items count as new again; they then come
#: under a new file name, so a copy already on the X3 is never replaced.
CONFIRM_GRACE_SECONDS = 2 * 3600


@dataclasses.dataclass
class FetchBundle:
    id: str
    title: str  # the row's title; the X3 saves the download as "<title>.<fmt>"
    subtitle: str
    item_ids: list[str]
    data: bytes
    fmt: str = "epub"

    @property
    def filename(self) -> str:
        return f"{self.title}.{self.fmt}"


def _fetch_key(pending: list[Item], retry: int = 0) -> str:
    """The same new items (ids and titles) give the same key, in every format; ``retry``
    gives the same items a new key (and so a new title) once they were sent whole."""
    h = hashlib.sha256(_FETCH_KEY_VERSION.encode())
    for it in pending:
        h.update(f"|{it.id}:{it.title}".encode())
    if retry:
        h.update(f"|retry:{retry}".encode())
    return base64.b32encode(h.digest()).decode("ascii").lower()[:10]


def fetch_title(items: list[Item]) -> str:
    """``Get 3 new · Oct 26 14.05``: the count and the time of the newest share (no ":",
    which the firmware turns into "_" in file names). See :func:`_unique_fetch_title`."""
    t = time.localtime(max(it.created_at for it in items))
    return f"Get {len(items)} new · {time.strftime('%b', t)} {t.tm_mday} {t.tm_hour:02d}.{t.tm_min:02d}"


def _unique_fetch_title(store: Store, base: str) -> str:
    """``base``, or ``base (2)``, ``(3)``, ...: never the name of a file the X3 already got
    from the catalog or OPDS (a later bundle would replace that file and reset its reading
    position), nor the title of an item, which may still be downloaded under that name."""
    taken = {title_key(t) for t in store.issued_titles() | store.titles_in_use()}
    # Titles of other bundles still listed or stored (in either format) are taken too.
    taken |= {title_key(t) for t in store.kv_prefixed(FETCH_TITLE_PREFIX).values()}
    title, n = base, 2
    while title_key(title) in taken:
        title, n = f"{base} ({n})", n + 1
    return title


def fetch_subtitle(items: list[Item], fmt: str) -> str:
    words = sum(it.word_count for it in items)
    head = ("Everything new in one book" if fmt == "epub" else "Everything new in one file") + (
        f" · {words:,} words" if words else "")
    names = "; ".join(device_title(it.title) for it in reversed(items))
    text = f"{head}: {names}"
    return text if len(text) <= 160 else text[:159].rstrip() + "…"


def _fetch_pending(store: Store) -> list[Item]:
    skipped = store.auto_skipped()
    waiting = awaiting_confirmation(store)
    return [it for it in store.pending_items() if skipped.get(it.id) != "damaged" and it.id not in waiting]


def build_fetch_bundle(store: Store, fmt: str = "epub") -> Optional[FetchBundle]:
    """The "Get N new" download for the items never delivered on any channel, or None.

    Oldest first, like the zero-tap delivery: several items become one digest of at most
    :data:`AUTO_CAP_BYTES`; an item that does not fit goes alone, at any size (catalog
    downloads have no 1 MiB limit), and anything left over is in the next bundle. The
    bytes and the title are stored under the id, so a resumed download gets the same
    bytes, and an earlier bundle of this format that was never fetched is dropped first.
    Nothing is marked delivered here (see :func:`fetch_sent`).

    Raises what storing or rendering raises; :func:`fetch_entry` and the OPDS feed fall
    back to listing the items alone.
    """
    fmt = fmt if fmt in FETCH_FORMATS else "epub"
    with _delivery_lock:
        pending = _fetch_pending(store)
        if not pending:
            return None
        # Items of a bundle sent whole but never confirmed (past the grace period) get a
        # new key, so the row has a new title and cannot replace the copy on the X3.
        sent_whole = {str(r.get("key")) for r in _sent_records(store) if r.get("complete")}
        # Retired ids never resolve again: after a Requeue the same items get a new one.
        avoid = sent_whole | {k[len(FETCH_RETIRED_PREFIX):] for k in store.kv_prefixed(FETCH_RETIRED_PREFIX)}
        retry = 0
        key = _fetch_key(pending)
        while key in avoid:
            retry += 1
            key = _fetch_key(pending, retry)
        return _bundle(store, key, fmt, pending)


def _bundle(store: Store, key: str, fmt: str, pending: Optional[list[Item]] = None) -> Optional[FetchBundle]:
    """Bundle ``key`` in ``fmt``: stored; else built from the items of the same bundle in
    another format (the same set, whatever its size); else, with ``pending``, a new
    bundle. None when ``key`` is unknown and there is no ``pending``."""
    ev = FETCH_EV_PREFIX + fmt
    stored = store.get_delivery(ev, key)
    if stored is not None:
        ids, data = stored
        batch = [it for it in (store.get(i) for i in ids) if it is not None]
    else:
        same = next((got for got in (store.get_delivery(FETCH_EV_PREFIX + f, key) for f in FETCH_FORMATS if f != fmt)
                     if got is not None), None)
        if same is not None:
            source, cap = [it for it in (store.get(i) for i in same[0]) if it is not None], False
        elif pending is not None:
            source, cap = pending, True
        else:
            return None
        batch, data = _render_bundle(store, source, fmt, cap=cap)
        if not batch or not data:
            return None
        _forget(store, store.delete_deliveries(ev, keep_ts=key))  # free its space before saving
        store.save_delivery(ev, key, [it.id for it in batch], data)
        log.info("fetch bundle all-%s: %d item(s), %d bytes (%s)", key, len(batch), len(data), fmt)
    if not batch:
        return None
    title = store.kv_get(FETCH_TITLE_PREFIX + key)
    if title and pending is not None and title_key(title) in {
            title_key(t) for t in store.issued_titles() | store.titles_in_use()}:
        # Listing a bundle whose name the X3 may already have (sent before: cancelled, or
        # seen in the other format) or an item now has: offer it under a new name, so a
        # press can never replace that file. A download keeps the name the X3 was shown.
        title = None
    if not title:
        title = _unique_fetch_title(store, fetch_title(batch))
        store.kv_set(FETCH_TITLE_PREFIX + key, title)
        names = _names(store, key)
        if title not in names:
            store.kv_set(FETCH_NAMES_PREFIX + key, json.dumps(names + [title]))
    return FetchBundle(id=f"all-{key}", title=title, subtitle=fetch_subtitle(batch, fmt),
                       item_ids=[it.id for it in batch], data=data, fmt=fmt)


def _render_bundle(store: Store, items: list[Item], fmt: str, *, cap: bool) -> tuple[list[Item], bytes | None]:
    if fmt == "txt":
        return _assemble_txt(store, items, cap=cap)
    if cap:
        batch, data, _ = _assemble(store, items, single_max=None)
        return batch, data
    if len(items) == 1:
        batch, data, _ = _single_payload(store, items[0], 0, None)
        return batch, data
    docs = [(it, _digest_document(store, it)) for it in items]
    if any(doc is None for _, doc in docs):
        return [], None  # a passthrough EPUB never shares a book; the EPUB listing never made one
    return items, _render_digest(docs)


def _names(store: Store, key: str) -> list[str]:
    """Every title bundle ``key`` was listed under: a client can still show an older one."""
    try:
        names = json.loads(store.kv_get(FETCH_NAMES_PREFIX + key) or "[]")
    except ValueError:
        return []
    return [str(n) for n in names] if isinstance(names, list) else []


def _forget(store: Store, keys: list[str]) -> None:
    """Drop the titles of removed bundles that no format still stores."""
    gone = [p + k for k in keys if not store.has_delivery(FETCH_EV_PREFIX + "*", k)
            for p in (FETCH_TITLE_PREFIX, FETCH_NAMES_PREFIX)]
    if gone:
        store.kv_delete(*gone)


def _assemble_txt(store: Store, pending: list[Item], *, cap: bool = True) -> tuple[list[Item], bytes | None]:
    """Item TXT files joined by a separator, oldest first, up to :data:`AUTO_CAP_BYTES`
    with ``cap`` (the first item always goes in)."""
    parts: list[bytes] = []
    batch: list[Item] = []
    total = 0
    sep = "\n\n* * *\n\n".encode("utf-8")
    for it in pending:
        # An uploaded EPUB (no doc.json) goes alone, as in the EPUB format, so that every
        # bundle can also be built as EPUB from exactly its items.
        alone = cap and _read_doc_json(store, it.id) is None
        if alone and batch:
            break
        try:
            data = store.file_bytes(it.id, "txt")
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("item %s: TXT not readable (%s); skipped this time", it.id, exc)
            continue
        data = data.strip()
        if not data:
            continue
        if cap and batch and total + len(sep) + len(data) > AUTO_CAP_BYTES:
            break
        total += len(data) + (len(sep) if batch else 0)
        parts.append(data)
        batch.append(it)
        if alone:
            break
    if not batch:
        return [], None
    return batch, sep.join(parts) + b"\n"


def fetch_lookup(store: Store, bundle_id: str, fmt: str = "epub") -> Optional[FetchBundle]:
    """The listed "Get N new" bundle for a download, in ``fmt`` (built from the same items
    if it was listed in another format), or None: ``none``, an unknown id, or a bundle
    replaced since. Marks nothing."""
    if not re.fullmatch(FETCH_ID_RE, bundle_id or "") or fmt not in FETCH_FORMATS:
        return None
    with _delivery_lock:
        return _bundle(store, bundle_id[4:], fmt)


def fetch_sent(store: Store, bundle: FetchBundle, *, complete: bool = True, confirm: bool = False,
               channel: str = "fetch") -> None:
    """After a bundle download: ``complete`` when the X3 received every byte.

    Its file name is reserved either way (the X3 may have saved it). Without ``confirm``
    a complete download marks the items delivered. With ``confirm`` (a plugin that
    reports saved files, :func:`fetch_confirmed`) it is only recorded: the items then
    wait for the report (:func:`awaiting_confirmation`); after an incomplete download
    they are offered again under the same name (the X3 kept no copy).
    """
    now = int(time.time())
    with _delivery_lock:
        store.issue_title(bundle.title, now)
        if confirm:
            # The X3 saves under the title it was shown, which may be an earlier name of
            # this bundle (another client's listing can rename it), so record every name.
            key = bundle.id[4:]
            for name in dict.fromkeys(_names(store, key) + [bundle.title]):
                k = FETCH_SENT_PREFIX + title_key(f"{name}.{bundle.fmt}")
                try:
                    prev = json.loads(store.kv_get(k) or "{}")
                    was_complete = bool(prev.get("complete")) and prev.get("key") == key
                except (ValueError, AttributeError):
                    was_complete = False
                store.kv_set(k, json.dumps({"ids": bundle.item_ids, "at": now, "title": name,
                                            "key": key, "complete": complete or was_complete}))
            return
        if complete:
            for i in bundle.item_ids:
                store.mark_delivered(i, channel, now)
            _retire(store, bundle.id[4:])


def _retire(store: Store, key: str) -> None:
    """A bundle whose items now count as delivered: drop its stored copies and its title,
    so the same items (after a Requeue) come again under a new name, never replacing the
    file on the X3."""
    for f in FETCH_FORMATS:
        store.delete_delivery(FETCH_EV_PREFIX + f, key)
    store.kv_delete(FETCH_TITLE_PREFIX + key, FETCH_NAMES_PREFIX + key)
    store.kv_set(FETCH_RETIRED_PREFIX + key, str(int(time.time())))


def requeue(store: Store, item_id: str) -> None:
    """Admin "Requeue": the item counts as new again, also for "Get N new": it leaves every
    sent-but-unconfirmed record, so it is not hidden as awaiting a report and a late
    report cannot mark it delivered again."""
    with _delivery_lock:
        store.requeue(item_id)
        for rec in _sent_records(store):
            if item_id not in rec["ids"]:
                continue
            if rec.get("key"):
                _retire(store, str(rec["key"]))
            rest = [i for i in rec["ids"] if i != item_id]
            if rest:
                fields = {k: v for k, v in rec.items() if k != "_k"}
                store.kv_set(rec["_k"], json.dumps({**fields, "ids": rest}))
            else:
                store.kv_delete(rec["_k"])


def _sent_records(store: Store) -> list[dict]:
    out = []
    for k, raw in store.kv_prefixed(FETCH_SENT_PREFIX).items():
        try:
            rec = json.loads(raw)
            rec["ids"] = [str(i) for i in rec["ids"]]
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        rec["_k"] = k
        out.append(rec)
    return out


def _still_new(store: Store, ids: list[str]) -> list[str]:
    return [i for i in ids if (it := store.get(i)) is not None and it.is_pending]


def awaiting_confirmation(store: Store, now: Optional[int] = None) -> set[str]:
    """Items of bundles the X3 received whole in the last :data:`CONFIRM_GRACE_SECONDS`
    and has not confirmed yet. They are left out of new bundles, LAN push and sleep
    delivery, so the X3 does not get them twice while its report is on the way."""
    now = int(now or time.time())
    out: set[str] = set()
    for rec in _sent_records(store):
        if rec.get("complete") and now - int(rec.get("at") or 0) < CONFIRM_GRACE_SECONDS:
            out.update(_still_new(store, rec["ids"]))
    return out


def fetch_confirmed(store: Store, path: str) -> list[str]:
    """The X3 saved ``path`` (its ``book.downloaded`` event): mark the items of the bundle
    sent under that name as delivered. Returns their ids; any other path changes nothing."""
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name:
        return []
    k = FETCH_SENT_PREFIX + title_key(name)
    with _delivery_lock:
        raw = store.kv_get(k)
        if raw is None:
            return []
        try:
            rec = json.loads(raw)
            ids = [str(i) for i in rec["ids"]]
        except (ValueError, TypeError, KeyError):
            rec, ids = {}, []
        now = int(time.time())
        for i in ids:
            store.mark_delivered(i, "fetch", now)
        if rec.get("key"):
            _retire(store, str(rec["key"]))
        # This record, and any other one (an earlier name of the same items) now settled.
        done = [r for r in _sent_records(store) if not _still_new(store, r["ids"])]
        for r in done:
            if r.get("key"):
                _retire(store, str(r["key"]))
        store.kv_delete(k, *(r["_k"] for r in done))
    log.info("the X3 confirmed %s (%d item(s))", name, len(ids))
    return ids


def unconfirmed_fetches(store: Store) -> dict[str, int]:
    """``{item_id: sent_at}`` for items the X3 received in a bundle but has not confirmed
    yet (it does so the next time it joins Wi-Fi)."""
    out: dict[str, int] = {}
    for rec in _sent_records(store):
        if rec.get("complete"):
            for i in _still_new(store, rec["ids"]):
                out[i] = max(out.get(i, 0), int(rec.get("at") or 0))
    return out


def fetch_download(store: Store, bundle_id: str, fmt: str = "epub", *, confirm: bool = False,
                   complete: bool = True, channel: str = "fetch") -> Optional[bytes]:
    """:func:`fetch_lookup` then :func:`fetch_sent`, as if the X3 downloaded it (tests)."""
    bundle = fetch_lookup(store, bundle_id, fmt)
    if bundle is None:
        return None
    fetch_sent(store, bundle, complete=complete, confirm=confirm, channel=channel)
    return bundle.data


def fetch_entry(store: Store, fmt: str = "epub") -> dict[str, str]:
    """The catalog's first row: "Get N new", "Nothing new right now", or (when the bundle
    cannot be built, for example on a full disk) a row saying so; the item rows that
    follow still load."""
    try:
        bundle = build_fetch_bundle(store, fmt)
    except Exception:  # noqa: BLE001 - never let the bundle break the whole catalog
        log.exception("could not prepare the \"Get N new\" download")
        return {"id": NOTHING_NEW_ID, "title": FETCH_FAILED_TITLE, "subtitle": FETCH_FAILED_SUBTITLE}
    if bundle is None:
        waiting = unconfirmed_fetches(store)
        if waiting:
            t = time.localtime(max(waiting.values()))
            sub = (f"Your last fetch ({time.strftime('%b', t)} {t.tm_mday} {t.tm_hour:02d}.{t.tm_min:02d}) is "
                   "confirmed the next time the X3 joins Wi-Fi.")
            return {"id": NOTHING_NEW_ID, "title": NOTHING_NEW_TITLE, "subtitle": sub}
        return {"id": NOTHING_NEW_ID, "title": NOTHING_NEW_TITLE, "subtitle": NOTHING_NEW_SUBTITLE}
    return {"id": bundle.id, "title": bundle.title, "subtitle": bundle.subtitle}


# --------------------------------------------------------------------------------------
# Catalog (plugin browse)
# --------------------------------------------------------------------------------------


def clamp_paging(page: int, limit: int) -> tuple[int, int]:
    """Clamp to the values the firmware can send: page >= 1, limit 2..17."""
    return max(1, int(page)), min(LIMIT_MAX, max(LIMIT_MIN, int(limit)))


def catalog_page(store: Store, page: int, limit: int, *, fmt: str = "epub") -> dict:
    """``{"items": [{"id", "title", "subtitle"}]}``: the :func:`fetch_entry` row first,
    then every item, newest first.

    Returns up to ``limit`` rows of that list starting at ``(page - 1) * (limit - 1)``.
    """
    page, limit = clamp_paging(page, limit)
    offset = (page - 1) * (limit - 1)
    if offset == 0:
        rows = [fetch_entry(store, fmt)]
        rows += [catalog_entry(it) for it in store.list_items(offset=0, limit=limit - 1, newest_first=True)]
        return {"items": rows}
    items = store.list_items(offset=offset - 1, limit=limit, newest_first=True)
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
