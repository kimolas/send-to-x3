""""Get N new": the catalog's first row (and the first OPDS entry) downloads everything
never delivered as one book, so the X3 fetches only when the user asks (§7.2)."""

from __future__ import annotations

import re
import time

import pytest

from sendtox3 import delivery
from sendtox3.delivery import (
    AUTO_CAP_BYTES,
    FETCH_ID_RE,
    build_fetch_bundle,
    catalog_page,
    device_title,
    fetch_download,
    fetch_title,
)
from sendtox3.ingest import Processor
from sendtox3.web.app import create_app

from test_web_fakes import add_item, epub_chapters, epub_title, install_fakes

AUTH = {"Authorization": "Bearer device-test-token"}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    return install_fakes(monkeypatch)


@pytest.fixture
async def client(serve_like_main, opts, store):
    return await serve_like_main(create_app(opts, store, Processor(store, opts)))


def _first(store, fmt="epub") -> dict:
    return catalog_page(store, 1, 9, fmt=fmt)["items"][0]


async def eventually(cond, timeout: float = 3.0) -> bool:
    """The server marks a download after the X3's TCP stack acknowledged the last byte,
    a moment after the client has read it."""
    import asyncio

    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            return False
        await asyncio.sleep(0.02)
    return True


# ------------------------------------------------------------------------ the row


def test_nothing_new_row(store):
    assert _first(store) == {"id": "none", "title": "Nothing new right now",
                             "subtitle": "Share something to Send to X3, then open this list again."}
    add_item(store, "Seen", delivered={"opds": 1})
    assert _first(store)["id"] == "none"
    assert fetch_download(store, "none") is None


def test_get_n_new_row(store):
    a = add_item(store, "First article", created_at=1_761_480_000, words=100, source_url="https://example.com/a")
    add_item(store, "Seen before", created_at=1_761_480_050, delivered={"catalog": 1})
    b = add_item(store, "Second note", created_at=1_761_480_300, words=20)
    row = _first(store)
    assert re.fullmatch(FETCH_ID_RE, row["id"])
    stamp = time.localtime(b.created_at)  # the newest share in the bundle
    assert row["title"] == f"Get 2 new · {time.strftime('%b', stamp)} {stamp.tm_mday} {stamp.tm_hour:02d}.{stamp.tm_min:02d}"
    assert row["subtitle"] == "Everything new in one book · 120 words: Second note; First article"
    assert set(row) == {"id", "title", "subtitle"}
    rows = catalog_page(store, 1, 9)["items"]
    assert [r["id"] for r in rows[1:]] == [it.id for it in store.list_items()]
    assert a.id in [r["id"] for r in rows]


def test_title_is_firmware_safe_as_a_file_name(store):
    for i in range(12):
        add_item(store, f"Item {i}", created_at=1_761_480_000 + i * 3600)
    title = _first(store)["title"]
    assert device_title(title) == title
    assert ":" not in title and ".." not in title and not title.endswith(".")
    assert len(f"{title}.epub".encode()) <= 100


def test_long_subtitle_is_cut(store):
    for i in range(20):
        add_item(store, f"A rather long article title number {i}", created_at=1000 + i)
    sub = _first(store)["subtitle"]
    assert len(sub) <= 160 and sub.endswith("…")


def test_txt_format_row_says_file(store):
    add_item(store, "Plain")
    assert _first(store, "txt")["subtitle"].startswith("Everything new in one file")


# ------------------------------------------------------------- bundle contents


def test_single_item_is_its_own_epub(store):
    it = add_item(store, "Only one")
    bundle = build_fetch_bundle(store)
    assert bundle.item_ids == [it.id] and bundle.title.startswith("Get 1 new · ")
    assert bundle.data == store.file_bytes(it.id, "epub")  # keeps its own title inside the book


def test_several_items_are_one_digest_oldest_first(store):
    add_item(store, "Older", created_at=1)
    add_item(store, "Newer", created_at=2)
    data = build_fetch_bundle(store).data
    assert epub_title(data).startswith("Inbox: 2 items (")
    assert epub_chapters(data) == ["Older", "Newer"]


def test_large_single_item_has_no_size_limit(store):
    """Catalog downloads have no 1 MiB limit (only sleep downloads do), so a big item
    goes whole, not as the "too large" note."""
    huge = add_item(store, "Gigantic", created_at=1, epub_size=1_300_000, passthrough=True)
    bundle = build_fetch_bundle(store)
    assert bundle.item_ids == [huge.id] and bundle.data == store.file_bytes(huge.id, "epub")


