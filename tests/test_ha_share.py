"""HA share worker: event mapping, and the websocket client against a fake Home Assistant."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from fakes.ha_ws import FakeHomeAssistant
from sendtox3.config import Options
from sendtox3.models import IngestRequest, Item, new_item_id
from sendtox3.workers import ha_share
from sendtox3.workers.ha_share import device_names_from_registry, share_event_to_request

TOKEN = "supervisor-test-token"


# ----------------------------------------------------------------------- mapping


@pytest.mark.parametrize(
    "data, expect",
    [
        # iOS, Safari: URL, share-sheet box pre-filled with the page title (or the user's
        # comment): sent as "comment + link", the Processor compares it with the page title
        ({"url": "https://example.org/a", "entered": "A page title", "sourceDeviceName": "Qiong's iPhone",
          "sourceDeviceID": "iphone", "sourceDevicePermanentID": "X"},
         dict(url="", text="A page title\n\nhttps://example.org/a", title="", sender="Qiong's iPhone")),
        # iOS, selected text: the box is pre-filled with the same text
        ({"text": "Some selected text.", "entered": "Some selected text.", "sourceDeviceName": "iPad"},
         dict(url="", text="Some selected text.", title="", sender="iPad")),
        # iOS, the user typed a note and shared nothing else
        ({"entered": "Remember to buy milk", "sourceDeviceName": "iPhone"},
         dict(url="", text="Remember to buy milk", title="", sender="iPhone")),
        # iOS, a long comment typed next to a URL is kept as text
        ({"url": "https://example.org/a", "entered": "line one\nline two"},
         dict(url="https://example.org/a", text="line one\nline two", title="")),
        # iOS, a comment added to shared text
        ({"text": "Quoted passage.", "entered": "My thoughts on this\nare long"},
         dict(text="My thoughts on this\nare long\n\nQuoted passage.", title="")),
        # Android, Chrome: URL + page title as subject
        ({"caller": "android-app://com.android.chrome", "subject": "Web Page Title",
          "url": "https://www.example.com/x", "device_id": "abc123"},
         dict(url="https://www.example.com/x", text="", title="Web Page Title", sender="Pixel 8")),
        # Android, text with a link inside
        ({"caller": "android-app://com.android.chrome", "text": '"a quote"\nhttps://example.com/p',
          "device_id": "unknown"},
         dict(url="", text='"a quote"\nhttps://example.com/p', title="", sender="")),
        # url and text identical
        ({"url": "https://e.org/", "text": "https://e.org/"}, dict(url="https://e.org/", text="")),
        # a "url" that is not one becomes text
        ({"url": "just some words"}, dict(url="", text="just some words")),
        # whitespace is trimmed; non-string junk ignored
        ({"url": "  https://e.org/z \n", "text": None, "subject": ["x"], "entered": 5},
         dict(url="", text="5\n\nhttps://e.org/z", title="")),
    ],
)
def test_share_event_to_request(data, expect):
    req = share_event_to_request(data, device_names={"abc123": "Pixel 8"}, received_at=123)
    assert isinstance(req, IngestRequest)
    assert req.source == "ha_share" and req.kind == "auto" and req.received_at == 123
    for k, v in expect.items():
        assert getattr(req, k) == v, k


@pytest.mark.parametrize("data", [{}, {"url": "", "text": "  "}, {"caller": "x", "device_id": "y"}, "nope", None])
def test_share_event_without_content(data):
    assert share_event_to_request(data) is None


def test_device_names_from_registry():
    devices = [
        {"id": "reg1", "name": "Pixel 8", "name_by_user": None, "identifiers": [["mobile_app", "abc123"]]},
        {"id": "reg2", "name": "iPhone", "name_by_user": "Qiong's iPhone", "identifiers": [["mobile_app", "ios-1"]]},
        {"id": "reg3", "name": "Hue bridge", "identifiers": [["hue", "001788"]]},
        {"id": "reg4", "name": "", "identifiers": [["mobile_app", "nameless"]]},
        "garbage",
    ]
    names = device_names_from_registry(devices)
    assert names["abc123"] == "Pixel 8" and names["reg1"] == "Pixel 8"
    assert names["ios-1"] == "Qiong's iPhone"
    assert names["reg3"] == "Hue bridge" and "001788" not in names and "nameless" not in names
    assert device_names_from_registry(None) == {}


# ------------------------------------------------------------------------ worker


class FakeProcessor:
    def __init__(self, *, fail: int = 0, delay: float = 0.0):
        self.requests: list[IngestRequest] = []
        self.fail = fail
        self.delay = delay

    async def process(self, req: IngestRequest) -> Item:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.requests.append(req)
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("extractor exploded")
        return Item(id=new_item_id(), created_at=req.received_at, kind="url" if req.url else "text",
                    source=req.source, title=req.title or "Shared note")


@dataclass
class State:
    opts: Options
    processor: Any
    status: dict[str, Any] = field(default_factory=dict)


@pytest.fixture(autouse=True)
def fast_worker(monkeypatch):
    monkeypatch.setattr(ha_share, "BACKOFF_INITIAL", 0.05)
    monkeypatch.setattr(ha_share, "BACKOFF_MAX", 0.2)
    monkeypatch.setattr(ha_share, "AUTH_BACKOFF_MIN", 0.1)
    monkeypatch.setattr(ha_share, "HANDSHAKE_TIMEOUT", 2.0)


@pytest.fixture
async def ha(monkeypatch):
    devices = [{"id": "reg1", "name": "Pixel 8", "name_by_user": None, "identifiers": [["mobile_app", "abc123"]]}]
    async with FakeHomeAssistant(token=TOKEN, devices=devices) as fake:
        monkeypatch.setenv("SENDTOX3_HA_WS_URL", fake.url)
        monkeypatch.setenv("SUPERVISOR_TOKEN", TOKEN)
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)
        yield fake


def make_state(opts: Options, processor: Any = None) -> State:
    opts.ha_share_enabled = True
    return State(opts=opts, processor=processor if processor is not None else FakeProcessor())


async def wait_until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def running(state: State):
    task = asyncio.create_task(ha_share.run(state))
    try:
        await wait_until(lambda: "ha_share" in state.status or task.done())
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_disabled_by_option(opts, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", TOKEN)
    state = State(opts=opts, processor=FakeProcessor())
    opts.ha_share_enabled = False
    await asyncio.wait_for(ha_share.run(state), 1)
    assert state.status["ha_share"]["enabled"] is False
    assert "disabled" in state.status["ha_share"]["reason"]
    assert state.status["ha_share"]["state"] == "disabled"


async def test_disabled_outside_home_assistant(opts, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    state = make_state(opts)
    await asyncio.wait_for(ha_share.run(state), 1)
    st = state.status["ha_share"]
    assert st["enabled"] is False and "SUPERVISOR_TOKEN" in st["reason"]
    assert st["url"] == ha_share.DEFAULT_WS_URL


async def test_ios_share_reaches_the_processor(ha, opts):
    state = make_state(opts)
    async with running(state):
        await ha.wait_subscribed()
        st = state.status["ha_share"]
        await wait_until(lambda: st["connected"])
        assert st["ha_version"] == "2026.10.4" and st["last_connected"]
        assert st["state"] == "ok" and "Listening" in st["detail"]
        sent = await ha.fire_event("mobile_app.share", {
            "url": "https://example.org/article", "entered": "An article", "sourceDeviceName": "Qiong's iPhone",
            "sourceDeviceID": "iphone_15", "sourceDevicePermanentID": "PERM",
        })
        assert sent == 1
        await wait_until(lambda: st["items_created"] == 1)
    req = state.processor.requests[0]
    assert (req.source, req.url, req.text, req.title, req.sender) == (
        "ha_share", "", "An article\n\nhttps://example.org/article", "", "Qiong's iPhone")
    assert st["events_received"] == 1 and st["last_item"]["title"] == "Shared note"
    assert st["connected"] is False  # after cancellation
    assert ha.protocol_errors == []
    first = ha.received[0]
    assert first["type"] == "subscribe_events" and first["event_type"] == "mobile_app.share"


async def test_android_share_resolves_device_name(ha, opts):
    state = make_state(opts)
    async with running(state):
        await ha.wait_subscribed()
        await wait_until(lambda: any(m.get("type") == "config/device_registry/list" for m in ha.received))
        await asyncio.sleep(0.05)  # let the registry result arrive
        await ha.fire_event("mobile_app.share", {
            "caller": "android-app://com.android.chrome", "subject": "Title from Chrome",
            "url": "https://example.com/x", "device_id": "abc123",
        }, coalesce=True)
        await wait_until(lambda: state.processor.requests)
    req = state.processor.requests[0]
    assert (req.url, req.title, req.sender) == ("https://example.com/x", "Title from Chrome", "Pixel 8")


async def test_other_events_and_empty_shares_are_ignored(ha, opts):
    state = make_state(opts)
    async with running(state):
        await ha.wait_subscribed()
        assert await ha.fire_event("state_changed", {"entity_id": "light.x"}) == 0  # not subscribed
        await ha.fire_event("mobile_app.share", {"caller": "android-app://x", "device_id": "abc123"})
        await ha.fire_event("mobile_app.share", {"text": "real one"})
        st = state.status["ha_share"]
        await wait_until(lambda: st["items_created"] == 1)
    assert st["ignored"] == 1 and st["events_received"] == 2
    assert [r.text for r in state.processor.requests] == ["real one"]


async def test_auth_failure_retries_until_token_works(ha, opts, monkeypatch):
    ha.token = "the-real-token"
    state = make_state(opts)
    async with running(state) as task:
        st = state.status["ha_share"]
        await wait_until(lambda: ha.auth_failures >= 2)
        assert not task.done() and st["connected"] is False
        assert "rejected the token" in st["last_error"]
        assert st["reconnects"] >= 2
        assert st["state"] == "error" and "rejected the token" in st["detail"]
        ha.token = TOKEN  # e.g. the Supervisor re-issued the token
        await ha.wait_subscribed()
        await wait_until(lambda: st["connected"])
        await ha.fire_event("mobile_app.share", {"text": "after auth fix"})
        await wait_until(lambda: state.processor.requests)
    assert state.processor.requests[0].text == "after auth fix"


async def test_reconnects_after_home_assistant_drops_the_socket(ha, opts):
    state = make_state(opts)
    async with running(state):
        await ha.wait_subscribed()
        st = state.status["ha_share"]
        await ha.drop_connections()
        await wait_until(lambda: ha.connections_total >= 2)
        await ha.wait_subscribed()
        await wait_until(lambda: st["connected"])
        assert st["reconnects"] >= 1 and "closed" in st["last_error"]
        await ha.fire_event("mobile_app.share", {"url": "https://example.org/after"})
        await wait_until(lambda: state.processor.requests)
    assert state.processor.requests[0].url == "https://example.org/after"


async def test_reconnects_after_home_assistant_restart(ha, opts):
    state = make_state(opts)
    async with running(state):
        await ha.wait_subscribed()
        st = state.status["ha_share"]
        port = ha.port
        await ha.stop()
        await wait_until(lambda: st["reconnects"] >= 3)  # connection refused, backing off
        assert st["connected"] is False and "connection failed" in st["last_error"]
        assert st["state"] == "warning" and "retrying in" in st["detail"]
        await ha.start(port)
        await ha.wait_subscribed(timeout=5)
        await ha.fire_event("mobile_app.share", {"text": "back"})
        await wait_until(lambda: state.processor.requests)


async def test_dead_connection_is_detected_by_ping(ha, opts, monkeypatch):
    monkeypatch.setattr(ha_share, "PING_INTERVAL", 0.1)
    monkeypatch.setattr(ha_share, "PONG_TIMEOUT", 0.1)
    ha.answer_pings = False
    state = make_state(opts)
    async with running(state):
        await wait_until(lambda: ha.connections_total >= 2)
        assert "stopped answering pings" in state.status["ha_share"]["last_error"]


async def test_answered_pings_keep_the_connection(ha, opts, monkeypatch):
    monkeypatch.setattr(ha_share, "PING_INTERVAL", 0.05)
    monkeypatch.setattr(ha_share, "PONG_TIMEOUT", 0.5)
    state = make_state(opts)
    async with running(state):
        await ha.wait_subscribed()
        await wait_until(lambda: ha.pings >= 4)
    assert ha.connections_total == 1 and ha.protocol_errors == []


async def test_slow_processing_does_not_stall_the_socket(ha, opts, monkeypatch):
    monkeypatch.setattr(ha_share, "PING_INTERVAL", 0.05)
    monkeypatch.setattr(ha_share, "PONG_TIMEOUT", 1.0)
    processor = FakeProcessor(delay=0.4)
    state = make_state(opts, processor)
    async with running(state):
        await ha.wait_subscribed()
        for n in range(3):
            await ha.fire_event("mobile_app.share", {"text": f"note {n}"})
        await wait_until(lambda: len(processor.requests) == 3)
    assert ha.connections_total == 1 and ha.pings >= 2
    assert [r.text for r in processor.requests] == ["note 0", "note 1", "note 2"]


async def test_processor_failure_does_not_kill_the_worker(ha, opts):
    processor = FakeProcessor(fail=1)
    state = make_state(opts, processor)
    async with running(state) as task:
        await ha.wait_subscribed()
        await ha.fire_event("mobile_app.share", {"text": "first"})
        st = state.status["ha_share"]
        await wait_until(lambda: "processing failed" in st["last_error"])
        await ha.fire_event("mobile_app.share", {"text": "second"})
        await wait_until(lambda: st["items_created"] == 1)
        assert not task.done()
    assert [r.text for r in processor.requests] == ["first", "second"]


async def test_failed_subscription_reconnects(ha, opts, monkeypatch):
    original = ha._command
    calls = {"n": 0}

    async def refuse_first(ws, mid, m):
        if m.get("type") == "subscribe_events" and calls["n"] == 0:
            calls["n"] += 1
            await ws.send_json({"id": mid, "type": "result", "success": False,
                                "error": {"code": "unauthorized", "message": "Unauthorized"}})
            return
        await original(ws, mid, m)

    monkeypatch.setattr(ha, "_command", refuse_first)
    state = make_state(opts)
    async with running(state):
        await ha.wait_subscribed()
        await wait_until(lambda: state.status["ha_share"]["connected"])
    assert "subscribe_events failed" in state.status["ha_share"]["last_error"]
    assert ha.connections_total == 2


async def test_status_matches_app_state_contract(ha, opts):
    app_state = pytest.importorskip("sendtox3.app_state")
    state = make_state(opts)
    async with running(state):
        await ha.wait_subscribed()
        await ha.fire_event("mobile_app.share", {"text": "hello"})
        await wait_until(lambda: state.status["ha_share"]["items_created"] == 1)
        shown = app_state.normalize_status(state.status["ha_share"])
    assert shown["state"] == "ok" and "1 share(s) queued" in shown["detail"] and shown["updated"] > 0
