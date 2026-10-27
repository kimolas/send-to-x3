"""Regression tests for review findings 35, 40/50, 46 and 52 (entry point / lifecycle)."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import pytest

from sendtox3 import __main__ as entry
from sendtox3 import plugin_bundle
from sendtox3.app_state import AppState
from sendtox3.ingest import ProcessResult, Processor
from sendtox3.models import Item, new_item_id
from sendtox3.web import ingest_api


@pytest.fixture
def opts0(opts):
    opts.port = 0
    return opts


async def _start(opts, **kw):
    stop = asyncio.Event()
    ready: asyncio.Future = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(entry.serve(opts, host="127.0.0.1", stop=stop, install_signals=False,
                                           started=lambda st, port: ready.set_result((st, port)), **kw))
    state, port = await asyncio.wait_for(ready, 5)
    return stop, task, state, port


async def _wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


# ---------------------------------------------------------------- finding 46


async def test_serve_starts_with_an_invalid_server_host(opts0, caplog):
    opts0.server_host = "http://192.168.1.50:8765/opds?x=1"
    stop, task, state, port = await _start(opts0)  # used to raise ValueError before binding
    try:
        assert state.base_url == "" and state.base_url_source == plugin_bundle.INVALID_SERVER_HOST
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/healthz") as r:
                assert r.status == 200
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
    assert "server_host" in caplog.text and "invalid" in caplog.text


# ---------------------------------------------------------------- findings 40/50


async def test_container_guess_is_replaced_once_the_supervisor_answers(opts0, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
    opts0.server_host = ""
    answers = [("http://172.30.33.2:8765", "local address"), ("http://172.30.33.2:8765", "local address"),
               ("http://192.168.1.50:8765", "supervisor")]
    calls = []

    async def fake_resolve(opts, **kw):
        calls.append(1)
        return answers[min(len(calls), len(answers)) - 1]

    monkeypatch.setattr(entry, "resolve_base_url", fake_resolve)
    monkeypatch.setattr(entry, "BASE_URL_RETRY_MIN", 0.01)
    monkeypatch.setattr(entry, "BASE_URL_RETRY_MAX", 0.02)
    stop, task, state, port = await _start(opts0)
    try:
        assert await _wait_for(lambda: state.base_url_source == "supervisor")
        assert state.base_url == "http://192.168.1.50:8765"
        n = len(calls)
        await asyncio.sleep(0.1)
        assert len(calls) == n == 3  # stops retrying once the address is known
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


def test_base_url_is_a_guess(opts, store, monkeypatch):
    st = AppState(opts=opts, store=store, processor=None)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    for url, how, guess in [("", "", True), ("http://10.0.0.5:8765", "local address", False),
                            ("http://1.2.3.4:8765", "options", False), ("", plugin_bundle.INVALID_SERVER_HOST, False),
                            ("http://1.2.3.4:8765", "supervisor", False)]:
        st.base_url, st.base_url_source = url, how
        assert entry.base_url_is_a_guess(st) is guess, (url, how)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    st.base_url, st.base_url_source = "http://172.30.33.2:8765", "local address"
    assert entry.base_url_is_a_guess(st)


async def test_refresh_base_url_without_supervisor_stops_at_the_first_address(opts, store, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    st = AppState(opts=opts, store=store, processor=None)
    answers = iter([("", ""), ("http://10.0.0.5:8765", "local address")])

    async def fake_resolve(opts, **kw):
        return next(answers)

    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(entry, "resolve_base_url", fake_resolve)
    await entry.refresh_base_url(st, delay_min=5, delay_max=8, sleep=fake_sleep)
    assert st.base_url == "http://10.0.0.5:8765" and delays == [5, 8]


# ---------------------------------------------------------------- finding 35


async def test_spooled_shares_are_recovered_at_startup(opts0, monkeypatch):
    recovered = asyncio.Event()

    class RecoveringProcessor(Processor):
        async def recover_pending(self) -> int:
            recovered.set()
            return 2

    monkeypatch.setattr(entry, "Processor", RecoveringProcessor)
    stop, task, state, port = await _start(opts0)
    try:
        await asyncio.wait_for(recovered.wait(), 3)
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_recover_failure_does_not_stop_the_app(opts, store, caplog):
    class Broken:
        async def recover_pending(self):
            raise RuntimeError("spool unreadable")

    await entry.recover_spool(AppState(opts=opts, store=store, processor=Broken()))
    assert "spool unreadable" in caplog.text


async def test_shutdown_lets_a_202_share_finish(opts0, monkeypatch):
    stored = asyncio.Event()

    class SlowProcessor(Processor):
        async def process_detailed(self, req):
            await asyncio.sleep(0.3)
            item = Item(id=new_item_id(), created_at=1, kind="text", source="http", title="Late one")
            self.store.add_item(item, epub=b"e", txt=b"t")
            stored.set()
            return ProcessResult(item, False)

        process_all = None

    monkeypatch.setattr(entry, "Processor", SlowProcessor)
    monkeypatch.setattr(ingest_api, "RESPONSE_WAIT_SECONDS", 0.02)
    stop, task, state, port = await _start(opts0)
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{port}/api/v1/ingest", json={"text": "x"},
                          headers={"Authorization": f"Bearer {state.opts.ingest_token}"}) as r:
            assert r.status == 202
    stop.set()  # shut down while the share is still processing
    await asyncio.wait_for(task, 10)
    assert stored.is_set()  # used to be dropped: the store was closed under the running task


async def test_drain_background_cancels_what_does_not_finish(monkeypatch):
    cancelled = asyncio.Event()

    async def forever():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    t = asyncio.ensure_future(forever())
    ingest_api._background.add(t)
    t.add_done_callback(ingest_api._background.discard)
    assert await ingest_api.drain_background(0.05) == 1
    assert cancelled.is_set() and t not in ingest_api._background
    assert await ingest_api.drain_background(0.05) == 0


# ---------------------------------------------------------------- finding 52


async def test_startup_warns_about_an_unusable_device_token(opts0, caplog):
    caplog.set_level(logging.INFO, logger="sendtox3")
    opts0.device_token = "my x3 secret"
    stop, task, state, port = await _start(opts0)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert "device_token option cannot be used by the X3 plugin" in caplog.text
    assert "my x3 secret" not in caplog.text  # never log the token itself
