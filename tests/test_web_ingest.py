"""POST /api/v1/ingest: auth, body formats, limits, error shapes."""

from __future__ import annotations

import json

import aiohttp
import pytest

from sendtox3.ingest import Processor
from sendtox3.web import ingest_api
from sendtox3.web.app import create_app

from test_web_fakes import fake_render_epub, install_fakes
from sendtox3.textmodel import Document, Paragraph

AUTH = {"Authorization": "Bearer ingest-test-token"}
PDF = b"%PDF-1.4\nBT (Shared) Tj (PDF) Tj ET\n%%EOF"


@pytest.fixture
def web(monkeypatch):
    w = install_fakes(monkeypatch)
    w.add("https://example.com/article", "A fine article")
    return w


@pytest.fixture
def app(opts, store, web):
    return create_app(opts, store, Processor(store, opts))


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


async def _ok(resp) -> dict:
    body = await resp.read()
    assert resp.status == 200, body
    assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
    assert resp.headers["Content-Length"] == str(len(body))
    data = json.loads(body)
    assert data["ok"] is True and set(data) >= {"id", "title", "status", "duplicate", "message"}
    return data


async def _err(resp, status: int) -> dict:
    body = await resp.read()
    assert resp.status == status, body
    data = json.loads(body)
    assert data["ok"] is False and isinstance(data["error"], str) and data["error"]
    return data


# ----------------------------------------------------------------------------- auth


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "ingest-test-token"},
                                     {"Authorization": "Bearer device-test-token"}])
async def test_auth_failures(client, store, headers):
    await _err(await client.post("/api/v1/ingest", json={"text": "hi"}, headers=headers), 401)
    assert store.count() == 0


async def test_bearer_case_insensitive_and_query_token(client):
    await _ok(await client.post("/api/v1/ingest", json={"text": "one"},
                                headers={"Authorization": "bearer ingest-test-token"}))
    await _ok(await client.post("/api/v1/ingest?token=ingest-test-token", json={"text": "two"}))


async def test_get_is_not_allowed(client):
    resp = await client.get("/api/v1/ingest", headers=AUTH)
    assert resp.status == 405


# ---------------------------------------------------------------------------- bodies


async def test_json_url(client, store, web):
    data = await _ok(await client.post("/api/v1/ingest", json={"url": "https://example.com/article"}, headers=AUTH))
    assert data["title"] == "A fine article" and data["status"] == "ready" and data["duplicate"] is False
    assert data["message"] == "Sent to X3: A fine article"
    item = store.get(data["id"])
    assert item.kind == "url" and item.source == "http"
    again = await _ok(await client.post("/api/v1/ingest", json={"url": "https://example.com/article"}, headers=AUTH))
    assert again["id"] == data["id"] and again["duplicate"] is True
    assert again["message"].startswith("Already in the queue")


async def test_json_text_title_kind_and_numbers(client, store):
    data = await _ok(await client.post("/api/v1/ingest", headers=AUTH, json={
        "text": "Line one\nLine two", "title": "My note", "kind": "text", "v": 1}))
    assert data["title"] == "My note" and store.get(data["id"]).kind == "text"
    numeric = await _ok(await client.post("/api/v1/ingest", headers=AUTH, json={"text": 42}))
    assert store.get(numeric["id"]).title == "42"


async def test_json_source_is_restricted(client, store):
    a = await _ok(await client.post("/api/v1/ingest", headers=AUTH, json={"text": "x1", "source": "ha_share"}))
    b = await _ok(await client.post("/api/v1/ingest", headers=AUTH, json={"text": "x2", "source": "evil"}))
    assert store.get(a["id"]).source == "ha_share" and store.get(b["id"]).source == "http"


async def test_json_html(client, store):
    data = await _ok(await client.post("/api/v1/ingest", headers=AUTH, json={
        "html": "<title>Captured</title><p>Body text.</p>", "url": "https://example.com/cap"}))
    assert data["title"] == "Captured" and store.get(data["id"]).source_url == "https://example.com/cap"


