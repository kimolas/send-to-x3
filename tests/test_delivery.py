"""Auto-delivery (§7.2: replay, digest, size caps) and catalog paging math."""

from __future__ import annotations

import concurrent.futures
import json
import time

import pytest

from sendtox3 import delivery
from sendtox3.delivery import (
    AUTO_CAP_BYTES,
    build_auto_delivery,
    catalog_page,
    catalog_subtitle,
    document_from_dict,
    document_to_dict,
)
from sendtox3.textmodel import Document, Heading, ListItem, Paragraph, Preformatted, Quote, Rule

from test_web_fakes import add_item, epub_chapters, epub_title, install_fakes


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    install_fakes(monkeypatch)


# ------------------------------------------------------------------- auto-delivery


def test_nothing_pending_returns_none(store):
    assert build_auto_delivery(store, "ev1", "100") is None
    add_item(store, "Read already", delivered={"catalog": 5})
    assert build_auto_delivery(store, "ev1", "100") is None
    assert store.delivery_stats() == []


def test_single_item_is_its_own_epub_and_replays_identically(store):
    it = add_item(store, "Only one")
    data = build_auto_delivery(store, "0a1b2c3d-1", "1761400000")
    assert data == store.file_bytes(it.id, "epub")
    assert store.get(it.id).delivered.keys() == {"auto"}
    add_item(store, "Arrived later")  # must not change a replay
    assert build_auto_delivery(store, "0a1b2c3d-1", "1761400000") == data
    assert store.delivery_stats()[0]["hits"] == 2


def test_digest_oldest_first_and_marks_auto(store):
    a = add_item(store, "Alpha", created_at=100)
    b = add_item(store, "Beta", created_at=200, status="failed")  # failed items deliver their fallback
    c = add_item(store, "Gamma", created_at=300)
    data = build_auto_delivery(store, "ev", "1")
    assert epub_chapters(data) == ["Alpha", "Beta", "Gamma"]
    assert epub_title(data).startswith("Inbox: 3 items (")
    for it in (a, b, c):
        assert "auto" in store.get(it.id).delivered
    assert build_auto_delivery(store, "ev", "2") is None  # a new event with nothing new -> 404
    assert build_auto_delivery(store, "ev", "1") == data  # the old event still replays


def test_same_ev_different_ts_is_a_new_event(store):
    add_item(store, "One")
    first = build_auto_delivery(store, "abcd-1", "10")
    add_item(store, "Two")
    second = build_auto_delivery(store, "abcd-1", "20")
    assert first and second and first != second
    assert epub_title(second) == "Two"


def test_digest_respects_900kb_cap(store):
    a = add_item(store, "Big A", created_at=1, epub_size=400_000)
    b = add_item(store, "Big B", created_at=2, epub_size=400_000)
    c = add_item(store, "Big C", created_at=3, epub_size=400_000)
    data = build_auto_delivery(store, "e", "1")
    assert len(data) <= AUTO_CAP_BYTES
    assert epub_chapters(data) == ["Big A", "Big B"]
    assert store.get(c.id).is_pending
    nxt = build_auto_delivery(store, "e", "2")
    assert nxt == store.file_bytes(c.id, "epub")
    assert not store.get(c.id).is_pending and not store.get(a.id).is_pending and not store.get(b.id).is_pending


def test_oversized_oldest_item_goes_alone(store):
    big = add_item(store, "Huge but ok", created_at=1, epub_size=950_000)
    small = add_item(store, "Small", created_at=2)
    size = store.file_path(big.id, "epub").stat().st_size
    assert AUTO_CAP_BYTES < size <= delivery.SINGLE_MAX_BYTES
    data = build_auto_delivery(store, "e", "1")
    assert data == store.file_bytes(big.id, "epub")
    assert store.get(small.id).is_pending
    assert epub_title(build_auto_delivery(store, "e", "2")) == "Small"


def test_item_over_1mb_becomes_a_note(store):
    huge = add_item(store, "Gigantic", created_at=1, epub_size=1_200_000)
    data = build_auto_delivery(store, "e", "1")
    assert len(data) < 100_000
    assert epub_title(data) == "Too large to deliver: Gigantic"
    # Only the note was delivered: the item stays pending for LAN push and the catalog
    # ("New"), but the note is not sent again and does not block later deliveries.
    assert store.get(huge.id).is_pending
    assert build_auto_delivery(store, "e", "2") is None
    assert build_auto_delivery(store, "e", "1") == data  # replay


