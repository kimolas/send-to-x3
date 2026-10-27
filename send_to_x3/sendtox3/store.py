"""Persistent queue: SQLite metadata + rendered files under the data dir.

Layout::

    <data>/sendtox3.db            items, deliveries, kv
    <data>/items/<id>/item.epub   always present
    <data>/items/<id>/item.txt    always present
    <data>/deliveries/<n>.epub    stored auto-delivery payloads (byte-identical replays)

All methods are synchronous and thread-safe (one connection guarded by a lock). They
are fast (single-row SQL, small files), so async callers may call them directly.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from .models import CHANNELS, Item

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


class Store:
    def __init__(self, data_dir: str | os.PathLike):
        self.dir = Path(data_dir)
        self.items_dir = self.dir / "items"
        self.deliveries_dir = self.dir / "deliveries"
        self.items_dir.mkdir(parents=True, exist_ok=True)
        self.deliveries_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.dir / "sendtox3.db", check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ items
    def item_dir(self, item_id: str) -> Path:
        return self.items_dir / item_id

    def add_item(self, item: Item, *, epub: bytes, txt: bytes) -> Item:
        """Write files first (atomically), then the row, so a row always has its files."""
        d = self.item_dir(item.id)
        d.mkdir(parents=True, exist_ok=True)
        for name, data in (("item.epub", epub), ("item.txt", txt)):
            tmp = d / (name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, d / name)
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
            row = self._db.execute("SELECT delivered FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                return
            delivered = json.loads(row[0] or "{}")
            delivered.setdefault(channel, when)
            self._db.execute("UPDATE items SET delivered=? WHERE id=?", (json.dumps(delivered), item_id))

    def requeue(self, item_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE items SET delivered='{}' WHERE id=?", (item_id,))

    def update_title(self, item_id: str, title: str) -> None:
        with self._lock:
            self._db.execute("UPDATE items SET title=? WHERE id=?", (title, item_id))

    def delete(self, item_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM items WHERE id=?", (item_id,))
        shutil.rmtree(self.item_dir(item_id), ignore_errors=True)
        return cur.rowcount > 0

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

    def prune(self, retention_days: int, now: Optional[int] = None) -> int:
        """Delete items and stored deliveries older than ``retention_days``."""
        now = int(now or time.time())
        cutoff = now - retention_days * 86400
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM items WHERE created_at<?", (cutoff,))]
            self._db.execute("DELETE FROM items WHERE created_at<?", (cutoff,))
            old = self._db.execute("SELECT n, file FROM deliveries WHERE created_at<?", (cutoff,)).fetchall()
            self._db.execute("DELETE FROM deliveries WHERE created_at<?", (cutoff,))
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
        """A stored auto-delivery for this device event, for byte-identical replays."""
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
        tmp = self.deliveries_dir / (name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.deliveries_dir / name)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO deliveries (ev, ts, item_ids, file, created_at) VALUES (?,?,?,?,?)",
                (ev, ts, json.dumps(ids), name, int(time.time())),
            )

    def delivery_stats(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ev, ts, item_ids, created_at, hits FROM deliveries ORDER BY n DESC LIMIT 20"
            ).fetchall()
        return [dict(r) | {"item_ids": json.loads(r["item_ids"])} for r in rows]

    # --------------------------------------------------------------------- kv
    def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, value))

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
