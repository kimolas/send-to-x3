"""Entry point: serve() lifecycle, worker supervision/backoff, prune task, --show-tokens."""

from __future__ import annotations

import asyncio
import logging
import sys
import types

import aiohttp
import pytest

from sendtox3 import __main__ as entry
from sendtox3.app_state import AppState, normalize_status, resolve_secrets
from sendtox3.config import Options


@pytest.fixture
def opts0(opts):
    opts.port = 0  # ephemeral port; serve() binds 127.0.0.1 in these tests
    return opts


async def _start(opts, **kw):
    stop = asyncio.Event()
    ready: asyncio.Future = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(entry.serve(opts, host="127.0.0.1", stop=stop, install_signals=False,
                                           started=lambda st, port: ready.set_result((st, port)), **kw))
    state, port = await asyncio.wait_for(ready, 5)
    return stop, task, state, port


async def test_serve_starts_and_stops(opts0, caplog):
    caplog.set_level(logging.INFO, logger="sendtox3")
    opts0.ingest_token = opts0.device_token = opts0.admin_token = ""
    stop, task, state, port = await _start(opts0)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/healthz") as r:
                assert r.status == 200 and (await r.json())["ok"] is True
            async with s.post(f"http://127.0.0.1:{port}/api/v1/ingest", json={"text": "x"},
                              headers={"Authorization": f"Bearer {state.opts.ingest_token}"}) as r:
                assert r.status in (200,)
        assert len(state.opts.device_token) >= 24 and state.base_url == "http://127.0.0.1:0"
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
    text = caplog.text
    assert "ingest=generated" in text and "listening on 127.0.0.1" in text and "/opds" in text
    for token in (state.opts.ingest_token, state.opts.device_token, state.opts.admin_token):
        assert token not in text  # token values are never logged


async def test_workers_get_app_state_and_are_cancelled(opts0, monkeypatch):
    seen = {}
    cancelled = asyncio.Event()

    async def run(app_state):
        seen["state"] = app_state
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    mod = types.ModuleType("sendtox3.workers.lan_push")
    mod.run = run
    monkeypatch.setitem(sys.modules, "sendtox3.workers.lan_push", mod)
    opts0.device_host = "192.168.1.60"
    stop, task, state, port = await _start(opts0)
    for _ in range(50):
        if "state" in seen:
            break
        await asyncio.sleep(0.02)
    st = seen["state"]
    assert st is state and st.opts is opts0 and st.store and st.processor and isinstance(st.status, dict)
    assert st.base_url
    stop.set()
    await asyncio.wait_for(task, 10)
    assert cancelled.is_set()


