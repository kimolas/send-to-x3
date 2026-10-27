"""Regression tests for review findings 26 (too-large note) and 38 (damaged oldest item)."""

from __future__ import annotations

import pytest

from sendtox3 import delivery
from sendtox3.delivery import build_auto_delivery, catalog_page
from sendtox3.textmodel import Document, Paragraph

from test_web_fakes import add_item, epub_chapters, epub_title, install_fakes


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    install_fakes(monkeypatch)


# ---------------------------------------------------------------- finding 26


def test_note_only_delivery_keeps_the_item_pending(store):
    huge = add_item(store, "Gigantic", created_at=1, epub_size=1_200_000, passthrough=True)
    note = build_auto_delivery(store, "e", "1")
    assert epub_title(note) == "Too large to deliver: Gigantic"
    it = store.get(huge.id)
    assert it.is_pending and it.delivered == {}
    assert [i.id for i in store.pending_items()] == [huge.id]  # LAN push still pushes it
    row = catalog_page(store, 1, 9)["items"][1]
    assert row["subtitle"].startswith("New · ")
    # The note is sent once; later items are delivered normally.
    assert build_auto_delivery(store, "e", "2") is None
    small = add_item(store, "Small", created_at=2)
    assert epub_title(build_auto_delivery(store, "e", "3")) == "Small"
    assert "auto" in store.get(small.id).delivered and store.get(huge.id).is_pending
    assert build_auto_delivery(store, "e", "1") == note  # replays stay byte-identical


def test_requeue_sends_the_note_again(store):
    huge = add_item(store, "Gigantic", created_at=1, epub_size=1_200_000, passthrough=True)
    first = build_auto_delivery(store, "e", "1")
    store.requeue(huge.id)
    assert build_auto_delivery(store, "e", "2") == first


def test_lan_push_delivery_of_a_noted_item_marks_it(store):
    huge = add_item(store, "Gigantic", created_at=1, epub_size=1_200_000, passthrough=True)
    build_auto_delivery(store, "e", "1")
    store.mark_delivered(huge.id, "push")
    assert not store.get(huge.id).is_pending and store.pending_items() == []


def test_deleting_or_pruning_clears_the_skip_marker(store):
    a = add_item(store, "A", created_at=1, epub_size=1_200_000, passthrough=True)
    b = add_item(store, "B", created_at=2, epub_size=1_200_000, passthrough=True)
    build_auto_delivery(store, "e", "1")
    build_auto_delivery(store, "e", "2")
    assert set(store.auto_skipped()) == {a.id, b.id}
    store.delete(a.id)
    assert set(store.auto_skipped()) == {b.id}
    store.prune(30, now=10 * 86400 * 365)
    assert store.auto_skipped() == {}


# ---------------------------------------------------------------- finding 38


@pytest.mark.parametrize("damage", ["empty", "missing"])
def test_damaged_oldest_item_does_not_block_deliveries(store, damage):
    broken = add_item(store, "Broken", created_at=1)
    ok = add_item(store, "Fine", created_at=2)
    path = store.file_path(broken.id, "epub")
    if damage == "empty":
        path.write_bytes(b"")
        (store.item_dir(broken.id) / "doc.json").write_bytes(b"")
    else:
        path.unlink()
    data = build_auto_delivery(store, "ev0", "1")
    assert data is not None and epub_title(data) == "Fine"
    assert "auto" in store.get(ok.id).delivered
    bad = store.get(broken.id)
    assert bad.status == "failed" and "damaged" in bad.error  # visible in the admin UI
    assert build_auto_delivery(store, "ev1", "2") is None  # not retried every sleep
    later = add_item(store, "Later", created_at=3)
    assert epub_title(build_auto_delivery(store, "ev2", "3")) == "Later" and later.id


def test_empty_epub_in_a_digest_position_is_skipped(store):
    a = add_item(store, "A", created_at=1)
    broken = add_item(store, "Broken", created_at=2)
    c = add_item(store, "C", created_at=3)
    store.file_path(broken.id, "epub").write_bytes(b"")
    data = build_auto_delivery(store, "e", "1")
    assert epub_chapters(data) == ["A", "C"]
    assert store.get(broken.id).is_pending and store.get(broken.id).status == "failed"
    assert all("auto" in store.get(i.id).delivered for i in (a, c))


def test_files_are_written_durably(store, monkeypatch):
    import os

    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))
    add_item(store, "Durable")
    assert len(synced) >= 3  # doc.json, item.epub, item.txt (plus their folder)


# ------------------------------------------------- finding 21: digest chapter titles


def test_digest_uses_the_stored_headline_unless_renamed(store):
    a = add_item(store, "Plain A", created_at=1)
    b = add_item(store, "Plain B", created_at=2)
    delivery.save_document(store, a.id, Document(title="What? A headline", blocks=[Paragraph("x " * 30)]),
                           item_title="Plain A")
    delivery.save_document(store, b.id, Document(title="Other: headline", blocks=[Paragraph("y " * 30)]),
                           item_title="Plain B")
    store.update_title(b.id, "Renamed B")
    assert epub_chapters(build_auto_delivery(store, "e", "1")) == ["What? A headline", "Renamed B"]
