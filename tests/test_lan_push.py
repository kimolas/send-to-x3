"""LAN push worker end to end: real Store, fake CrossPoint device (1.6.5 and 2.0)."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from fakes.crosspoint_server import FakeCrossPoint
from sendtox3.config import Options
from sendtox3.device_client import DeviceClient
from sendtox3.models import Item, new_item_id
from sendtox3.store import Store
from sendtox3.workers import lan_push


@dataclass
class State:
    opts: Options
    store: Store
    status: dict[str, Any] = field(default_factory=dict)


@pytest.fixture(autouse=True)
def fast_worker(monkeypatch):
    monkeypatch.setattr(lan_push, "MIN_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(lan_push, "ITEM_GAP_SECONDS", 0.0)
    monkeypatch.setattr(lan_push, "STATUS_TIMEOUT", 1.0)


@pytest.fixture(params=["1.6.5", "2.0"])
async def device(request, tmp_path):
    async with FakeCrossPoint(tmp_path / "sd", version=request.param) as dev:
        yield dev


def make_state(opts: Options, store: Store, host: str, **overrides: Any) -> State:
    opts.device_host = host
    opts.push_poll_seconds = 0
    for k, v in overrides.items():
        setattr(opts, k, v)
    return State(opts=opts, store=store)


def add(store: Store, title: str, created_at: int, *, epub: bytes | None = None, txt: bytes | None = None) -> Item:
    item = Item(id=new_item_id(), created_at=created_at, kind="text", source="test", title=title)
    return store.add_item(item, epub=epub if epub is not None else f"EPUB:{title}".encode(),
                          txt=txt if txt is not None else f"TXT:{title}\n".encode())


async def wait_until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def running(state: State):
    task = asyncio.create_task(lan_push.run(state))
    try:
        await wait_until(lambda: "lan_push" in state.status or task.done())
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def uploads(dev: FakeCrossPoint) -> int:
    return len(dev.requests_to("POST", "/upload"))


# --------------------------------------------------------------------------- tests


async def test_disabled_without_device_host(opts, store):
    state = State(opts=opts, store=store)
    opts.device_host = "  "
    await asyncio.wait_for(lan_push.run(state), 1)
    assert state.status["lan_push"]["enabled"] is False
    assert state.status["lan_push"]["state"] == "disabled"


async def test_invalid_device_host_idles_with_error(opts, store):
    state = State(opts=opts, store=store)
    opts.device_host = "https://192.168.1.5"
    async with running(state) as task:
        st = state.status["lan_push"]
        await wait_until(lambda: st["state"] == "error")
        await asyncio.sleep(0.1)
        assert not task.done()  # does not return (the supervisor would restart it in a loop)
    assert st["enabled"] is False and "invalid device_host" in st["last_error"]
    assert "Invalid device_host" in st["detail"]


async def test_pushes_all_pending_items(device, opts, store):
    a = add(store, "First article", 1)
    b = add(store, "Second – “quoted” note", 2)
    c = add(store, "Third", 3)
    other = add(store, "Already read", 4)
    store.mark_delivered(other.id, "opds")
    state = make_state(opts, store, device.host)
    async with running(state):
        await wait_until(lambda: not store.pending_items())
        st = state.status["lan_push"]
        await wait_until(lambda: st["pushed_count"] == 3)
    assert device.listdir("/Inbox") == sorted(
        ["First article.epub", "Second – “quoted” note.epub", "Third.epub"])
    assert device.read("/Inbox/First article.epub") == b"EPUB:First article"
    for item in (a, b, c):
        assert set(store.get(item.id).delivered) == {"push"}
    assert store.get(other.id).delivered.keys() == {"opds"}  # delivered elsewhere: not pushed
    assert uploads(device) == 3
    assert st["online"] is False  # the worker was cancelled
    assert st["last_pushed"] == "/Inbox/Third.epub" and st["last_push"]
    assert st["device"] == "X3" and st["device_version"] == device.version_string
    assert st["last_error"] == ""
    assert st["state"] == "ok" and "last delivered /Inbox/Third.epub" in st["detail"]
    assert device.library_dirty
    assert device.max_in_flight == 1


async def test_txt_format_and_custom_folder(device, opts, store):
    add(store, "Plain", 1, txt=b"hello\n")
    state = make_state(opts, store, device.host, delivery_format="txt", push_folder="/Reading/Shared")
    async with running(state):
        await wait_until(lambda: not store.pending_items())
    assert device.read("/Reading/Shared/Plain.txt") == b"hello\n"


async def test_name_collisions(device, opts, store):
    device.write("/Inbox/Report.epub", b"someone else's book")
    device.write("/Inbox/report (2).epub", b"another one")  # same size as our bytes, other content
    item = add(store, "Report", 1)
    state = make_state(opts, store, device.host)
    async with running(state):
        await wait_until(lambda: not store.pending_items())
    assert device.read("/Inbox/Report.epub") == b"someone else's book"  # never overwritten
    assert device.read("/Inbox/Report (3).epub") == b"EPUB:Report"
    assert state.status["lan_push"]["last_pushed"] == "/Inbox/Report (3).epub"
    assert store.get(item.id).delivered.keys() == {"push"}


async def test_no_double_push_across_polls_and_restarts(device, opts, store):
    add(store, "One", 1)
    add(store, "Two", 2)
    state = make_state(opts, store, device.host)
    async with running(state):
        await wait_until(lambda: not store.pending_items())
        polls = len(device.requests_to("GET", "/api/status"))
        await wait_until(lambda: len(device.requests_to("GET", "/api/status")) >= polls + 5)
    async with running(make_state(opts, store, device.host)):  # a second worker / app restart
        await wait_until(lambda: len(device.requests_to("GET", "/api/status")) >= polls + 10)
    assert uploads(device) == 2
    assert device.listdir("/Inbox") == ["One.epub", "Two.epub"]


async def test_identical_copy_is_marked_without_upload(device, opts, store):
    """Bookkeeping lost after an upload (crash), or a requeued item: no second copy."""
    item = add(store, "Saved", 1)
    device.write("/Inbox/Saved.epub", b"EPUB:Saved")
    requeued = add(store, "Requeued", 2)
    device.write("/Inbox/Requeued (2).epub", b"EPUB:Requeued")
    device.write("/Inbox/Requeued.epub", b"a different book with the same title")
    state = make_state(opts, store, device.host)
    async with running(state):
        await wait_until(lambda: not store.pending_items())
    assert uploads(device) == 0
    assert store.get(item.id).delivered.keys() == {"push"}
    assert store.get(requeued.id).delivered.keys() == {"push"}
    assert state.status["lan_push"]["pushed_count"] == 2


async def test_device_appears_later(device, opts, store):
    device.go_offline()
    add(store, "Waiting", 1)
    state = make_state(opts, store, device.host)
    async with running(state):
        st = state.status["lan_push"]
        await asyncio.sleep(0.3)
        assert st["online"] is False and st["last_seen"] is None and store.pending_items()
        assert st["state"] == "waiting" and "open File Transfer" in st["detail"]
        await device.go_online()
        await wait_until(lambda: not store.pending_items())
        assert st["online"] is True and st["last_seen"]
        await wait_until(lambda: st["state"] == "ok")
    assert device.listdir("/Inbox") == ["Waiting.epub"]


async def test_new_item_while_device_stays_online(device, opts, store):
    add(store, "Early", 1)
    state = make_state(opts, store, device.host)
    async with running(state):
        await wait_until(lambda: not store.pending_items())
        add(store, "Late", 2)
        await wait_until(lambda: not store.pending_items())
    assert device.listdir("/Inbox") == ["Early.epub", "Late.epub"]
    assert uploads(device) == 2


async def test_device_vanishes_mid_push_then_returns(device, opts, store):
    items = [add(store, f"Item {n}", n) for n in range(1, 5)]
    device.vanish_after_uploads = 2  # the user leaves File Transfer during the third upload
    state = make_state(opts, store, device.host)
    async with running(state):
        st = state.status["lan_push"]
        await wait_until(lambda: not device.online)
        await wait_until(lambda: st["online"] is False and st["pushed_count"] == 2)
        await asyncio.sleep(0.2)
        assert [i.id for i in store.pending_items()] == [items[2].id, items[3].id]
        assert "did not answer" in st["last_error"]
        assert st["state"] == "waiting" and "2 item(s) wait" in st["detail"]
        await device.go_online()
        await wait_until(lambda: not store.pending_items())
        await wait_until(lambda: st["pushed_count"] == 4)
    assert device.listdir("/Inbox") == [f"Item {n}.epub" for n in range(1, 5)]
    assert uploads(device) == 5  # 4 stored + the one that hit the vanished device


async def test_connection_lost_after_store_is_resolved(device, opts, store):
    item = add(store, "Fragile", 1)
    device.inject("POST", "/upload", reset_after=True)
    state = make_state(opts, store, device.host)
    async with running(state):
        await wait_until(lambda: not store.pending_items())
    assert device.listdir("/Inbox") == ["Fragile.epub"]
    assert store.get(item.id).delivered.keys() == {"push"}


async def test_refused_item_does_not_block_others_or_spin(device, opts, store):
    bad = add(store, "Refused", 1)
    good = add(store, "Fine", 2)
    device.inject("POST", "/upload", status=400, body="Failed to create file on SD card")
    state = make_state(opts, store, device.host)
    async with running(state):
        await wait_until(lambda: store.get(good.id).delivered)
        polls = len(device.requests_to("GET", "/api/status"))
        await wait_until(lambda: len(device.requests_to("GET", "/api/status")) >= polls + 5)
        st = state.status["lan_push"]
        assert bad.id in st["last_error"] and "Failed to create file" in st["last_error"]
        assert st["state"] == "warning" and "1 item(s) not delivered" in st["detail"]
        assert uploads(device) == 2  # the refused item is not retried in this session
        # Leaving and re-entering File Transfer retries it.
        device.go_offline()
        await wait_until(lambda: st["online"] is False)
        await device.go_online()
        await wait_until(lambda: not store.pending_items())
    assert device.listdir("/Inbox") == ["Fine.epub", "Refused.epub"]


async def test_disk_full_stops_the_batch(device, opts, store):
    add(store, "A", 1)
    add(store, "B", 2)
    device.disk_full = True
    state = make_state(opts, store, device.host)
    async with running(state):
        st = state.status["lan_push"]
        await wait_until(lambda: "storage problem" in st["last_error"])
        await asyncio.sleep(0.1)
    assert len(store.pending_items()) == 2
    assert device.listdir("/Inbox") == []  # no truncated files left behind


async def test_missing_item_file_is_skipped(device, opts, store):
    broken = add(store, "Broken", 1)
    ok = add(store, "Okay", 2)
    store.file_path(broken.id, "epub").unlink()
    state = make_state(opts, store, device.host)
    async with running(state):
        await wait_until(lambda: store.get(ok.id).delivered)
        st = state.status["lan_push"]
        await wait_until(lambda: broken.id in st["last_error"])
    assert [i.id for i in store.pending_items()] == [broken.id]
    assert device.listdir("/Inbox") == ["Okay.epub"]


async def test_unsafe_title_is_sanitized(device, opts, store):
    item = add(store, 'a/b: c? "d" <e> | f*.', 1)
    state = make_state(opts, store, device.host)
    async with running(state):
        await wait_until(lambda: not store.pending_items())
    assert device.listdir("/Inbox") == ["a-b - c 'd' e - f.epub"]
    assert store.get(item.id).delivered.keys() == {"push"}


async def test_folder_cannot_be_created(device, opts, store):
    add(store, "X", 1)
    device.write("/Inbox", b"a file where the folder should be")
    state = make_state(opts, store, device.host)
    async with running(state):
        st = state.status["lan_push"]
        await wait_until(lambda: "could not create /Inbox" in st["last_error"])
        await wait_until(lambda: st["state"] == "error")
    assert store.pending_items()


async def test_worker_survives_unexpected_errors(device, opts, store, monkeypatch):
    add(store, "X", 1)
    state = make_state(opts, store, device.host)
    calls = {"n": 0}
    real = store.pending_items

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real()

    monkeypatch.setattr(store, "pending_items", flaky)
    async with running(state) as task:
        await wait_until(lambda: not real())
        assert not task.done()
    assert "database is locked" in state.status["lan_push"]["last_error"]


async def test_push_items_directly(device, store):
    """push_items is reusable (e.g. an admin "push now" button)."""
    a = add(store, "Direct", 1)
    async with DeviceClient(device.host) as client:
        result = await lan_push.push_items(client, store, [a], folder="/Inbox", fmt="epub")
        assert result.delivered == [(a.id, "/Inbox/Direct.epub")] and result.uploaded == 1
        again = await lan_push.push_items(client, store, [a], folder="/Inbox", fmt="epub")
        assert again.skipped == [a.id] and not again.delivered


async def test_status_matches_app_state_contract(device, opts, store):
    """The admin UI reads status through app_state.normalize_status."""
    app_state = pytest.importorskip("sendtox3.app_state")
    add(store, "Shown", 1)
    state = make_state(opts, store, device.host)
    async with running(state):
        await wait_until(lambda: not store.pending_items())
        await wait_until(lambda: state.status["lan_push"]["state"] == "ok")
        shown = app_state.normalize_status(state.status["lan_push"])
    assert shown["state"] == "ok" and "Shown.epub" in shown["detail"] and shown["updated"] > 0