def test_worker_plan(opts, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    opts.imap_enabled, opts.ha_share_enabled, opts.device_host = True, True, "x3"
    assert [n for n, _ in entry.worker_plan(opts)] == ["imap", "lan_push"]
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    assert [n for n, _ in entry.worker_plan(opts)] == ["imap", "ha_share", "lan_push"]
    opts.imap_enabled, opts.ha_share_enabled, opts.device_host = False, False, ""
    assert entry.worker_plan(opts) == []


async def test_supervise_restarts_with_backoff(opts, store):
    state = AppState(opts=opts, store=store, processor=None)
    runs = []
    delays = []

    async def flaky(app_state):
        runs.append(1)
        if len(runs) == 3:
            return  # a worker that returns is restarted too
        raise RuntimeError(f"boom {len(runs)}")

    async def fake_sleep(d):
        delays.append(d)
        if len(delays) >= 6:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await entry.supervise("imap", flaky, state, backoff_min=5, backoff_max=30, sleep=fake_sleep)
    assert delays == [5, 10, 20, 30, 30, 30]
    assert len(runs) == 6
    assert normalize_status(state.status["imap"])["state"] == "error"


async def test_disabled_worker_is_not_restarted(opts, store):
    state = AppState(opts=opts, store=store, processor=None)
    runs = []

    async def disabled(app_state):
        runs.append(1)
        app_state.status["ha_share"] = {"enabled": False, "reason": "not running under Home Assistant"}

    async def no_sleep(d):
        raise AssertionError("must not back off")

    await entry.supervise("ha_share", disabled, state, sleep=no_sleep)
    assert runs == [1] and state.channel_status("ha_share")["state"] == "disabled"


def test_normalize_worker_status_dicts():
    lan = {"enabled": True, "host": "192.168.1.60", "online": False, "last_seen": 100, "device_version": "",
           "pending": 2, "pushed_count": 0, "last_error": "", "last_error_at": None}
    assert normalize_status(lan)["state"] == "waiting"
    assert "not in File Transfer mode" in normalize_status(lan)["detail"]
    lan.update(online=True, device_version="1.6.5", pushed_count=3, last_seen=200)
    st = normalize_status(lan)
    assert st["state"] == "ok" and "CrossPoint 1.6.5" in st["detail"] and "3 files pushed" in st["detail"]
    assert st["updated"] == 200
    lan.update(online=False, last_error="disk full", last_error_at=300)
    assert normalize_status(lan) == {"state": "error", "detail": "disk full", "updated": 300}
    imap = {"enabled": True, "state": "idle", "connected": True, "messages_processed": 4, "last_sync_at": 50}
    assert normalize_status(imap)["state"] == "ok" and "4 messages" in normalize_status(imap)["detail"]
    imap.update(state="backoff", connected=False, last_error="LOGIN failed", last_error_at=60)
    assert normalize_status(imap)["state"] == "error" and normalize_status(imap)["detail"] == "LOGIN failed"
    assert normalize_status({"enabled": False, "reason": "no SUPERVISOR_TOKEN"})["state"] == "disabled"
    assert normalize_status({"state": "not_configured", "enabled": True})["detail"] == "not configured"
    ha = {"enabled": True, "connected": True, "events_received": 5, "items_created": 4, "last_event": 9}
    assert normalize_status(ha)["state"] == "ok" and "4 items created" in normalize_status(ha)["detail"]


async def test_missing_worker_module_marks_error(opts, store):
    state = AppState(opts=opts, store=store, processor=None)
    await entry.run_worker_module("imap", "sendtox3.workers.does_not_exist", state)
    assert state.channel_status("imap")["state"] == "error"


async def test_prune_daily(opts, store, monkeypatch):
    state = AppState(opts=opts, store=store, processor=None)
    calls = []
    monkeypatch.setattr(store, "prune", lambda days: calls.append(days) or 2)
    task = asyncio.create_task(entry.prune_daily(state, first_delay=0, interval=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2 and calls[0] == opts.retention_days


def test_show_tokens(data_dir, capsys, monkeypatch):
    (data_dir / "options.json").write_text('{"ingest_token": "fixed-ingest"}')
    assert entry.main(["--show-tokens"]) == 0
    out = capsys.readouterr().out
    assert "ingest_token: fixed-ingest" in out
    first = out
    entry.main(["--show-tokens"])
    assert capsys.readouterr().out == first  # generated tokens persist


def test_resolve_secrets(store):
    o = Options(ingest_token="mine")
    configured = resolve_secrets(o, store)
    assert configured == {"ingest_token": True, "device_token": False, "admin_token": False}
    assert o.ingest_token == "mine" and o.device_token and o.device_token != o.admin_token
    again = Options()
    resolve_secrets(again, store)
    assert again.device_token == o.device_token


def test_normalize_status_variants():
    assert normalize_status(None)["state"] == "unknown"
    assert normalize_status("ok")["state"] == "ok"
    assert normalize_status("connected to imap")["detail"] == "connected to imap"
    assert normalize_status({"ok": False, "error": "auth failed"}) == {"state": "error", "detail": "auth failed",
                                                                      "updated": 0}
    assert normalize_status({"state": "waiting", "detail": "x", "updated": "12"})["updated"] == 12