def test_passthrough_epub_is_never_in_a_digest(store):
    a = add_item(store, "A", created_at=1)
    p = add_item(store, "Uploaded book", created_at=2, passthrough=True)
    c = add_item(store, "C", created_at=3)
    first = build_auto_delivery(store, "e", "1")
    assert epub_title(first) == "A" and store.get(p.id).is_pending  # digest stopped before it
    second = build_auto_delivery(store, "e", "2")
    assert second == store.file_bytes(p.id, "epub")
    third = build_auto_delivery(store, "e", "3")
    assert epub_title(third) == "C"
    assert all(not store.get(i.id).is_pending for i in (a, p, c))


def test_digest_re_render_drops_items_when_estimate_is_low(store, monkeypatch):
    for i in range(3):
        add_item(store, f"Item {i}", created_at=i + 1)
    real = delivery.render_epub

    def inflating(docs, **kw):
        data = real(docs, **kw)
        return data + b"\0" * (AUTO_CAP_BYTES if len(docs) > 2 else 0)

    monkeypatch.setattr(delivery, "render_epub", inflating)
    data = build_auto_delivery(store, "e", "1")
    assert len(data) <= AUTO_CAP_BYTES
    assert epub_chapters(data) == ["Item 0", "Item 1"]


def test_missing_epub_file_is_skipped(store):
    gone = add_item(store, "Gone", created_at=1)
    ok = add_item(store, "Present", created_at=2)
    store.file_path(gone.id, "epub").unlink()
    data = build_auto_delivery(store, "e", "1")
    assert epub_title(data) == "Present"
    assert store.get(gone.id).is_pending and not store.get(ok.id).is_pending


def test_digest_uses_current_item_title(store):
    a = add_item(store, "Old name", created_at=1)
    add_item(store, "Other", created_at=2)
    store.update_title(a.id, "Renamed")
    assert epub_chapters(build_auto_delivery(store, "e", "1"))[0] == "Renamed"


def test_concurrent_requests_for_one_event_build_once(store):
    for i in range(3):
        add_item(store, f"T{i}", created_at=i + 1)
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: build_auto_delivery(store, "ev", "1"), range(4)))
    assert len({r for r in results}) == 1 and results[0] is not None
    assert len(store.delivery_stats()) == 1


# -------------------------------------------------------------------------- catalog


def _ids(page):
    return [row["id"] for row in page["items"]]


def _rows(store) -> list[str]:
    """The catalog as the device pages through it: the "Get N new" row, then items."""
    return [catalog_page(store, 1, 2)["items"][0]["id"]] + [it.id for it in store.list_items(limit=1000)]


def test_nine_items_page_size_eight(store):
    for i in range(8):
        add_item(store, f"Item {i}", created_at=1000 + i)
    rows = _rows(store)
    assert rows[0].startswith("all-") and len(rows) == 9
    p1 = catalog_page(store, 1, 9)  # firmware sends limit = page_size + 1
    assert _ids(p1) == rows[:9]  # "Get N new" + 7 items shown, 1 extra -> the device adds "Next Page"
    p2 = catalog_page(store, 2, 9)
    assert _ids(p2) == rows[8:]  # offset (2-1)*(9-1) = 8
    assert len(p2["items"]) == 1


def test_seven_items_has_no_extra_row(store):
    for i in range(7):
        add_item(store, f"Item {i}", created_at=1000 + i)
    assert len(catalog_page(store, 1, 9)["items"]) == 8  # "Get N new" + 7: no "Next Page"


@pytest.mark.parametrize("page,limit,start,count", [(1, 9, 0, 9), (2, 9, 8, 9), (3, 9, 16, 5), (4, 9, 24, 0),
                                                    (1, 2, 0, 2), (5, 2, 4, 2), (2, 17, 16, 5)])
def test_exact_offsets(store, page, limit, start, count):
    for i in range(20):
        add_item(store, f"Item {i}", created_at=1000 + i)
    rows = _rows(store)
    assert len(rows) == 21
    assert _ids(catalog_page(store, page, limit)) == rows[start:start + count]


