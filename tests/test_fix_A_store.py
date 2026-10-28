"""Regression tests for review findings 25 (titles reused after prune/delete) and 35
(shares lost on restart: the spool and Processor.recover_pending)."""

from __future__ import annotations

import asyncio
import time

import pytest

from sendtox3 import ingest
from sendtox3.ingest import Processor, StorageError
from sendtox3.models import IngestRequest, Item, new_item_id
from sendtox3.store import Store

from test_web_fakes import install_fakes


@pytest.fixture
def web(monkeypatch):
    return install_fakes(monkeypatch)


@pytest.fixture
def proc(store, opts, web):
    return Processor(store, opts)


def _age_spool(store, seconds=60):
    """Pretend the spooled entries were written by an earlier run of the app."""
    store._db.execute("UPDATE pending_requests SET created_at=created_at-?", (seconds,))


# ---------------------------------------------------------------- finding 25


async def test_downloaded_title_is_not_reused_after_prune(proc, store, web):
    web.add("https://example.com/b1", "Morning Briefing")
    first = await proc.process(IngestRequest(text="https://example.com/b1"))
    store.mark_delivered(first.id, "catalog")  # now /Inbox/Morning Briefing.epub on the X3
    store._db.execute("UPDATE items SET created_at=? WHERE id=?", (int(time.time()) - 40 * 86400, first.id))
    assert store.prune(30) == 1
    web.add("https://example.com/b2", "Morning Briefing")
    second = await proc.process(IngestRequest(text="https://example.com/b2"))
    assert second.title == "Morning Briefing (2)"  # would overwrite the old file otherwise


async def test_downloaded_title_is_not_reused_after_delete(proc, store, web, monkeypatch):
    from sendtox3.render.names import unique_title

    monkeypatch.setattr(ingest, "unique_title", unique_title)  # the real one: FAT names ignore case
    web.add("https://example.com/d1", "Weekly Digest")
    first = await proc.process(IngestRequest(text="https://example.com/d1"))
    store.mark_delivered(first.id, "opds")
    store.delete(first.id)
    web.add("https://example.com/d2", "weekly digest")
    second = await proc.process(IngestRequest(text="https://example.com/d2"))
    assert second.title == "weekly digest (2)"


async def test_title_never_downloaded_by_name_is_free_again(proc, store, web):
    web.add("https://example.com/n1", "Never Downloaded")
    first = await proc.process(IngestRequest(text="https://example.com/n1"))
    store.mark_delivered(first.id, "auto")  # saved as /Inbox/<ts>-<ev>.epub: no clash
    store.delete(first.id)
    web.add("https://example.com/n2", "Never Downloaded")
    assert (await proc.process(IngestRequest(text="https://example.com/n2"))).title == "Never Downloaded"


def test_issued_titles_never_expire(store):
    """Nothing tells the app when a file leaves the card, and on the Send to X3 firmware
    Open on a reused title's row would open the old file instead of downloading."""
    it = store.add_item(Item(id=new_item_id(), created_at=1, kind="text", source="test", title="Old"),
                        epub=b"e", txt=b"t")
    store.mark_delivered(it.id, "catalog", when=1000)
    store.delete(it.id)
    assert store.issued_titles() == {"Old"}
    store.prune(30, now=1000 + 10 * 366 * 86400)
    assert store.issued_titles() == {"Old"}


# ---------------------------------------------------------------- finding 35


async def test_share_interrupted_by_a_restart_is_recovered(store, opts, web, data_dir):
    web.add("https://example.com/slowpdf", "Slow report")
    web.delay = 5
    first_run = Processor(store, opts)
    task = asyncio.ensure_future(first_run.process(IngestRequest(url="https://example.com/slowpdf")))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if store.spool_count():
            break
    task.cancel()  # the app stops (or crashes) while the share is being processed
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.count() == 0 and store.spool_count() == 1
    store.close()

    web.delay = 0
    reopened = Store(data_dir)
    _age_spool(reopened)
    try:
        assert await Processor(reopened, opts).recover_pending() == 1
        [item] = reopened.list_items()
        assert item.title == "Slow report" and item.status == "ready"
        assert reopened.spool_count() == 0
    finally:
        reopened.close()