def test_items_over_the_cap_wait_for_the_next_bundle(store):
    a = add_item(store, "A", created_at=1, epub_size=AUTO_CAP_BYTES // 2 + 1000)
    b = add_item(store, "B", created_at=2, epub_size=AUTO_CAP_BYTES // 2 + 1000)
    first = build_fetch_bundle(store)
    assert first.item_ids == [a.id] and first.title.startswith("Get 1 new")
    fetch_download(store, first.id)
    second = build_fetch_bundle(store)
    assert second.item_ids == [b.id] and second.id != first.id


def test_damaged_item_is_skipped(store):
    bad = add_item(store, "Broken", created_at=1)
    good = add_item(store, "Fine", created_at=2)
    store.file_path(bad.id, "epub").unlink()
    bundle = build_fetch_bundle(store)
    assert bundle.item_ids == [good.id]
    assert store.get(bad.id).status == "failed"


def test_txt_bundle_joins_the_item_files(store):
    a = add_item(store, "Alpha", created_at=1, text="first body")
    b = add_item(store, "Beta", created_at=2, text="second body")
    data = build_fetch_bundle(store, "txt").data.decode("utf-8")
    assert data.index("Alpha") < data.index("first body") < data.index("* * *") < data.index("Beta")
    assert data.endswith("\n") and "\r" not in data and "\t" not in data
    assert build_fetch_bundle(store, "txt").item_ids == [a.id, b.id]
    epub = build_fetch_bundle(store, "epub")
    txt = build_fetch_bundle(store, "txt")
    assert (txt.id, txt.title) == (epub.id, epub.title) and txt.data != epub.data  # one bundle, two formats


# ----------------------------------------------------------- download semantics


def test_download_marks_items_fetched_and_the_row_resets(store):
    a = add_item(store, "A", created_at=1)
    b = add_item(store, "B", created_at=2)
    row = _first(store)
    data = fetch_download(store, row["id"])
    assert epub_chapters(data) == ["A", "B"]
    for it in (a, b):
        got = store.get(it.id)
        assert "fetch" in got.delivered and not got.is_pending
    assert _first(store)["id"] == "none"
    rows = catalog_page(store, 1, 9)["items"]
    assert all(r["subtitle"].startswith("Read on X3 ") for r in rows[1:])
    assert store.delivery_stats() == []  # bundles are not sleep deliveries (no admin warnings)


def test_same_new_items_give_the_same_id_and_bytes(store, monkeypatch):
    add_item(store, "A", created_at=1)
    add_item(store, "B", created_at=2)
    first = build_fetch_bundle(store)
    calls = []
    monkeypatch.setattr(delivery, "render_epub", lambda *a, **k: calls.append(1) or b"rebuilt")
    again = build_fetch_bundle(store)
    assert (again.id, again.data, again.title) == (first.id, first.data, first.title)
    assert calls == []  # built once, stored
    # Repeated downloads (a resumed transfer after a drop, or Confirm pressed twice before
    # the report) get the same bytes.
    assert fetch_download(store, first.id, complete=False) == first.data
    assert fetch_download(store, first.id, confirm=True) == fetch_download(store, first.id, confirm=True) == first.data


def test_a_new_share_makes_a_new_bundle_and_drops_the_unfetched_one(store):
    add_item(store, "A", created_at=1)
    old = build_fetch_bundle(store)
    add_item(store, "B", created_at=2)
    new = build_fetch_bundle(store)
    assert new.id != old.id and len(new.item_ids) == 2
    assert fetch_download(store, old.id) is None  # replaced: the X3 shows "Download failed", nothing is marked
    assert all(store.get(i).is_pending for i in new.item_ids)
    assert len(list(store.deliveries_dir.iterdir())) == 1


def test_renaming_an_item_rebuilds_the_bundle(store):
    it = add_item(store, "Old name", created_at=1)
    old = build_fetch_bundle(store)
    store.update_title(it.id, "New name")
    assert build_fetch_bundle(store).id != old.id


def test_bundle_items_delivered_elsewhere_are_not_refetched(store):
    a = add_item(store, "A", created_at=1)
    b = add_item(store, "B", created_at=2)
    build_fetch_bundle(store)
    store.mark_delivered(a.id, "push")
    bundle = build_fetch_bundle(store)
    assert bundle.item_ids == [b.id]


def test_unknown_ids(store):
    add_item(store, "A")
    assert fetch_download(store, "all-aaaaaaaaaa") is None
    assert fetch_download(store, "abcdefghij") is None


def test_fetch_title_uses_the_newest_share():
    from sendtox3.models import Item

    older = Item(id="aaaaaaaaaa", created_at=1_761_480_000, kind="text", source="test", title="x")
    newer = Item(id="bbbbbbbbbb", created_at=1_761_490_000, kind="text", source="test", title="y")
    t = time.localtime(newer.created_at)
    assert fetch_title([older, newer]).endswith(f"{t.tm_hour:02d}.{t.tm_min:02d}")
    assert fetch_title([older, newer]).startswith("Get 2 new · ")


def test_prune_removes_old_bundles(store):
    add_item(store, "A", created_at=int(time.time()))
    build_fetch_bundle(store)
    assert store.prune(30, now=int(time.time()) + 31 * 86400) == 1
    assert list(store.deliveries_dir.iterdir()) == []


# --------------------------------------------------------------------- over HTTP


def _framing_ok(resp, body: bytes) -> bool:
    return (resp.headers["Content-Length"] == str(len(body)) and "Transfer-Encoding" not in resp.headers
            and "Content-Encoding" not in resp.headers and resp.headers.get("Connection", "").lower() == "close")


@pytest.mark.parametrize("fmt,ctype", [("epub", "application/epub+zip"), ("txt", "text/plain; charset=utf-8")])
async def test_http_fetch_flow(client, store, opts, fmt, ctype):
    opts.delivery_format = fmt
    a = add_item(store, "A", created_at=1)
    b = add_item(store, "B", created_at=2)
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)).json())["items"]
    assert rows[0]["title"].startswith("Get 2 new · ")
    resp = await client.get(f"/device/v1/items/{rows[0]['id']}.{fmt}", headers=AUTH)
    body = await resp.read()
    assert resp.status == 200 and resp.headers["Content-Type"] == ctype and body and _framing_ok(resp, body)
    assert await eventually(lambda: all("fetch" in store.get(i.id).delivered for i in (a, b)))
    # Delivered (no confirm): the bundle is retired, so a second press is "Download failed"
    # rather than a replacement of the saved file.
    again = await client.get(f"/device/v1/items/{rows[0]['id']}.{fmt}", headers=AUTH)
    assert again.status == 404
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)).json())["items"]
    assert rows[0]["id"] == "none"


