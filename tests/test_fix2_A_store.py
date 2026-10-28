"""Round-2 regression tests for group A (store): R2-A4 (titles downloaded before the
issued_titles table existed stay reserved) and R2-A7 (the data folder and the database
holding the tokens are owner-only), plus write_file's tmp names (two identical uploads
spooled at once no longer race on one tmp file)."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time

import pytest

from sendtox3 import delivery
from sendtox3.ingest import Processor
from sendtox3.models import IngestRequest, Item, new_item_id
from sendtox3.store import FILE_MODE, Store, write_file
from sendtox3.textmodel import Document

from test_web_fakes import install_fakes

POSIX_MODES = pytest.mark.skipif(os.name != "posix", reason="chmod is (nearly) a no-op on this platform")


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------- R2-A4: backfill


def _old_database(data_dir, rows: list[tuple[str, dict]], *, now: int) -> None:
    """A database as the version before issued_titles left it: items, no such table."""
    db = sqlite3.connect(data_dir / "sendtox3.db")
    db.executescript("""
        CREATE TABLE items (id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, kind TEXT NOT NULL,
            source TEXT NOT NULL, title TEXT NOT NULL, byline TEXT NOT NULL DEFAULT '',
            site TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ready', error TEXT NOT NULL DEFAULT '',
            word_count INTEGER NOT NULL DEFAULT 0, dedupe_key TEXT NOT NULL DEFAULT '',
            delivered TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    """)
    for title, delivered in rows:
        db.execute("INSERT INTO items (id, created_at, kind, source, title, delivered) VALUES (?,?,?,?,?,?)",
                   (new_item_id(), now - 86400, "url", "http", title, json.dumps(delivered)))
    db.commit()
    db.close()


def test_titles_downloaded_before_the_upgrade_are_backfilled(data_dir):
    now = int(time.time())
    old = now - 800 * 86400
    _old_database(data_dir, [
        ("Morning Briefing", {"catalog": now - 3600}),
        ("OPDS Book", {"opds": now - 7200, "auto": now - 9000}),
        ("Auto Only", {"auto": now - 3600}),  # sent in a digest: not saved under its title
        ("Pushed", {"push": now - 3600}),  # LAN push saves <title>.epub too
        ("Never Sent", {}),
        ("Ancient", {"catalog": old}),  # reservations never expire: its file may be on the card
    ], now=now)
    store = Store(data_dir)
    try:
        assert store.issued_titles() == {"Morning Briefing", "OPDS Book", "Pushed", "Ancient"}
        rows = dict(store._db.execute("SELECT title, issued_at FROM issued_titles").fetchall())
        assert rows["Morning Briefing"] == now - 3600 and rows["OPDS Book"] == now - 7200
        # Deleting or pruning the item no longer frees its title.
        for it in store.list_items(limit=100):
            store.delete(it.id)
        assert {"Morning Briefing", "OPDS Book", "Pushed", "Ancient"} <= store.issued_titles()
    finally:
        store.close()


def test_pushed_titles_are_backfilled_after_the_first_migration(data_dir):
    """A database from 0.2.0: catalog and OPDS titles were reserved, pushed ones not."""
    now = int(time.time())
    _old_database(data_dir, [("Pushed", {"push": now - 3600}), ("Auto Only", {"auto": now - 60})], now=now)
    db = sqlite3.connect(data_dir / "sendtox3.db")
    db.execute("INSERT INTO kv (k, v) VALUES ('migration:issued_titles_backfill', '1')")
    db.commit()
    db.close()
    store = Store(data_dir)
    try:
        assert store.issued_titles() == {"Pushed"}
        assert store.kv_get("migration:issued_titles_backfill_push") == "1"
    finally:
        store.close()


def test_backfill_runs_once(data_dir):
    store = Store(data_dir)
    item = Item(id=new_item_id(), created_at=int(time.time()), kind="url", source="http", title="Later",
                delivered={"catalog": int(time.time())})
    store.add_item(item, epub=b"e", txt=b"t")  # delivered JSON written directly, not via mark_delivered
    store.close()
    store = Store(data_dir)
    try:
        assert "Later" not in store.issued_titles()  # the migration already ran on the first open
    finally:
        store.close()


async def test_backfilled_title_is_not_reused_after_delete(data_dir, opts, monkeypatch):
    web = install_fakes(monkeypatch)
    now = int(time.time())
    _old_database(data_dir, [("Morning Briefing", {"catalog": now - 3600})], now=now)
    store = Store(data_dir)
    try:
        for it in store.list_items(limit=10):
            store.delete(it.id)
        web.add("https://example.com/b2", "Morning Briefing")
        item = await Processor(store, opts).process(IngestRequest(url="https://example.com/b2"))
        assert item.title == "Morning Briefing (2)"
    finally:
        store.close()


# ---------------------------------------------------------------- R2-A7: permissions


@POSIX_MODES
def test_data_folder_and_database_are_owner_only(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    os.chmod(data, 0o755)
    old = os.umask(0o022)
    try:
        store = Store(data)
        try:
            store.kv_set("k", "v")  # makes sure -wal and -shm exist
            assert _mode(data) == 0o700
            for sub in ("items", "deliveries", "spool"):
                assert _mode(data / sub) == 0o700, sub
            for name in ("sendtox3.db", "sendtox3.db-wal", "sendtox3.db-shm"):
                assert (data / name).exists() and _mode(data / name) == 0o600, name
            store.add_item(Item(id="abcdefghij", created_at=1, kind="text", source="http", title="T"),
                           epub=b"epub", txt=b"txt")
            delivery.save_document(store, "abcdefghij", Document(title="T"))
            assert _mode(store.item_dir("abcdefghij")) == 0o700
            for name in ("item.epub", "item.txt", "doc.json"):
                assert _mode(store.item_dir("abcdefghij") / name) == 0o600, name
            store.save_delivery("ev", "1", ["abcdefghij"], b"payload")
            assert {_mode(p) for p in (data / "deliveries").iterdir()} == {0o600}
            spool_id = store.spool_add(IngestRequest(data=b"%PDF-1.4 bytes", filename="a.pdf"))
            assert _mode(data / "spool" / f"{spool_id}.bin") == 0o600
        finally:
            store.close()
    finally:
        os.umask(old)


@POSIX_MODES
def test_existing_loose_permissions_are_tightened(tmp_path):
    data = tmp_path / "data"
    Store(data).close()
    for p in [data, data / "items", data / "deliveries", data / "spool"]:
        os.chmod(p, 0o755)
    for name in ("sendtox3.db", "sendtox3.db-wal", "sendtox3.db-shm"):
        if (data / name).exists():
            os.chmod(data / name, 0o644)
    store = Store(data)
    try:
        assert _mode(data) == _mode(data / "items") == _mode(data / "spool") == 0o700
        assert _mode(data / "sendtox3.db") == 0o600
        for name in ("sendtox3.db-wal", "sendtox3.db-shm"):
            if (data / name).exists():
                assert _mode(data / name) == 0o600, name
    finally:
        store.close()


@POSIX_MODES
def test_chmod_failure_does_not_stop_the_store(tmp_path, monkeypatch):
    def refuse(path, mode, *a, **kw):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(os, "chmod", refuse)
    store = Store(tmp_path / "data")
    try:
        store.kv_set("k", "v")
        assert store.kv_get("k") == "v"
    finally:
        store.close()


@POSIX_MODES
def test_write_file_is_owner_only_whatever_the_umask(tmp_path):
    old = os.umask(0)
    try:
        write_file(tmp_path / "f.bin", b"data")
    finally:
        os.umask(old)
    assert _mode(tmp_path / "f.bin") == FILE_MODE and (tmp_path / "f.bin").read_bytes() == b"data"


# ---------------------------------------------------------------- write_file tmp names


def test_concurrent_writers_of_one_path_do_not_collide(tmp_path):
    target = tmp_path / "same.bin"
    payload = os.urandom(512 * 1024)
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def writer():
        try:
            start.wait()
            for _ in range(5):
                write_file(target, payload)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert target.read_bytes() == payload
    assert [p.name for p in tmp_path.iterdir()] == ["same.bin"]  # no tmp file left behind


def test_failed_write_leaves_no_tmp_file(tmp_path, monkeypatch):
    def boom(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        write_file(tmp_path / "x.bin", b"data")
    assert list(tmp_path.iterdir()) == []


def test_stale_tmp_files_are_removed_at_start(data_dir):
    Store(data_dir).close()
    stale, fresh = data_dir / "spool" / "x.bin.1a2b3c4d.tmp", data_dir / "deliveries" / "y.epub.5e6f7a8b.tmp"
    for p in (stale, fresh):
        p.write_bytes(b"partial")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    Store(data_dir).close()
    assert not stale.exists() and fresh.exists()  # a recent one may belong to a running server


def test_failed_backfill_does_not_stop_the_store(data_dir, monkeypatch):
    def boom(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "_backfill_issued_titles", boom)
    store = Store(data_dir)
    try:
        store.kv_set("k", "v")
        assert store.kv_get("k") == "v"
    finally:
        store.close()


def test_identical_large_uploads_spooled_at_once(store):
    req = IngestRequest(data=b"%PDF-1.4 " + os.urandom(400 * 1024), filename="big.pdf")
    ids: list[str] = []
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def spool():
        try:
            start.wait()
            ids.append(store.spool_add(req))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=spool) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(ids)) == 1 and store.spool_count() == 1
    assert store.spooled_requests()[0].request.data == req.data