async def test_spooled_file_payload_is_recovered(store, opts, web, data_dir):
    pdf = b"%PDF-1.4\n(Spooled) Tj (bytes) Tj\n%%EOF" + b" " * 300_000  # above the inline limit
    store.spool_add(IngestRequest(data=pdf, filename="Scan_2026.pdf", source="email", sender="a@example.com"))
    _age_spool(store)
    assert (data_dir / "spool").is_dir() and len(list((data_dir / "spool").iterdir())) == 1
    assert await Processor(store, opts).recover_pending() == 1
    [item] = store.list_items()
    assert item.kind == "pdf" and item.title == "Scan 2026" and item.source == "email"
    assert list((data_dir / "spool").iterdir()) == []


async def test_successful_processing_leaves_the_spool_empty(proc, store, web):
    await proc.process(IngestRequest(text="A note"))
    await proc.process(IngestRequest(data=b"x" * 400_000, filename="big.txt"))
    assert store.spool_count() == 0 and list(store.spool_dir.iterdir()) == []


async def test_shares_in_flight_in_this_run_are_not_recovered(proc, store, web):
    store.spool_add(IngestRequest(text="being processed right now"))
    assert await proc.recover_pending() == 0
    assert store.count() == 0 and store.spool_count() == 1


async def test_a_share_that_keeps_crashing_the_app_is_given_up(proc, store, web):
    req = IngestRequest(text="https://example.com/poison")
    sid = store.spool_add(req)
    for _ in range(ingest.SPOOL_MAX_ATTEMPTS - 1):
        store.spool_attempt(sid)
    _age_spool(store)
    assert await proc.recover_pending() == 1
    [item] = store.list_items()
    assert item.status == "failed" and "stopped 3 times" in item.error
    assert item.source_url == "https://example.com/poison" and web.calls == []
    assert store.spool_count() == 0


async def test_recovery_counts_attempts(proc, store, web):
    sid = store.spool_add(IngestRequest(text="counted"))
    _age_spool(store)
    seen = []
    real = proc._process_request

    async def spy(req):
        seen.append([e.attempts for e in store.spooled_requests()])
        return await real(req)

    proc._process_request = spy
    await proc.recover_pending()
    assert seen == [[2]] and store.spool_count() == 0 and sid


async def test_lost_spool_file_is_recorded_as_failed(proc, store, web):
    sid = store.spool_add(IngestRequest(data=b"%PDF-1.4 lost", filename="lost.pdf"))
    (store.spool_dir / f"{sid}.bin").unlink()
    _age_spool(store)
    assert await proc.recover_pending() == 1
    [item] = store.list_items()
    assert item.status == "failed" and "lost" in item.error and item.title == "lost"


async def test_recovery_stops_while_storage_fails(proc, store, web, monkeypatch):
    store.spool_add(IngestRequest(text="one"))
    store.spool_add(IngestRequest(text="two"))
    _age_spool(store)

    def full(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "add_item", full)
    assert await proc.recover_pending() == 0
    assert store.spool_count() == 2  # kept for the next start


def test_spool_round_trip(store):
    req = IngestRequest(kind="url", source="ha_share", url="https://example.com/x", text="t", title="T",
                        sender="Pixel 8", received_at=123)
    sid = store.spool_add(req)
    assert store.spool_add(IngestRequest(**{**req.__dict__, "received_at": 456})) == sid  # one entry
    [entry] = store.spooled_requests()
    assert entry.id == sid and entry.request == req and entry.attempts == 1 and not entry.data_lost
    store.spool_remove(sid)
    assert store.spooled_requests() == []


async def test_storage_error_is_the_only_exception(proc, store, web, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(store, "spool_add", broken)  # a spool bug: process anyway
    item = await proc.process(IngestRequest(text="still processed"))
    assert item.status == "ready"
    monkeypatch.setattr(store, "titles_in_use", broken)  # store unusable: StorageError
    with pytest.raises(StorageError):
        await proc.process(IngestRequest(text="cannot be stored"))
