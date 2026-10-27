"""Round-2 regression tests, group E: R2-E4 (a share still inside its 25 s wait when the
app shuts down gets a 202 "processing" answer instead of a dropped connection)."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from sendtox3 import __main__ as entry
from sendtox3.ingest import Processor
from sendtox3.models import IngestRequest
from sendtox3.web import ingest_api


@pytest.fixture
def opts0(opts):
    opts.port = 0
    return opts


class _Forever:
    """A processor whose share never finishes on its own (cancelled at shutdown)."""

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def process_detailed(self, req):
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    process_all = None


async def test_run_processor_answers_processing_when_the_shutdown_cancels_the_share():
    proc = _Forever()
    waiting = asyncio.ensure_future(
        ingest_api.run_processor(proc, IngestRequest(kind="text", text="x"), wait=30))
    await asyncio.wait_for(proc.started.wait(), 2)
    assert await ingest_api.drain_background(0.05) == 1
    # Used to raise CancelledError here, which dropped the client's connection.
    assert await asyncio.wait_for(waiting, 2) is None
    assert proc.cancelled.is_set()


async def test_a_cancelled_request_still_raises():
    """Only the shutdown's cancellation of the share is turned into "processing"; a
    cancelled request handler keeps its CancelledError (and the share keeps running)."""
    proc = _Forever()
    waiting = asyncio.ensure_future(
        ingest_api.run_processor(proc, IngestRequest(kind="text", text="x"), wait=30))
    await asyncio.wait_for(proc.started.wait(), 2)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not proc.cancelled.is_set()
    assert await ingest_api.drain_background(0.05) == 1


async def test_shutdown_during_the_wait_answers_202(opts0, monkeypatch):
    holder = {}

    class ForeverProcessor(Processor):
        async def process_detailed(self, req):
            holder["started"].set()
            await asyncio.sleep(60)

        process_all = None

    holder["started"] = asyncio.Event()
    monkeypatch.setattr(entry, "Processor", ForeverProcessor)
    monkeypatch.setattr(entry, "BACKGROUND_GRACE", 0.1)
    monkeypatch.setattr(ingest_api, "RESPONSE_WAIT_SECONDS", 30.0)
    stop = asyncio.Event()
    ready: asyncio.Future = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(entry.serve(opts0, host="127.0.0.1", stop=stop, install_signals=False,
                                           started=lambda st, port: ready.set_result((st, port))))
    state, port = await asyncio.wait_for(ready, 5)

    async def post():
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{port}/api/v1/ingest", json={"text": "x"},
                              headers={"Authorization": f"Bearer {state.opts.ingest_token}"}) as r:
                return r.status, await r.json()

    try:
        sent = asyncio.ensure_future(post())
        await asyncio.wait_for(holder["started"].wait(), 5)
        stop.set()  # shut down while the request is still inside its 25 s wait
        status, body = await asyncio.wait_for(sent, 10)  # used to be ServerDisconnectedError
        assert status == 202 and body["ok"] is True and body["status"] == "processing"
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
