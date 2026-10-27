"""The phone shortcuts against the real share API (``POST /api/v1/ingest``).

The iOS Shortcut runs in the simulator (``shortcuts/tools/simulate.py``) and makes real
HTTP requests to the app on 127.0.0.1; the Android request is rendered exactly as HTTP
Shortcuts builds it. A recording processor stands in for extraction, so this checks
the wire contract only: auth, JSON body, the fields the server sees, and the reply the
shortcuts show.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

import pytest
from sendtox3.ingest import ProcessResult
from sendtox3.models import IngestRequest, Item, new_item_id
from sendtox3.web.app import create_app
from test_shortcuts import ba, bs, render_android_request, sim


class RecordingProcessor:
    def __init__(self) -> None:
        self.requests: list[IngestRequest] = []

    async def process_detailed(self, req: IngestRequest) -> ProcessResult:
        self.requests.append(req)
        title = req.title or "Derived title"
        return ProcessResult(Item(id=new_item_id(), created_at=int(time.time()), kind="url" if req.url else "text",
                                  source=req.source, title=title, source_url=req.url))

    async def process(self, req: IngestRequest) -> Item:
        return (await self.process_detailed(req)).item


@pytest.fixture
async def api(aiohttp_server, opts, store):
    processor = RecordingProcessor()
    server = await aiohttp_server(create_app(opts, store, processor), host="127.0.0.1")
    return str(server.make_url("/api/v1/ingest")), processor


def real_http(req: sim.HttpRequest) -> tuple[int, bytes]:
    """What Get Contents of URL does: JSON body, the configured headers."""
    request = urllib.request.Request(req.url, data=json.dumps(req.json_body).encode("utf-8"), method=req.method,
                                     headers={**req.headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def run_ios(url: str, token: str, item) -> sim.RunResult:
    cfg = bs.ShortcutConfig(ingest_url=url, token=token, home_wifi="Home", email="me@example.com")
    return await asyncio.to_thread(sim.run, bs.build_workflow(cfg), wifi="Home", http=real_http,
                                   shortcut_input=[item])


async def test_ios_safari_page_reaches_the_app(api):
    url, processor = api
    r = await run_ios(url, "ingest-test-token", sim.SafariPage("https://example.com/a", "A page", "Selected words."))
    [req] = processor.requests
    assert (req.url, req.text, req.title, req.kind) == ("https://example.com/a", "Selected words.", "A page", "auto")
    assert req.source == "http"  # "ios-shortcut" is not a stored source name; the API maps it to http
    assert r.notifications == [("Send to X3", "Sent to X3: A page")] and not r.alerts


async def test_ios_text_round_trips_exactly(api):
    url, processor = api
    text = 'Ünïcode "quotes" \\ backslash\nnew line — see https://example.com/x'
    r = await run_ios(url, "ingest-test-token", sim.Text(text))
    [req] = processor.requests
    assert req.text == text and req.url == "" and req.title == ""  # the app finds the link itself
    assert r.notifications == [("Send to X3", "Sent to X3: Derived title")]


async def test_ios_link_only(api):
    url, processor = api
    await run_ios(url, "ingest-test-token", sim.URL("https://example.com/b"))
    [req] = processor.requests
    assert (req.url, req.text) == ("https://example.com/b", "")


async def test_ios_wrong_token_shows_the_server_error(api):
    url, processor = api
    r = await run_ios(url, "wrong", sim.Text("hello"))
    assert not processor.requests and not r.notifications
    [(title, message)] = r.alerts
    assert title == "Send to X3: not saved" and message.startswith("invalid or missing ingest token")


@pytest.mark.parametrize("text", [
    "https://example.com/article?id=1&utm_source=x",
    'Quote "this" \\ and / and </script>\nnext line\ttab',
    "Ünïcödé 中文 😀",
])
async def test_android_request_reaches_the_app(aiohttp_client, opts, store, text):
    processor = RecordingProcessor()
    client = await aiohttp_client(create_app(opts, store, processor))
    data = ba.build_http_shortcuts()
    _, headers, body = render_android_request(data, text=text, title="Shared title", values={
        "sendtox3_url": "http://unused", "sendtox3_token": "ingest-test-token"})
    resp = await client.post("/api/v1/ingest", data=body, headers=headers)
    assert resp.status == 200
    reply = await resp.json()
    assert reply["ok"] is True and reply["message"] == "Sent to X3: Shared title"
    [req] = processor.requests
    assert (req.text, req.title, req.kind) == (text, "Shared title", "auto")


async def test_android_wrong_token_answers_json_error(aiohttp_client, opts, store):
    client = await aiohttp_client(create_app(opts, store, RecordingProcessor()))
    _, headers, body = render_android_request(ba.build_http_shortcuts(), text="x", title="", values={
        "sendtox3_url": "http://unused", "sendtox3_token": "nope"})
    resp = await client.post("/api/v1/ingest", data=body, headers=headers)
    assert resp.status == 401
    reply = json.loads(await resp.read())
    assert reply["ok"] is False and reply["error"]  # what the failure script shows