async def test_http_nothing_new_is_a_small_404(client, store):
    resp = await client.get("/device/v1/items/none.epub", headers=AUTH)
    body = await resp.read()
    assert resp.status == 404 and 0 < len(body) < 100 and _framing_ok(resp, body)
    assert (await client.get("/device/v1/items/all-aaaaaaaaaa.txt", headers=AUTH)).status == 404


@pytest.mark.parametrize("path", ["/device/v1/items/none.epub", "/device/v1/items/all-aaaaaaaaaa.epub",
                                  "/opds/items/all-aaaaaaaaaa.epub"])
async def test_http_needs_the_device_token(client, store, path):
    add_item(store, "A")
    assert (await client.get(path)).status == 401
    assert store.pending_items()


async def test_http_bad_bundle_ids_are_404(client):
    for path in ("/device/v1/items/all-AAAAAAAAAA.epub", "/device/v1/items/all-aaaa.epub",
                 "/device/v1/items/none.pdf", "/opds/items/none.epub"):
        assert (await client.get(path, headers=AUTH)).status == 404, path


async def test_http_catalog_builds_off_the_event_loop(client, store, monkeypatch):
    seen = []
    real = delivery.catalog_page

    def spy(*a, **k):
        import threading

        seen.append(threading.current_thread() is threading.main_thread())
        return real(*a, **k)

    monkeypatch.setattr(delivery, "catalog_page", spy)
    add_item(store, "A")
    assert (await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)).status == 200
    assert seen == [False]


async def test_http_sleep_delivery_stays_off_by_default(client, store):
    it = add_item(store, "A")
    resp = await client.get("/device/v1/deliver?ev=a-1&ts=1", headers=AUTH)
    assert resp.status == 404 and store.get(it.id).is_pending


# ------------------------------------------------- file names never repeat on the X3


def _fetch_all(store, fmt="epub", *, confirm=False) -> list[str]:
    """Press "Get N new" until nothing is new; the file names the X3 would save."""
    names = []
    for _ in range(20):
        row = _first(store, fmt)
        if row["id"] == "none":
            return names
        assert fetch_download(store, row["id"], fmt) is not None
        names.append(f"{row['title']}.{fmt}")
    raise AssertionError("never ran out of new items")


def test_bundles_split_in_the_same_second_get_different_names(store):
    """Uploaded EPUBs always go alone and big items split, so one email with three EPUB
    attachments makes three "Get 1 new" bundles with the same count and minute: each
    must still get its own file name, or the X3 would keep only the last one."""
    for name in ("Book one", "Book two", "Book three"):
        add_item(store, name, created_at=1_761_480_000, passthrough=True)
    names = _fetch_all(store)
    assert len(names) == 3 and len({n.casefold() for n in names}) == 3, names
    assert names[0].startswith("Get 1 new · ") and names[1].endswith(" (2).epub") and names[2].endswith(" (3).epub")
    for n in names:
        assert device_title(n[:-5]) == n[:-5]


