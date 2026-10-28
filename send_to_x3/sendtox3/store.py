"""Persistent queue: SQLite metadata + rendered files under the data dir.

Layout::

    <data>/sendtox3.db            items, deliveries, kv
    <data>/items/<id>/item.epub   always present
    <data>/items/<id>/item.txt    always present
    <data>/deliveries/<n>.epub    stored auto-delivery payloads (byte-identical replays)
    <data>/spool/<id>.bin         file bytes of a share still being processed

Everything is owner-only (folders 0700, files 0600): the database holds the tokens.

All methods are synchronous and thread-safe (one connection guarded by a lock). They
are fast (single-row SQL, small files), so async callers may call them directly;
:meth:`Store.spool_add` with a large file payload is the exception (call it in a thread).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .models import CHANNELS, IngestRequest, Item

log = logging.getLogger(__name__)

#: Channels whose downloads are saved on the X3 as ``<title>.<ext>`` (LAN push into
#: ``push_folder``, by default the catalog's ``/Inbox``). Their titles stay reserved for
#: good (``issued_titles``), even after the item is deleted or pruned: the file stays on
#: the X3, so a later item with the same title would replace it (and its reading
#: progress) on stock firmware, and on the Send to X3 firmware (``download.open``) its
#: catalog row would open the old file instead of downloading the new one. Nothing
#: tells the app when the file leaves the card, so a reservation never expires.
TITLE_NAMED_CHANNELS = ("catalog", "opds", "push")
#: Permissions of everything in the data folder: it holds the generated tokens.
DIR_MODE = 0o700
FILE_MODE = 0o600
_AUTO_SKIP_PREFIX = "auto_skip:"
#: kv rows of "Get N new" bundles (delivery.py): ``fetch-title:<key>`` is a bundle's
#: title; ``fetch-sent:<file name key>`` the items of a bundle sent to an X3 that
#: confirms the saved file later (``{"ids": [...], "at": unix}``).
FETCH_TITLE_PREFIX = "fetch-title:"
FETCH_SENT_PREFIX = "fetch-sent:"
#: ``fetch-retired:<key>`` = unix time: a bundle id that must never resolve again.
FETCH_RETIRED_PREFIX = "fetch-retired:"
#: ``fetch-names:<key>`` = every title the bundle was listed under (a JSON list).
FETCH_NAMES_PREFIX = "fetch-names:"
#: One-time migrations of ``issued_titles``: kv key -> the channels whose earlier
#: downloads it reserves (LAN push joined :data:`TITLE_NAMED_CHANNELS` in 0.3.0).
_ISSUED_BACKFILLS = (("migration:issued_titles_backfill", ("catalog", "opds")),
                     ("migration:issued_titles_backfill_push", ("push",)))
_STALE_TMP_SECONDS = 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    byline TEXT NOT NULL DEFAULT '',
    site TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ready',
    error TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    dedupe_key TEXT NOT NULL DEFAULT '',
    delivered TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS items_created ON items(created_at);
CREATE INDEX IF NOT EXISTS items_dedupe ON items(dedupe_key);
CREATE TABLE IF NOT EXISTS deliveries (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    ev TEXT NOT NULL,
    ts TEXT NOT NULL,
    item_ids TEXT NOT NULL,
    file TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    UNIQUE(ev, ts)
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_requests (
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS issued_titles (
    key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    issued_at INTEGER NOT NULL
);
"""

_ITEM_COLS = (
    "id",
    "created_at",
    "kind",
    "source",
    "title",
    "byline",
    "site",
    "source_url",
    "status",
    "error",
    "word_count",
    "dedupe_key",
    "delivered",
)


def _row_to_item(row: sqlite3.Row) -> Item:
    d = dict(row)
    d["delivered"] = json.loads(d.get("delivered") or "{}")
    return Item(**d)


def _title_key(title: str) -> str:
    """FAT compares long file names case-insensitively."""
    return unicodedata.normalize("NFC", title).casefold()


