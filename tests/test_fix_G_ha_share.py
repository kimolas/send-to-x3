"""Regression tests for the HA share worker: an edited iOS share-sheet box is kept,
reconnects stay quick after Home Assistant restarts, and storage failures are retried."""

from __future__ import annotations

import asyncio
import socket

import pytest
from sendtox3 import ingest
from sendtox3.ingest import Processor, StorageError
from sendtox3.models import IngestRequest, Item, new_item_id
from sendtox3.workers import ha_share
from sendtox3.workers.ha_share import share_event_to_request
from test_ha_share import TOKEN, FakeProcessor, make_state, running, wait_until
from test_web_fakes import FakeWeb

#: The module's real timing, before test_ha_share's autouse fixture shrinks it.
REAL_TIMING = {name: getattr(ha_share, name) for name in
               ("BACKOFF_INITIAL", "BACKOFF_MAX", "AUTH_BACKOFF_MIN", "AUTH_BACKOFF_MAX")}

# ------------------------------------------------------- 33: the edited iOS share box


@pytest.mark.parametrize("entered", ["Read before Monday’s meeting", "A page title"])
def test_ios_box_with_a_link_is_sent_as_comment_and_link(entered):
    req = share_event_to_request({"url": "https://example.com/a", "entered": entered, "sourceDeviceName": "iPhone"})
    assert (req.url, req.text, req.title) == ("", f"{entered}\n\nhttps://example.com/a", "")


@pytest.mark.parametrize("url", ["https://example.com/a.", "https://example.com/it's"])
def test_ios_box_with_a_link_that_would_not_survive_in_text_keeps_the_url(url):
    req = share_event_to_request({"url": url, "entered": "My note"})
    assert (req.url, req.text, req.title) == (url, "My note", "")


def test_android_subject_is_still_the_title():
    req = share_event_to_request({"url": "https://example.com/a", "subject": "Page title"})
    assert (req.url, req.text, req.title) == ("https://example.com/a", "", "Page title")


@pytest.fixture
def web(monkeypatch) -> FakeWeb:
    """Only the fetch is faked: classification, text extraction and rendering are real."""
    fake = FakeWeb()
    monkeypatch.setattr(ingest, "url_to_document", fake)
    return fake


async def test_edited_box_reaches_the_x3(store, opts, web):
    """The words the user typed are in what the X3 gets, even though the page has a title."""
    web.add("https://example.com/a", "Quarterly Results Explained",
            "The article body, with enough words in it to read as an article on the device.")
    req = share_event_to_request({"url": "https://example.com/a", "entered": "Read before Monday’s meeting"})
    item = await Processor(store, opts).process(req)
    assert item.status == "ready" and item.source_url == "https://example.com/a"
    assert "Read before Monday’s meeting" in store.file_bytes(item.id, "txt").decode("utf-8")


async def test_unedited_box_is_the_article(store, opts, web):
    web.add("https://example.com/b", "Why Paper Still Wins",
            "The article body, with enough words in it to read as an article on the device.")
    req = share_event_to_request({"url": "https://example.com/b", "entered": "Why Paper Still Wins"})
    item = await Processor(store, opts).process(req)
    assert (item.kind, item.status, item.title) == ("url", "ready", "Why Paper Still Wins")
    assert web.calls == ["https://example.com/b"]
    assert "The article body" in store.file_bytes(item.id, "txt").decode("utf-8")


# --------------------------------------------- 36: quick reconnects after Core restarts


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    """Records the worker's reconnect waits (any sleep of 1 s or more) without waiting."""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        if delay >= 1:
            recorded.append(delay)
            delay = 0
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ha_share.random, "uniform", lambda a, b: 1.0)
    for name, value in REAL_TIMING.items():
        monkeypatch.setattr(ha_share, name, value)
    return recorded