def test_big_items_in_the_same_minute_get_different_names(store):
    add_item(store, "Big A", created_at=1_761_480_000, epub_size=AUTO_CAP_BYTES // 2 + 1000)
    add_item(store, "Big B", created_at=1_761_480_005, epub_size=AUTO_CAP_BYTES // 2 + 1000)
    names = _fetch_all(store)
    assert len(names) == 2 and names[0] != names[1], names


def test_a_cancelled_bundle_is_offered_again_under_a_new_name(store):
    add_item(store, "A", created_at=1_761_480_000)
    first = build_fetch_bundle(store)
    fetch_download(store, first.id, confirm=True, complete=False)  # the X3 got only part of it
    again = build_fetch_bundle(store)
    assert again.id == first.id and again.item_ids == first.item_ids  # the same bundle, same bytes
    # A name that was ever sent is not listed again: had the X3 kept a copy after all,
    # a press could replace it.
    assert again.title == f"{first.title} (2)"
    assert delivery.fetch_lookup(store, first.id).title == again.title


def test_a_received_bundle_waits_for_the_report_then_comes_under_a_new_name(store, monkeypatch):
    """Received whole but not confirmed (the report is still queued on the X3): not
    offered again, so a habitual Confirm cannot replace the book being read; after the
    grace period it comes back under a new name, as a second copy."""
    a = add_item(store, "A", created_at=1_761_480_000)
    first = build_fetch_bundle(store)
    fetch_download(store, first.id, confirm=True)
    row = _first(store)
    assert row["id"] == "none" and "confirmed the next time the X3 joins Wi-Fi" in row["subtitle"]
    assert delivery.awaiting_confirmation(store) == {a.id}
    later = time.time() + delivery.CONFIRM_GRACE_SECONDS + 60
    monkeypatch.setattr(delivery.time, "time", lambda: later)
    again = build_fetch_bundle(store)
    assert again.item_ids == [a.id] and again.id != first.id and again.title == f"{first.title} (2)"
    # Whichever report arrives settles both names.
    fetch_download(store, again.id, confirm=True)
    assert delivery.fetch_confirmed(store, f"/Inbox/{first.title}.epub") == [a.id]
    assert delivery.unconfirmed_fetches(store) == {} and store.kv_prefixed("fetch-sent:") == {}


def test_title_never_reuses_an_item_file_name(store):
    stamp = time.localtime(1_761_480_000)
    taken = f"Get 1 new · {time.strftime('%b', stamp)} {stamp.tm_mday} {stamp.tm_hour:02d}.{stamp.tm_min:02d}"
    add_item(store, taken, created_at=1_761_480_000, delivered={"catalog": 1})
    store.issue_title(taken)
    add_item(store, "Fresh", created_at=1_761_480_000)
    assert _first(store)["title"] == f"{taken} (2)"


# --------------------------------------------------- the X3 confirms saved files


def test_confirm_marks_only_after_the_x3_reports_the_file(store):
    a = add_item(store, "A", created_at=1)
    b = add_item(store, "B", created_at=2)
    row = _first(store)
    assert fetch_download(store, row["id"], confirm=True) is not None
    assert store.get(a.id).is_pending and store.get(b.id).is_pending
    assert set(delivery.unconfirmed_fetches(store)) == {a.id, b.id}
    assert _first(store)["id"] == "none"  # received whole: waits for the report, never re-offered meanwhile
    assert delivery.fetch_confirmed(store, "/Inbox/Some other book.epub") == []
    assert sorted(delivery.fetch_confirmed(store, f"/Inbox/{row['title']}.epub")) == sorted([a.id, b.id])
    assert all("fetch" in store.get(i.id).delivered for i in (a, b))
    assert delivery.unconfirmed_fetches(store) == {}
    assert _first(store)["id"] == "none"
    assert delivery.fetch_confirmed(store, f"/Inbox/{row['title']}.epub") == []  # a repeat changes nothing


def test_confirm_matches_case_insensitively_and_from_the_sd_root(store):
    it = add_item(store, "A", created_at=1)
    row = _first(store)
    fetch_download(store, row["id"], confirm=True)
    # The X3 saves to the SD root when it cannot create the inbox folder; FAT ignores case.
    assert delivery.fetch_confirmed(store, f"/{row['title'].upper()}.EPUB") == [it.id]


def test_sent_title_is_reserved_even_before_confirmation(store):
    add_item(store, "A", created_at=1_761_480_000, passthrough=True)
    add_item(store, "B", created_at=1_761_480_000, passthrough=True)
    first = build_fetch_bundle(store)
    fetch_download(store, first.id, confirm=True)  # no report yet
    assert first.title in store.issued_titles()
    second = build_fetch_bundle(store)  # B alone: same count and minute
    assert second.item_ids != first.item_ids and second.title == f"{first.title} (2)"


# ------------------------------------------------------- format and failure cases


def test_listed_in_one_format_downloaded_in_the_other(store):
    """delivery_format changed without reinstalling the plugin: the X3 asks for the
    other extension. It gets the same items in that format, not a 404."""
    a = add_item(store, "A", created_at=1)
    b = add_item(store, "B", created_at=2)
    row = _first(store, "txt")
    got = delivery.fetch_lookup(store, row["id"], "epub")
    assert got is not None and got.item_ids == [a.id, b.id] and got.title == row["title"]
    assert epub_chapters(got.data) == ["A", "B"]


def test_a_failing_bundle_still_lists_the_items(store, monkeypatch):
    it = add_item(store, "A", created_at=1)

    def full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "save_delivery", full)
    rows = catalog_page(store, 1, 9)["items"]
    assert rows[0] == {"id": "none", "title": delivery.FETCH_FAILED_TITLE, "subtitle": delivery.FETCH_FAILED_SUBTITLE}
    assert [r["id"] for r in rows[1:]] == [it.id]


def test_replaced_bundle_frees_its_file_before_the_new_one_is_written(store, monkeypatch):
    add_item(store, "A", created_at=1)
    build_fetch_bundle(store)
    add_item(store, "B", created_at=2)
    seen = []
    real = store.save_delivery
    monkeypatch.setattr(store, "save_delivery",
                        lambda *a, **k: seen.append(len(list(store.deliveries_dir.iterdir()))) or real(*a, **k))
    build_fetch_bundle(store)
    assert seen == [0]


def test_prune_forgets_titles_and_unconfirmed_sends(store):
    add_item(store, "A", created_at=int(time.time()))
    row = _first(store)
    fetch_download(store, row["id"], confirm=True)
    assert store.kv_prefixed("fetch-title:") and store.kv_prefixed("fetch-sent:")
    store.prune(30, now=int(time.time()) + 31 * 86400)
    assert store.kv_prefixed("fetch-title:") == {} and store.kv_prefixed("fetch-sent:") == {}


# ------------------------------------------------------------- over HTTP: confirm


async def test_http_confirm_flow(client, store):
    a = add_item(store, "A", created_at=1)
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)).json())["items"]
    resp = await client.get(f"/device/v1/items/{rows[0]['id']}.epub?confirm=1", headers=AUTH)
    assert resp.status == 200 and await resp.read() == store.file_bytes(a.id, "epub")
    assert await eventually(lambda: a.id in delivery.unconfirmed_fetches(store))
    assert store.get(a.id).is_pending  # not on the X3 until it says so
    bad = await client.post("/device/v1/fetched", data=b"/Inbox/x.epub")
    assert bad.status == 401
    other = await client.post("/device/v1/fetched", data=b"/Inbox/Something else.epub", headers=AUTH)
    assert other.status == 200 and (await other.json()) == {"ok": True, "confirmed": 0}
    path = f"/Inbox/{rows[0]['title']}.epub".encode()
    ok = await client.post("/device/v1/fetched", data=path,
                           headers={**AUTH, "Content-Type": "text/plain; charset=utf-8"})
    body = await ok.read()
    assert ok.status == 200 and (await ok.json())["confirmed"] == 1
    assert ok.headers["Content-Length"] == str(len(body)) and ok.headers.get("Connection", "").lower() == "close"
    assert "fetch" in store.get(a.id).delivered
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)).json())["items"]
    assert rows[0]["id"] == "none"