title_key = _title_key


def write_file(path: Path, data: bytes) -> None:
    """Write ``path`` atomically and durably (tmp file, fsync, rename, fsync of the
    folder): after a power cut the file is either absent or complete, never 0 bytes
    next to a database row that points at it. The file is owner-only (0600), and two
    writers of one path (identical uploads spooled at once) do not share a tmp file."""
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - some file systems refuse fsync on folders
        pass
    finally:
        os.close(fd)


def _request_fields(req: IngestRequest) -> dict:
    return {f.name: getattr(req, f.name) for f in dataclasses.fields(req) if f.name != "data"}


def spool_id_for(req: IngestRequest) -> str:
    """Content id of a request: a retried or double-tapped identical share reuses one
    spool entry (the receive time does not count)."""
    fields = _request_fields(req)
    fields.pop("received_at", None)
    h = hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if req.data is not None:
        h.update(b"\0data\0")
        h.update(req.data)
    return h.hexdigest()[:32]


@dataclass
class SpooledRequest:
    """A request written to the spool before processing and not yet removed."""

    id: str
    request: IngestRequest
    created_at: int
    attempts: int  # processing attempts so far, the first one included
    data_lost: bool = False  # it had a file whose bytes are missing or truncated


def _owner_only(path: Path, mode: int) -> None:
    """chmod ``path`` to ``mode``; best effort (a folder we do not own, a file system
    without permissions)."""
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.debug("could not chmod %s to %o: %s", path, mode, exc)