@pytest.mark.parametrize("body,ctype", [
    (b"{not json", "application/json"),
    (b"[1, 2]", "application/json"),
    (b'{"text": ["a"]}', "application/json"),
    (b'{"text": "x", "kind": "video"}', "application/json"),
    (b"{}", "application/json"),
    (b'{"text": "   "}', "application/json"),
    (b"\xff\xfe", "application/json"),
    (b"\x89PNG....", "image/png"),
])
async def test_422_cases(client, store, body, ctype):
    await _err(await client.post("/api/v1/ingest", data=body, headers={**AUTH, "Content-Type": ctype}), 422)
    assert store.count() == 0


async def test_form_urlencoded(client, store):
    data = await _ok(await client.post("/api/v1/ingest", headers=AUTH,
                                       data={"text": "https://example.com/article", "title": "ignored?"}))
    assert store.get(data["id"]).kind == "url"


async def test_multipart_fields_and_pdf(client, store):
    form = aiohttp.FormData()
    form.add_field("title", "Quarterly")
    form.add_field("file", PDF, filename="report.pdf", content_type="application/pdf")
    data = await _ok(await client.post("/api/v1/ingest", data=form, headers=AUTH))
    item = store.get(data["id"])
    assert item.kind == "pdf" and item.title == "Quarterly"
    form = aiohttp.FormData()
    form.add_field("text", "A multipart note")
    form.add_field("file", b"", filename="", content_type="application/octet-stream")  # empty file input
    data = await _ok(await client.post("/api/v1/ingest", data=form, headers=AUTH))
    assert store.get(data["id"]).kind == "text"


async def test_multipart_epub_and_txt(client, store):
    book = fake_render_epub([Document(title="Shared Book", blocks=[Paragraph("Chapter.")])])
    form = aiohttp.FormData()
    form.add_field("file", book, filename="book.epub", content_type="application/epub+zip")
    data = await _ok(await client.post("/api/v1/ingest", data=form, headers=AUTH))
    assert data["title"] == "Shared Book" and store.file_bytes(data["id"], "epub") == book
    form = aiohttp.FormData()
    form.add_field("file", "plain text file".encode(), filename="a.txt", content_type="text/plain")
    data = await _ok(await client.post("/api/v1/ingest", data=form, headers=AUTH))
    assert store.get(data["id"]).kind == "text"


async def test_multipart_unsupported_file(client, store):
    form = aiohttp.FormData()
    form.add_field("file", b"\x00\x01\x02\x03" * 8, filename="photo.heic", content_type="image/heic")
    await _err(await client.post("/api/v1/ingest", data=form, headers=AUTH), 422)
    assert store.count() == 0


async def test_pdf_disabled_is_422(aiohttp_client, opts, store, web):
    opts.pdf_enabled = False
    client = await aiohttp_client(create_app(opts, store, Processor(store, opts)))
    form = aiohttp.FormData()
    form.add_field("file", PDF, filename="x.pdf", content_type="application/pdf")
    data = await _err(await client.post("/api/v1/ingest", data=form, headers=AUTH), 422)
    assert "pdf_enabled" in data["error"]


async def test_text_plain_is_classified(client, store):
    data = await _ok(await client.post("/api/v1/ingest", data="https://example.com/article\n".encode(),
                                       headers={**AUTH, "Content-Type": "text/plain; charset=utf-8"}))
    assert store.get(data["id"]).kind == "url"
    data = await _ok(await client.post("/api/v1/ingest", data="Ünïcode note".encode("utf-8"),
                                       headers={**AUTH, "Content-Type": "text/plain"}))
    assert data["title"] == "Ünïcode note"


async def test_body_without_content_type_is_classified(client, store):
    data = await _ok(await client.post("/api/v1/ingest", data=b"https://example.com/article",
                                       headers={**AUTH, "Content-Type": "application/octet-stream"}))
    assert store.get(data["id"]).kind == "url" and data["title"] == "A fine article"
    pdf = await _ok(await client.post("/api/v1/ingest", data=PDF,
                                      headers={**AUTH, "Content-Type": "application/octet-stream"}))
    assert store.get(pdf["id"]).kind == "pdf"