async def test_http_confirm_never_fails_on_odd_bodies(client, store):
    for body in (b"", b"\xff\xfe garbage", b"x" * 100_000):
        resp = await client.post("/device/v1/fetched", data=body, headers=AUTH)
        assert resp.status == 200, body[:20]


async def test_http_without_confirm_marks_once_sent(client, store):
    a = add_item(store, "A", created_at=1)
    rows = (await (await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)).json())["items"]
    resp = await client.get(f"/device/v1/items/{rows[0]['id']}.epub", headers=AUTH)  # a 0.1.0 plugin: no confirm
    assert resp.status == 200 and await resp.read()
    assert await eventually(lambda: "fetch" in store.get(a.id).delivered)


async def test_http_catalog_survives_a_failing_bundle(client, store, monkeypatch):
    it = add_item(store, "A", created_at=1)
    monkeypatch.setattr(delivery, "_render_bundle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    resp = await client.get("/device/v1/catalog?page=1&limit=9", headers=AUTH)
    rows = (await resp.json())["items"]
    assert resp.status == 200 and rows[0]["title"] == delivery.FETCH_FAILED_TITLE and rows[1]["id"] == it.id
    basic = {"Authorization": "Basic " + __import__("base64").b64encode(b"x3:device-test-token").decode()}
    feed = await client.get("/opds", headers=basic)
    text = await feed.text()
    assert feed.status == 200 and "all-" not in text and f"urn:sendtox3:{it.id}" in text


@pytest.mark.parametrize("cancel", [False, True], ids=["like-production", "handler-cancellation"])
async def test_http_dropped_download_is_not_marked(opts, store, cancel):
    """The X3 cancels (Back) or loses Wi-Fi mid-download: nothing is marked, so the
    items stay in "Get N new" (also without confirm, e.g. OPDS). Served by an AppRunner
    as in ``__main__`` (aiohttp's test server always cancels handlers on disconnect).

    The book is larger than the test client's receive buffer: macOS loopback sizes that
    at about 400 KB whatever SO_RCVBUF says, and a TCP stack acknowledges what fits in
    its buffer. The X3's lwIP window is about 5.7 KB, so there even a small book is
    mostly unacknowledged when it gives up; ``test_ack_decision`` covers that logic."""
    import asyncio
    import base64
    import socket
    import struct

    from aiohttp import web

    big = add_item(store, "Book", created_at=1, epub_size=12_000_000, passthrough=True)
    runner = web.AppRunner(create_app(opts, store, Processor(store, opts)), handler_cancellation=cancel)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        row = _first(store)
        basic = "Basic " + base64.b64encode(b"x3:device-test-token").decode()
        for path, auth in ((f"/device/v1/items/{row['id']}.epub", "Bearer device-test-token"),
                           (f"/device/v1/items/{row['id']}.epub?confirm=1", "Bearer device-test-token"),
                           (f"/opds/items/{row['id']}.epub", basic)):
            loop = asyncio.get_running_loop()
            # A small receive window, set before connecting, like the X3's lwIP stack: its
            # TCP can only acknowledge what the app has read.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.connect(("127.0.0.1", port))
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: {auth}\r\n\r\n".encode())
            first = await loop.run_in_executor(None, sock.recv, 4096)
            assert first.startswith(b"HTTP/1.1 200"), first[:80]
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()  # RST: the client is gone
            await asyncio.sleep(1.0)
            assert store.get(big.id).is_pending, path
            assert delivery.unconfirmed_fetches(store) == {}, path
    finally:
        await runner.cleanup()


async def test_web_ui_shows_sent_but_not_confirmed(aiohttp_client, opts, store):
    from test_web_admin import login  # the admin tests' sign-in helper

    a = add_item(store, "Sent one", created_at=1)
    row = _first(store)
    fetch_download(store, row["id"], confirm=True)
    client = await aiohttp_client(create_app(opts, store, Processor(store, opts)))
    await login(client)
    html = await (await client.get("/admin/")).text()
    assert "not confirmed yet" in html
    item = await (await client.get(f"/admin/items/{a.id}")).text()
    assert "not confirmed yet" in item


async def test_http_read_all_then_close_counts(opts, store):
    """The X3 reads the whole body and closes at once (its final ACK can ride on the FIN):
    that counts as received, without confirm (marked) and with it (recorded)."""
    import asyncio
    import socket

    from aiohttp import web

    runner = web.AppRunner(create_app(opts, store, Processor(store, opts)))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    def read_all(path: str) -> int:
        sock = socket.create_connection(("127.0.0.1", port))
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer device-test-token\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += sock.recv(65536)
        head, body = buf.split(b"\r\n\r\n", 1)
        length = int(next(ln.split(b":")[1] for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")))
        while len(body) < length:
            body += sock.recv(65536)
        sock.close()  # FIN right after the last byte
        return len(body)

    try:
        loop = asyncio.get_running_loop()
        a = add_item(store, "A", created_at=1)
        row = _first(store)
        assert await loop.run_in_executor(None, read_all, f"/device/v1/items/{row['id']}.epub") > 0
        assert await eventually(lambda: "fetch" in store.get(a.id).delivered)
        b = add_item(store, "B", created_at=2)
        row = _first(store)
        await loop.run_in_executor(None, read_all, f"/device/v1/items/{row['id']}.epub?confirm=1")
        assert await eventually(lambda: b.id in delivery.awaiting_confirmation(store))
    finally:
        await runner.cleanup()


async def test_http_reports_from_other_plugins_are_ignored(client, store):
    a = add_item(store, "A", created_at=1)
    row = _first(store)
    fetch_download(store, row["id"], confirm=True)
    path = f"/Inbox/{row['title']}.epub".encode()
    other = await client.post("/device/v1/fetched?plugin=someone-else", data=path, headers=AUTH)
    assert other.status == 200 and (await other.json())["confirmed"] == 0 and store.get(a.id).is_pending
    ours = await client.post("/device/v1/fetched?plugin=sendtox3", data=path, headers=AUTH)
    assert (await ours.json())["confirmed"] == 1


def test_txt_bundle_sends_an_uploaded_epub_alone(store):
    """So that every bundle can also be built as EPUB from exactly its items (an OPDS
    feed next to a TXT plugin, or a plugin installed with the other format)."""
    note = add_item(store, "Note", created_at=1)
    book = add_item(store, "Book", created_at=2, passthrough=True)
    first = build_fetch_bundle(store, "txt")
    assert first.item_ids == [note.id]
    assert delivery.fetch_lookup(store, first.id, "epub") is not None
    fetch_download(store, first.id, "txt")
    second = build_fetch_bundle(store, "txt")
    assert second.item_ids == [book.id]
    assert delivery.fetch_lookup(store, second.id, "epub").data == store.file_bytes(book.id, "epub")


def test_sleep_delivery_skips_items_awaiting_confirmation(store):
    a = add_item(store, "A", created_at=1)
    fetch_download(store, _first(store)["id"], confirm=True)
    assert delivery.build_auto_delivery(store, "ev", "1") is None  # not sent twice meanwhile
    b = add_item(store, "B", created_at=2)
    data = delivery.build_auto_delivery(store, "ev", "2")
    assert data is not None and "auto" in store.get(b.id).delivered and store.get(a.id).is_pending


def test_nothing_new_row_says_a_fetch_is_awaiting_its_report(store):
    add_item(store, "A", created_at=1)
    fetch_download(store, _first(store)["id"], confirm=True)
    row = _first(store)
    assert row["id"] == "none" and row["title"] == "Nothing new right now"
    assert row["subtitle"].startswith("Your last fetch (") and len(row["subtitle"]) <= 160


def test_a_late_report_for_an_incomplete_send_still_counts(store):
    """The X3 saved it although the server saw the connection end first."""
    a = add_item(store, "A", created_at=1)
    row = _first(store)
    fetch_download(store, row["id"], confirm=True, complete=False)
    assert _first(store)["id"] == row["id"]  # offered again (under a new name)
    assert delivery.fetch_confirmed(store, f"/Inbox/{row['title']}.epub") == [a.id]


def test_a_complete_send_is_never_downgraded_by_a_later_cancel(store):
    a = add_item(store, "A", created_at=1)
    row = _first(store)
    fetch_download(store, row["id"], confirm=True)  # received whole
    fetch_download(store, row["id"], confirm=True, complete=False)  # Confirm again, then Back
    assert a.id in delivery.awaiting_confirmation(store)  # still not offered under that name


class _FakeTransport:
    def __init__(self, unacked):
        self.unacked = list(unacked)  # one value per poll; the last one repeats
        self.closing = False
        self.protocol = None

    def get_extra_info(self, name):
        return None

    def is_closing(self):
        return self.closing

    def get_write_buffer_size(self):
        return 0


@pytest.mark.parametrize("script,expected", [
    (["ack", 0], True),                      # every byte acknowledged
    (["ack", 3000, 3000, "rst"], False),      # gave up with bytes outstanding
    (["ack", 3000, "fin:0"], True),           # read it all, final ACK riding on the FIN
    (["ack", 3000, "fin:2000"], False),       # closed cleanly but had not read it all
    (["ack", None], True),                    # this system cannot say: the empty buffer has to do
])
async def test_ack_decision(monkeypatch, script, expected):
    """``send_whole``'s verdict, from what the kernel reports and how the client leaves."""
    import asyncio

    from sendtox3.web import common

    transport = _FakeTransport([])
    watch = common._CloseWatch(type("Inner", (), {"eof_received": lambda self: None,
                                                   "connection_lost": lambda self, exc: None})(), transport)
    steps = [x for x in script[1:]]

    def unacked(_t):
        step = steps[0] if len(steps) == 1 else steps.pop(0)
        if isinstance(step, str) and step.startswith("fin:"):
            left = int(step[4:])
            monkeypatch.setattr(common, "_unacked_bytes", lambda _t: left)
            watch.eof_received()
            transport.closing = True
            return left if left else 1  # the poll that raced the close saw bytes still out
        if step == "rst":
            watch.connection_lost(ConnectionResetError())
            transport.closing = True
            return 1
        return step

    monkeypatch.setattr(common, "_unacked_bytes", unacked)
    assert await asyncio.wait_for(common._acknowledged(transport, watch, 1000), 5) is expected


# ------------------------------------------- names stay unique in every combination


def test_mixed_formats_never_reuse_a_live_title(store, monkeypatch):
    """A TXT plugin and an OPDS reader (always EPUB), two uploaded EPUBs and a note shared
    in the same minute, a report that comes late: a rebuilt bundle must not take a title
    another stored bundle already has (it would replace that book on the X3)."""
    clock = [1_761_480_000.0]
    monkeypatch.setattr(delivery.time, "time", lambda: clock[0])
    import sendtox3.store as store_mod

    monkeypatch.setattr(store_mod.time, "time", lambda: clock[0])
    m = 1_761_480_000
    add_item(store, "Book one", created_at=m, passthrough=True)
    add_item(store, "Book two", created_at=m, passthrough=True)
    add_item(store, "Note", created_at=m)
    names: dict[str, set[str]] = {}

    def offered(bundle):
        names.setdefault(bundle.filename.rsplit(".", 1)[0].casefold(), set()).add(tuple(bundle.item_ids))

    first = delivery.fetch_lookup(store, _first(store, "txt")["id"], "txt")  # the plugin fetches, report late
    offered(first)
    fetch_download(store, first.id, "txt", confirm=True)
    offered(delivery.fetch_lookup(store, _first(store, "txt")["id"], "txt"))  # listed, not fetched
    clock[0] += delivery.CONFIRM_GRACE_SECONDS + 600
    for _ in range(4):  # the OPDS reader fetches whatever is new
        bundle = delivery.build_fetch_bundle(store, "epub")
        if bundle is None:
            break
        offered(bundle)
        fetch_download(store, bundle.id, "epub")
    assert all(len(contents) == 1 for contents in names.values()), names


def test_requeue_brings_the_items_back_under_a_new_name(store):
    a = add_item(store, "A", created_at=1_761_480_000)
    row = _first(store)
    fetch_download(store, row["id"], confirm=True)
    delivery.fetch_confirmed(store, f"/Inbox/{row['title']}.epub")
    delivery.requeue(store, a.id)
    again = _first(store)
    assert again["title"] == f"{row['title']} (2)"  # never the name of the copy on the X3
    # Without confirm (OPDS) too.
    fetch_download(store, again["id"])
    delivery.requeue(store, a.id)
    assert _first(store)["title"] == f"{row['title']} (3)"


def test_requeue_of_an_item_awaiting_its_report(store):
    """Requeued while a sent bundle waits for its report: the item is new at once, and the
    late report does not mark it delivered again."""
    a = add_item(store, "A", created_at=1)
    b = add_item(store, "B", created_at=2)
    row = _first(store)
    fetch_download(store, row["id"], confirm=True)
    delivery.requeue(store, a.id)
    assert a.id not in delivery.awaiting_confirmation(store) and b.id in delivery.awaiting_confirmation(store)
    assert delivery.build_fetch_bundle(store).item_ids == [a.id]
    assert delivery.fetch_confirmed(store, f"/Inbox/{row['title']}.epub") == [b.id]
    assert store.get(a.id).is_pending


async def test_admin_requeue_uses_the_fetch_aware_requeue(serve_like_main, opts, store):
    import re as _re

    a = add_item(store, "A", created_at=1)
    row = _first(store)
    fetch_download(store, row["id"], confirm=True)
    client = await serve_like_main(create_app(opts, store, Processor(store, opts)))
    html = await (await client.get("/admin?token=admin-test-token")).text()
    csrf = _re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    store.mark_delivered(a.id, "push")  # requeue is offered for delivered items
    resp = await client.post(f"/admin/items/{a.id}/requeue", data={"csrf": csrf})
    assert resp.status == 200
    assert store.get(a.id).is_pending and a.id not in delivery.awaiting_confirmation(store)


@pytest.mark.parametrize("fmt", ["epub", "txt"])
def test_requeue_while_the_report_is_pending_never_reuses_the_name(store, fmt):
    """Requeue ("Deliver again") inside the report window: the bundle was never retired
    by a report, but its name is on the X3, so the next row has a new one."""
    a = add_item(store, "A", created_at=1_761_480_000)
    b = add_item(store, "B", created_at=1_761_480_000)
    row = _first(store, fmt)
    fetch_download(store, row["id"], fmt, confirm=True)
    delivery.requeue(store, a.id)
    delivery.requeue(store, b.id)
    assert delivery.fetch_confirmed(store, f"/Inbox/{row['title']}.{fmt}") == []  # the record went with the requeue
    again = _first(store, fmt)
    assert again["id"] != "none" and again["title"] != row["title"]


def test_settled_records_retire_their_bundles_in_both_formats(store, monkeypatch):
    """OPDS (EPUB) and a TXT plugin list the same item; the TXT copy is fetched, its
    report is lost, a retry name is fetched and reported, then the item is requeued:
    the old name must not come back from the EPUB copy."""
    clock = [1_761_480_000.0]
    monkeypatch.setattr(delivery.time, "time", lambda: clock[0])
    a = add_item(store, "A", created_at=1_761_480_000)
    first = _first(store, "txt")
    assert delivery.build_fetch_bundle(store, "epub").title == first["title"]
    fetch_download(store, first["id"], "txt", confirm=True)  # its report is lost
    clock[0] += delivery.CONFIRM_GRACE_SECONDS + 60
    retry = _first(store, "txt")
    fetch_download(store, retry["id"], "txt", confirm=True)
    assert delivery.fetch_confirmed(store, f"/Inbox/{retry['title']}.txt") == [a.id]
    delivery.requeue(store, a.id)
    names = {_first(store, "txt")["title"], delivery.build_fetch_bundle(store, "epub").title}
    assert first["title"] not in names and retry["title"] not in names


def test_a_retired_id_never_resolves_again(store):
    """After a Requeue the same items get a new id: a stale row on another client (or a
    second X3) cannot resolve to different items under its old file name."""
    a = add_item(store, "A", created_at=1)
    b = add_item(store, "B", created_at=2)
    row = _first(store, "txt")
    fetch_download(store, row["id"], "txt")  # received, retired
    delivery.requeue(store, a.id)
    delivery.requeue(store, b.id)
    again = delivery.build_fetch_bundle(store, "epub")
    assert again.id != row["id"] and again.title != row["title"]
    assert delivery.fetch_lookup(store, row["id"], "txt") is None  # the old row: Download failed
    assert store.kv_prefixed("fetch-retired:")
    store.prune(30, now=int(time.time()) + 31 * 86400)
    assert store.kv_prefixed("fetch-retired:") == {}


async def test_a_share_cannot_take_the_name_of_a_listed_bundle(store, opts):
    """Someone shares a note titled exactly like the "Get N new" row on screen: the note
    gets another name, so downloading it never replaces the bundle's file."""
    from sendtox3.models import IngestRequest

    add_item(store, "A", created_at=1)
    row = _first(store)
    processor = Processor(store, opts)
    item = await processor.process(IngestRequest(kind="text", source="test", text="Copied text", title=row["title"]))
    assert item.title.casefold() != row["title"].casefold()


def test_a_listing_renames_a_bundle_whose_name_an_item_took(store):
    a = add_item(store, "A", created_at=1)
    row = _first(store)
    store.update_title(a.id, row["title"])  # renamed in the web UI to the row's text
    assert delivery.build_fetch_bundle(store).title != row["title"]


def test_a_report_under_an_earlier_name_of_the_bundle_counts(store):
    """Another client's listing renamed the bundle while the X3 still showed the old row:
    the X3 saves and reports the old name, which must still settle the items."""
    a = add_item(store, "A", created_at=1)
    old = _first(store, "txt")  # the X3's row
    opds = delivery.build_fetch_bundle(store, "epub")
    fetch_download(store, opds.id, "epub", complete=False)  # an OPDS client starts and drops it
    renamed = delivery.build_fetch_bundle(store, "epub")
    assert renamed.id == old["id"] and renamed.title != old["title"]
    fetch_download(store, old["id"], "txt", confirm=True)  # the X3 presses its stale row
    assert delivery.fetch_confirmed(store, f"/Inbox/{old['title']}.txt") == [a.id]
    assert delivery.unconfirmed_fetches(store) == {} and store.kv_prefixed("fetch-sent:") == {}