class Store:
    """The data folder and everything in it are private to the account the app runs
    as (folders 0700, files 0600): the database holds the generated tokens, and the
    PDF worker runs as an unprivileged account that must not read any of it. Files
    are created owner-only (:func:`write_file`), so this holds whatever the umask."""

    def __init__(self, data_dir: str | os.PathLike):
        self.dir = Path(data_dir)
        self.items_dir = self.dir / "items"
        self.deliveries_dir = self.dir / "deliveries"
        self.spool_dir = self.dir / "spool"
        self.dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        _owner_only(self.dir, DIR_MODE)
        for d in (self.items_dir, self.deliveries_dir, self.spool_dir):
            d.mkdir(mode=DIR_MODE, exist_ok=True)
            _owner_only(d, DIR_MODE)
        db_path = self.dir / "sendtox3.db"
        try:  # create it owner-only; SQLite gives its -wal and -shm files the same mode
            os.close(os.open(db_path, os.O_RDWR | os.O_CREAT, FILE_MODE))
        except OSError as exc:
            log.debug("could not pre-create %s: %s", db_path, exc)
        _owner_only(db_path, FILE_MODE)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        for suffix in ("-wal", "-shm"):  # made by an older version, or with a looser mode
            _owner_only(db_path.with_name(db_path.name + suffix), FILE_MODE)
        try:
            self._backfill_issued_titles()
        except sqlite3.Error:  # retried at the next start; the queue itself still works
            log.exception("could not reserve the titles of earlier downloads")
        self._remove_stale_tmp_files()

    def _remove_stale_tmp_files(self) -> None:
        """Tmp files a crash or power cut left in the spool and deliveries folders
        (:func:`write_file` names them uniquely, so nothing else replaces them). Only
        old ones: a ``--show-tokens`` run may open the store while the server writes."""
        cutoff = time.time() - _STALE_TMP_SECONDS
        for folder in (self.spool_dir, self.deliveries_dir):
            try:
                for p in folder.glob("*.tmp"):
                    with contextlib.suppress(OSError):
                        if p.stat().st_mtime < cutoff:
                            p.unlink()
            except OSError:
                pass

    def _backfill_issued_titles(self) -> None:
        """One-time migrations: items downloaded through a channel of
        :data:`TITLE_NAMED_CHANNELS` before it reserved titles reserve them too (their
        files are on the X3). An item pushed under a numbered name (``Title (2).epub``)
        reserves only its title: the database never recorded the name on the card."""
        for done_key, wanted in _ISSUED_BACKFILLS:
            if self.kv_get(done_key):
                continue
            with self._lock:
                rows = self._db.execute("SELECT title, delivered FROM items WHERE delivered!='{}'").fetchall()
                found: dict[str, tuple[str, int]] = {}
                for title, delivered in rows:
                    try:
                        channels = json.loads(delivered or "{}")
                        when = max(int(channels[c]) for c in wanted if c in channels)
                    except (ValueError, TypeError, AttributeError):  # max() of nothing, bad JSON
                        continue
                    key = _title_key(title)
                    if when > found.get(key, ("", -1))[1]:
                        found[key] = (title, when)
                self._db.execute("BEGIN")
                try:
                    self._db.executemany(
                        "INSERT OR IGNORE INTO issued_titles (key, title, issued_at) VALUES (?, ?, ?)",
                        [(k, t, w) for k, (t, w) in found.items()],
                    )
                    self._db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (done_key, "1"))
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            if found:
                log.info("reserved %d title(s) of items downloaded (%s) before this version",
                         len(found), ", ".join(wanted))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ items
    def item_dir(self, item_id: str) -> Path:
        return self.items_dir / item_id

    def add_item(self, item: Item, *, epub: bytes, txt: bytes) -> Item:
        """Write files first (atomically and durably), then the row, so a row always has
        its files."""
        d = self.item_dir(item.id)
        d.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        for name, data in (("item.epub", epub), ("item.txt", txt)):
            write_file(d / name, data)
        values = [getattr(item, c) if c != "delivered" else json.dumps(item.delivered) for c in _ITEM_COLS]
        with self._lock:
            self._db.execute(
                f"INSERT INTO items ({','.join(_ITEM_COLS)}) VALUES ({','.join('?' * len(_ITEM_COLS))})",
                values,
            )
        return item

    def get(self, item_id: str) -> Optional[Item]:
        with self._lock:
            row = self._db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def list_items(self, *, offset: int = 0, limit: int = 50, newest_first: bool = True) -> list[Item]:
        order = "DESC" if newest_first else "ASC"
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM items ORDER BY created_at {order}, rowid {order} LIMIT ? OFFSET ?",
                (max(0, limit), max(0, offset)),
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM items").fetchone()[0])

    def pending_items(self) -> list[Item]:
        """Items never delivered through any channel, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM items WHERE delivered='{}' ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def file_path(self, item_id: str, fmt: str) -> Path:
        if fmt not in ("epub", "txt"):
            raise ValueError(fmt)
        return self.item_dir(item_id) / f"item.{fmt}"

    def file_bytes(self, item_id: str, fmt: str) -> bytes:
        return self.file_path(item_id, fmt).read_bytes()

    def mark_delivered(self, item_id: str, channel: str, when: Optional[int] = None) -> None:
        if channel not in CHANNELS:
            raise ValueError(channel)
        when = int(when or time.time())
        with self._lock:
            row = self._db.execute("SELECT delivered, title FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                return
            delivered = json.loads(row[0] or "{}")
            delivered.setdefault(channel, when)
            self._db.execute("UPDATE items SET delivered=? WHERE id=?", (json.dumps(delivered), item_id))
            if channel in TITLE_NAMED_CHANNELS:
                title = row[1]
                self._db.execute(
                    "INSERT OR REPLACE INTO issued_titles (key, title, issued_at) VALUES (?, ?, ?)",
                    (_title_key(title), title, when),
                )

    def mark_failed(self, item_id: str, error: str) -> None:
        """Flag an item whose stored files turned out to be unusable."""
        with self._lock:
            self._db.execute("UPDATE items SET status='failed', error=? WHERE id=?", (error, item_id))

    def requeue(self, item_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE items SET delivered='{}' WHERE id=?", (item_id,))
            self._db.execute("DELETE FROM kv WHERE k=?", (_AUTO_SKIP_PREFIX + item_id,))

    def update_title(self, item_id: str, title: str) -> None:
        with self._lock:
            self._db.execute("UPDATE items SET title=? WHERE id=?", (title, item_id))

    def delete(self, item_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM items WHERE id=?", (item_id,))
            self._db.execute("DELETE FROM kv WHERE k=?", (_AUTO_SKIP_PREFIX + item_id,))
        shutil.rmtree(self.item_dir(item_id), ignore_errors=True)
        return cur.rowcount > 0

    # ------------------------------------------------------- auto-delivery skips
    def set_auto_skip(self, item_id: str, reason: str) -> None:
        """Leave a pending item out of zero-tap deliveries (it stays pending for the
        other channels): its "too large" note was already sent, or its files are damaged."""
        self.kv_set(_AUTO_SKIP_PREFIX + item_id, reason)

    def auto_skipped(self) -> dict[str, str]:
        """``{item_id: reason}`` for items left out of zero-tap deliveries."""
        with self._lock:
            rows = self._db.execute("SELECT k, v FROM kv WHERE k GLOB ?", (_AUTO_SKIP_PREFIX + "*",)).fetchall()
        return {k[len(_AUTO_SKIP_PREFIX):]: v for k, v in rows}

    def find_duplicate(self, dedupe_key: str, within_seconds: int = 86400) -> Optional[Item]:
        if not dedupe_key:
            return None
        cutoff = int(time.time()) - within_seconds
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM items WHERE dedupe_key=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                (dedupe_key, cutoff),
            ).fetchone()
        return _row_to_item(row) if row else None

    def titles_in_use(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT title FROM items").fetchall()}

    def issued_titles(self) -> set[str]:
        """Names of files saved on the X3 as ``<title>.<ext>`` (catalog, OPDS, LAN push,
        "Get N new" bundles), including those of items deleted or pruned since: a new
        item must not reuse them (see :data:`TITLE_NAMED_CHANNELS`). Never expire."""
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT title FROM issued_titles").fetchall()}

    def prune(self, retention_days: int, now: Optional[int] = None) -> int:
        """Delete items and stored deliveries older than ``retention_days``. Reserved
        titles stay (:meth:`issued_titles`): their files may still be on the X3."""
        now = int(now or time.time())
        cutoff = now - retention_days * 86400
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM items WHERE created_at<?", (cutoff,))]
            self._db.execute("DELETE FROM items WHERE created_at<?", (cutoff,))
            self._db.executemany("DELETE FROM kv WHERE k=?", [(_AUTO_SKIP_PREFIX + i,) for i in ids])
            old = self._db.execute("SELECT n, file FROM deliveries WHERE created_at<?", (cutoff,)).fetchall()
            self._db.execute("DELETE FROM deliveries WHERE created_at<?", (cutoff,))
            # Titles of bundles no longer stored, and sent bundles never confirmed.
            for prefix in (FETCH_TITLE_PREFIX, FETCH_NAMES_PREFIX):
                self._db.execute(
                    "DELETE FROM kv WHERE k GLOB ? AND substr(k, ?) NOT IN "
                    "(SELECT ts FROM deliveries WHERE ev GLOB 'fetch-*')",
                    (prefix + "*", len(prefix) + 1))
            for k, v in self._db.execute("SELECT k, v FROM kv WHERE k GLOB ?", (FETCH_SENT_PREFIX + "*",)).fetchall():
                try:
                    at = int(json.loads(v).get("at") or 0)
                except (ValueError, TypeError, AttributeError):
                    at = 0
                if at < cutoff:
                    self._db.execute("DELETE FROM kv WHERE k=?", (k,))
            for k, v in self._db.execute("SELECT k, v FROM kv WHERE k GLOB ?",
                                         (FETCH_RETIRED_PREFIX + "*",)).fetchall():
                try:
                    expired = int(v) < cutoff
                except ValueError:
                    expired = True
                if expired:
                    self._db.execute("DELETE FROM kv WHERE k=?", (k,))
        for i in ids:
            shutil.rmtree(self.item_dir(i), ignore_errors=True)
        for _, f in old:
            try:
                (self.deliveries_dir / f).unlink()
            except FileNotFoundError:
                pass
        return len(ids)

    # ------------------------------------------------------------- deliveries
    def get_delivery(self, ev: str, ts: str) -> Optional[tuple[list[str], bytes]]:
        """A stored delivery (a device event's, or a "Get N new" bundle), for
        byte-identical replays."""
        with self._lock:
            row = self._db.execute("SELECT n, item_ids, file FROM deliveries WHERE ev=? AND ts=?", (ev, ts)).fetchone()
            if not row:
                return None
            self._db.execute("UPDATE deliveries SET hits=hits+1 WHERE n=?", (row["n"],))
        try:
            data = (self.deliveries_dir / row["file"]).read_bytes()
        except FileNotFoundError:
            return None
        return json.loads(row["item_ids"]), data

    def save_delivery(self, ev: str, ts: str, item_ids: Iterable[str], data: bytes) -> None:
        ids = list(item_ids)
        name = f"{int(time.time())}-{secrets.token_hex(4)}.epub"
        write_file(self.deliveries_dir / name, data)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO deliveries (ev, ts, item_ids, file, created_at) VALUES (?,?,?,?,?)",
                (ev, ts, json.dumps(ids), name, int(time.time())),
            )

    def delete_deliveries(self, ev: str, *, keep_ts: str = "") -> list[str]:
        """Drop the stored deliveries of ``ev`` except ``keep_ts`` (replaced "Get N new"
        bundles). Returns the ``ts`` of those removed."""
        with self._lock:
            old = self._db.execute("SELECT n, ts, file FROM deliveries WHERE ev=? AND ts<>?",
                                   (ev, keep_ts)).fetchall()
            self._db.executemany("DELETE FROM deliveries WHERE n=?", [(r["n"],) for r in old])
        for r in old:
            try:
                (self.deliveries_dir / r["file"]).unlink()
            except FileNotFoundError:
                pass
        return [r["ts"] for r in old]

    def delete_delivery(self, ev: str, ts: str) -> bool:
        """Drop one stored delivery (and its file); False when there was none."""
        with self._lock:
            row = self._db.execute("SELECT n, file FROM deliveries WHERE ev=? AND ts=?", (ev, ts)).fetchone()
            if row is None:
                return False
            self._db.execute("DELETE FROM deliveries WHERE n=?", (row["n"],))
        try:
            (self.deliveries_dir / row["file"]).unlink()
        except FileNotFoundError:
            pass
        return True

    def has_delivery(self, ev_glob: str, ts: str) -> bool:
        """Whether a stored delivery matches (``ev`` as a GLOB pattern), without counting a hit."""
        with self._lock:
            return self._db.execute("SELECT 1 FROM deliveries WHERE ev GLOB ? AND ts=? LIMIT 1",
                                    (ev_glob, ts)).fetchone() is not None

    def issue_title(self, title: str, when: Optional[int] = None) -> None:
        """Reserve ``title`` as the name (without extension) of a file now on the X3 (see
        :meth:`issued_titles`)."""
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO issued_titles (key, title, issued_at) VALUES (?, ?, ?)",
                             (_title_key(title), title, int(when or time.time())))

    def delivery_stats(self) -> list[dict]:
        """The latest zero-tap (``sleep.enter``) deliveries; "Get N new" bundles are left out."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ev, ts, item_ids, created_at, hits FROM deliveries WHERE ev NOT GLOB 'fetch-*' "
                "ORDER BY n DESC LIMIT 20"
            ).fetchall()
        return [dict(r) | {"item_ids": json.loads(r["item_ids"])} for r in rows]

    # ------------------------------------------------------------------ spool
    def spool_add(self, req: IngestRequest) -> str:
        """Persist a request before it is processed (file bytes as ``spool/<id>.bin``,
        then the row), so a share accepted before a crash or restart is not lost.
        Identical requests share one entry. Returns the spool id."""
        spool_id = spool_id_for(req)
        fields = _request_fields(req)
        fields["has_data"] = req.data is not None
        fields["data_len"] = len(req.data) if req.data is not None else 0
        if req.data is not None:
            path = self.spool_dir / f"{spool_id}.bin"
            try:
                fresh = path.stat().st_size != len(req.data)
            except FileNotFoundError:
                fresh = True
            if fresh:
                write_file(path, req.data)
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO pending_requests (id, created_at, json) VALUES (?, ?, ?)",
                (spool_id, int(time.time()), json.dumps(fields, ensure_ascii=False)),
            )
        return spool_id

    def spooled_requests(self) -> list[SpooledRequest]:
        """Every spooled request, oldest first. One whose file is missing or truncated
        comes back with ``data=None`` and ``data_lost`` set (its name and fields are kept,
        so the caller can record what was lost)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, json, created_at, attempts FROM pending_requests ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
        names = {f.name for f in dataclasses.fields(IngestRequest)} - {"data"}
        out: list[SpooledRequest] = []
        for spool_id, raw, created_at, attempts in rows:
            try:
                fields = json.loads(raw)
                req = IngestRequest(**{k: v for k, v in fields.items() if k in names})
            except (AttributeError, TypeError, ValueError) as exc:
                log.warning("spool entry %s is unreadable (%s); dropped", spool_id, exc)
                self.spool_remove(spool_id)
                continue
            lost = False
            if fields.get("has_data"):
                try:
                    data: Optional[bytes] = (self.spool_dir / f"{spool_id}.bin").read_bytes()
                except OSError:
                    data = None
                if data is not None and len(data) == int(fields.get("data_len") or 0):
                    req.data = data
                else:
                    log.warning("spool entry %s: its file is missing or truncated", spool_id)
                    lost = True
            out.append(SpooledRequest(spool_id, req, int(created_at), int(attempts), lost))
        return out

    def spool_attempt(self, spool_id: str) -> int:
        """Count one more processing attempt of a spooled request; returns the count."""
        with self._lock:
            self._db.execute("UPDATE pending_requests SET attempts=attempts+1 WHERE id=?", (spool_id,))
            row = self._db.execute("SELECT attempts FROM pending_requests WHERE id=?", (spool_id,)).fetchone()
        return int(row[0]) if row else 0

    def spool_remove(self, spool_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pending_requests WHERE id=?", (spool_id,))
        try:
            (self.spool_dir / f"{spool_id}.bin").unlink()
        except FileNotFoundError:
            pass

    def spool_count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM pending_requests").fetchone()[0])

    # --------------------------------------------------------------------- kv
    def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, value))

    def kv_delete(self, *keys: str) -> None:
        with self._lock:
            self._db.executemany("DELETE FROM kv WHERE k=?", [(k,) for k in keys])

    def kv_prefixed(self, prefix: str) -> dict[str, str]:
        """``{key: value}`` for every kv row whose key starts with ``prefix``."""
        with self._lock:
            rows = self._db.execute("SELECT k, v FROM kv WHERE substr(k, 1, ?) = ?", (len(prefix), prefix)).fetchall()
        return {k: v for k, v in rows}

    def secret(self, name: str, configured: str = "") -> str:
        """Configured value wins; otherwise a generated token persisted in kv."""
        if configured:
            return configured
        key = f"secret:{name}"
        val = self.kv_get(key)
        if not val:
            val = secrets.token_urlsafe(24)
            self.kv_set(key, val)
        return val