async def test_reconnect_waits_stay_short_while_home_assistant_is_down(opts, monkeypatch, waits):
    monkeypatch.setenv("SENDTOX3_HA_WS_URL", f"ws://127.0.0.1:{_closed_port()}/api/websocket")
    monkeypatch.setenv("SUPERVISOR_TOKEN", TOKEN)
    state = make_state(opts)
    async with running(state):
        await wait_until(lambda: len(waits) >= 10)
    assert waits[:4] == [2.0, 4.0, 5.0, 5.0]
    assert max(waits) <= 10.0  # was 120 s: shares sent right after Core came back were lost
    assert "connection failed" in state.status["ha_share"]["last_error"]


async def test_a_rejected_token_still_backs_off_slowly(opts, monkeypatch, waits):
    from fakes.ha_ws import FakeHomeAssistant

    async with FakeHomeAssistant(token="the-real-token") as fake:
        monkeypatch.setenv("SENDTOX3_HA_WS_URL", fake.url)
        monkeypatch.setenv("SUPERVISOR_TOKEN", TOKEN)
        state = make_state(opts)
        async with running(state):
            await wait_until(lambda: len(waits) >= 4)
    assert waits[:4] == [30.0, 60.0, 120.0, 120.0]
    assert state.status["ha_share"]["state"] == "error"


async def test_a_token_rejected_after_a_long_session_backs_off_slowly(ha_running, opts, monkeypatch, waits):
    monkeypatch.setattr(ha_share, "STABLE_SECONDS", 0.0)  # every session counts as long-lived
    state = make_state(opts)
    async with running(state):
        await ha_running.wait_subscribed()
        await wait_until(lambda: state.status["ha_share"]["connected"])
        ha_running.token = "rotated"
        await ha_running.drop_connections()
        await wait_until(lambda: ha_running.auth_failures >= 1 and len(waits) >= 2)
    assert waits[:2] == [2.0, 30.0]  # the reset for a long session must not undo the token backoff


# ------------------------------------------------------- storage failures are retried


class FlakyStorageProcessor(FakeProcessor):
    def __init__(self, failures: int):
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def process(self, req: IngestRequest) -> Item:
        self.attempts += 1
        if self.failures > 0:
            self.failures -= 1
            raise StorageError("[Errno 28] No space left on device")
        return Item(id=new_item_id(), created_at=req.received_at, kind="text", source=req.source, title="Saved")


async def test_storage_failure_is_retried_not_dropped(ha_running, opts, monkeypatch):
    monkeypatch.setattr(ha_share, "STORAGE_RETRY_SECONDS", 0.05)
    processor = FlakyStorageProcessor(failures=2)
    state = make_state(opts, processor)
    async with running(state) as task:
        await ha_running.wait_subscribed()
        st = state.status["ha_share"]
        await wait_until(lambda: st["connected"])
        await ha_running.fire_event("mobile_app.share", {"text": "keep me"})
        await wait_until(lambda: "could not save" in st["last_error"])
        assert st["items_created"] == 0
        await wait_until(lambda: st["items_created"] == 1)
        assert not task.done() and st["state"] == "ok"
    assert processor.attempts == 3 and st["last_item"]["title"] == "Saved"


@pytest.fixture
async def ha_running(monkeypatch):
    from fakes.ha_ws import FakeHomeAssistant

    async with FakeHomeAssistant(token=TOKEN) as fake:
        monkeypatch.setenv("SENDTOX3_HA_WS_URL", fake.url)
        monkeypatch.setenv("SUPERVISOR_TOKEN", TOKEN)
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)
        yield fake


@pytest.fixture(autouse=True)
def fast_worker(monkeypatch):
    """Same as test_ha_share's autouse fixture (fixtures are not shared across modules)."""
    monkeypatch.setattr(ha_share, "BACKOFF_INITIAL", 0.05)
    monkeypatch.setattr(ha_share, "BACKOFF_MAX", 0.2)
    monkeypatch.setattr(ha_share, "AUTH_BACKOFF_MIN", 0.1)
    monkeypatch.setattr(ha_share, "AUTH_BACKOFF_MAX", 0.4)
    monkeypatch.setattr(ha_share, "HANDSHAKE_TIMEOUT", 2.0)