async def test_raw_pdf_body_with_filename(client, store):
    data = await _ok(await client.post("/api/v1/ingest?filename=Raw_Upload.pdf", data=PDF,
                                       headers={**AUTH, "Content-Type": "application/pdf"}))
    item = store.get(data["id"])
    assert item.kind == "pdf" and item.title == "Raw Upload"


async def test_query_string_fields(client, store):
    data = await _ok(await client.post("/api/v1/ingest?url=https%3A%2F%2Fexample.com%2Farticle", headers=AUTH))
    assert store.get(data["id"]).kind == "url"


async def test_failed_item_is_still_200(client, web, store):
    web.pages["https://example.com/broken"] = RuntimeError("kaputt")
    data = await _ok(await client.post("/api/v1/ingest", json={"url": "https://example.com/broken"}, headers=AUTH))
    assert data["status"] == "failed" and "kaputt" in data["error"]
    assert data["message"].startswith("Saved, but could not extract")


# ---------------------------------------------------------------------------- limits


async def test_413_json_body(client, store, monkeypatch):
    monkeypatch.setattr(ingest_api, "MAX_INGEST_BYTES", 1000)
    await _err(await client.post("/api/v1/ingest", json={"text": "x" * 2000}, headers=AUTH), 413)
    assert store.count() == 0


async def test_413_streamed_text_without_length(client, monkeypatch):
    monkeypatch.setattr(ingest_api, "MAX_INGEST_BYTES", 1000)

    async def gen():
        for _ in range(10):
            yield b"y" * 500

    await _err(await client.post("/api/v1/ingest", data=gen(), headers={**AUTH, "Content-Type": "text/plain"}), 413)


async def test_413_multipart(client, monkeypatch):
    monkeypatch.setattr(ingest_api, "MAX_INGEST_BYTES", 1000)
    form = aiohttp.FormData()
    form.add_field("file", b"%PDF-" + b"z" * 5000, filename="big.pdf", content_type="application/pdf")
    await _err(await client.post("/api/v1/ingest", data=form, headers=AUTH), 413)


async def test_cap_is_30_mb():
    assert ingest_api.MAX_INGEST_BYTES == 30 * 1024 * 1024


async def test_client_disconnect_does_not_lose_the_share(client, store, web):
    import asyncio

    web.delay = 0.3
    web.add("https://example.com/slow", "Slow one")
    with pytest.raises(asyncio.TimeoutError):
        await client.post("/api/v1/ingest", json={"url": "https://example.com/slow"}, headers=AUTH,
                          timeout=aiohttp.ClientTimeout(total=0.05))
    for _ in range(40):
        if store.count():
            break
        await asyncio.sleep(0.05)
    assert [i.title for i in store.list_items()] == ["Slow one"]


async def test_slow_processing_answers_202_and_finishes_in_background(aiohttp_client, opts, store, monkeypatch):
    import asyncio

    from sendtox3.models import Item, new_item_id
    from sendtox3.ingest import ProcessResult

    done = asyncio.Event()

    class SlowProcessor:
        async def process_detailed(self, req):
            await asyncio.sleep(0.3)
            item = Item(id=new_item_id(), created_at=1, kind="text", source="http", title="Slow one")
            store.add_item(item, epub=b"e", txt=b"t")
            done.set()
            return ProcessResult(item, False)

    monkeypatch.setattr(ingest_api, "RESPONSE_WAIT_SECONDS", 0.05)
    client = await aiohttp_client(create_app(opts, store, SlowProcessor()))
    resp = await client.post("/api/v1/ingest", json={"text": "hello"}, headers=AUTH)
    assert resp.status == 202
    data = await resp.json()
    assert data["ok"] is True and data["status"] == "processing"
    await asyncio.wait_for(done.wait(), 5)
    assert [i.title for i in store.list_items()] == ["Slow one"]