@pytest.mark.parametrize("page,limit,offset,start,count", [
    (2, 9, None, 8, 9), (3, 9, None, 16, 5),  # 2.0.0rc, 3f387a9: (page - 1) * (limit - 1)
    (2, 9, 8, 8, 9), (3, 9, 16, 16, 5),  # develop 8f4d035+: the offset it sends
    (1, 9, 16, 16, 5), (3, 9, 0, 0, 9),  # the offset wins over the page
    (2, 8, None, 8, 8), (3, 8, None, 16, 5),  # develop with a 0.2.0 plugin: (page - 1) * limit
])
def test_start_row_per_firmware(store, page, limit, offset, start, count):
    for i in range(20):
        add_item(store, f"Item {i}", created_at=1000 + i)
    rows = _rows(store)
    assert delivery.PAGE_SIZE == 8
    assert _ids(catalog_page(store, page, limit, offset=offset)) == rows[start:start + count]


def test_paging_is_clamped(store):
    items = [add_item(store, f"Item {i}", created_at=1000 + i) for i in range(30)]
    assert len(catalog_page(store, 1, 1)["items"]) == 2  # limit >= 2
    assert len(catalog_page(store, 1, 500)["items"]) == 17  # limit <= 17
    assert _ids(catalog_page(store, 0, 9)) == _ids(catalog_page(store, 1, 9))
    assert _ids(catalog_page(store, -3, 9)) == _ids(catalog_page(store, 1, 9))
    assert len(items) == 30


def test_rows_have_exactly_the_mapped_fields(store):
    add_item(store, "Row")
    row = catalog_page(store, 1, 9)["items"][0]
    assert set(row) == {"id", "title", "subtitle"}
    json.dumps(catalog_page(store, 1, 9))  # serializable


def test_subtitles(store):
    when = int(time.mktime((2026, 10, 25, 12, 0, 0, 0, 0, -1)))
    fresh = add_item(store, "Fresh", words=1234, source_url="https://www.example.com/a")
    read = add_item(store, "Read", words=1, source_url="https://news.example.org/x", delivered={"opds": when})
    failed = add_item(store, "Failed", status="failed", kind="url", source_url="https://blocked.example/")
    note = add_item(store, "Note", kind="text")
    mail = add_item(store, "Mail", kind="email")
    sub = {it.id: catalog_subtitle(store.get(it.id)) for it in (fresh, read, failed, note, mail)}
    assert sub[fresh.id] == "New · 1,234 words · example.com"
    assert sub[read.id] == "Read on X3 2026-10-25 · 1 word · news.example.org"
    assert sub[failed.id] == "New · extraction failed · blocked.example"
    assert sub[note.id].endswith("· note") and sub[mail.id].endswith("· email")


def test_subtitle_uses_first_delivery(store):
    t1 = int(time.mktime((2026, 10, 20, 9, 0, 0, 0, 0, -1)))
    t2 = int(time.mktime((2026, 10, 24, 9, 0, 0, 0, 0, -1)))
    it = add_item(store, "Twice", delivered={"push": t2, "auto": t1})
    assert catalog_subtitle(store.get(it.id)).startswith("Read on X3 2026-10-20")


def test_unsafe_titles_are_sanitized(store):
    bad = add_item(store, "Wait... what {url} a/b")
    good = add_item(store, "Fine title … with ellipsis (2)")
    rows = {r["id"]: r["title"] for r in catalog_page(store, 1, 9)["items"]}
    assert ".." not in rows[bad.id] and "{" not in rows[bad.id] and "/" not in rows[bad.id]
    assert rows[good.id] == "Fine title … with ellipsis (2)"  # already safe: unchanged


def test_document_round_trip():
    doc = Document(title="T", blocks=[Heading("H", 3), Paragraph("p"), ListItem("li", True, 2, 1), Quote("q"),
                                      Preformatted("a\n  b"), Rule()],
                   byline="by", site="s", source_url="u", published="2026-01-01", lang="de", notes=["n"])
    assert document_from_dict(json.loads(json.dumps(document_to_dict(doc)))) == doc


def test_corrupt_doc_json_is_treated_as_passthrough(store):
    it = add_item(store, "Corrupt")
    (store.item_dir(it.id) / "doc.json").write_text("{not json")
    assert delivery.load_document(store, it.id) is None
    assert build_auto_delivery(store, "e", "1") == store.file_bytes(it.id, "epub")
