"""``POST /api/v1/ingest``: the share endpoint for iOS Shortcuts, Android HTTP Shortcuts,
HA automations, curl.

Auth: ``Authorization: Bearer <ingest_token>`` or ``?token=``. Bodies:

- ``application/json`` ``{"url"?, "text"?, "title"?, "html"?, "kind"?, "source"?}``
- ``application/x-www-form-urlencoded`` / ``multipart/form-data`` with the same fields,
  plus an optional file part (``.pdf``, ``.txt``, ``.html``, ``.epub``)
- ``text/plain`` (classified automatically), ``text/html``
- a raw file (``application/pdf``, ``application/epub+zip``, ``application/octet-stream``;
  name from ``?filename=``)

Fields may also come from the query string (for clients that cannot build a body).
Answers ``200 {"ok":true,"id","title","status","duplicate","message","items"}`` (the
first item's fields; ``items`` lists every item when one share became several, e.g. a
list of links), ``202 {"ok":true,"status":"processing"}`` when processing takes longer
than 25 s (it continues in the background), ``401/413/422 {"ok":false,"error"}``, or
``503 {"ok":false,"error"}`` when the server could not store the share (disk full,
database error): nothing was saved and the client should try again later. Processing
is synchronous (25 s wait) so the Shortcut can show the resulting title.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Mapping, Optional

from aiohttp import BodyPartReader, web

from ..ingest import ProcessResult, StorageError, sniff_file_type
from ..models import IngestRequest
from .common import bearer_token, get_state, json_error, json_response, tokens_equal

if TYPE_CHECKING:  # pragma: no cover
    from ..app_state import AppState

log = logging.getLogger(__name__)

#: Request size cap ("30 MB").
MAX_INGEST_BYTES = 30 * 1024 * 1024
_FIELDS = ("url", "text", "title", "html", "kind", "source")
_RAW_TEXT_MAX = 256 * 1024
_KINDS = {"auto", "url", "text", "html", "pdf"}
_SOURCES = {"http", "email", "ha_share", "admin"}
_FILE_TYPES = {
    "application/pdf", "application/epub+zip", "application/octet-stream", "application/x-pdf",
}


class BadRequest(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _too_large() -> BadRequest:
    return BadRequest(413, f"request too large (limit {MAX_INGEST_BYTES // (1024 * 1024)} MB)")


async def _read_capped(request: web.Request) -> bytes:
    if request.content_length is not None and request.content_length > MAX_INGEST_BYTES:
        raise _too_large()
    buf = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        buf += chunk
        if len(buf) > MAX_INGEST_BYTES:
            raise _too_large()
    return bytes(buf)


def _str_field(value: Any, name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        raise BadRequest(422, f"field {name!r} must be a string")
    return value


def _fields_from(mapping: Mapping[str, Any]) -> dict[str, str]:
    return {k: _str_field(mapping.get(k), k) for k in _FIELDS if mapping.get(k) is not None}


async def _read_multipart(request: web.Request) -> tuple[dict[str, str], Optional[tuple[bytes, str, str]]]:
    """Stream a multipart body: text fields plus at most one file part, size-capped."""
    reader = await request.multipart()
    fields: dict[str, str] = {}
    file_part: Optional[tuple[bytes, str, str]] = None
    total = 0
    while True:
        part = await reader.next()
        if part is None:
            break
        if not isinstance(part, BodyPartReader):  # nested multipart: not supported
            raise BadRequest(422, "nested multipart bodies are not supported")
        buf = bytearray()
        while True:
            chunk = await part.read_chunk(65536)
            if not chunk:
                break
            buf += chunk
            total += len(chunk)
            if total > MAX_INGEST_BYTES:
                raise _too_large()
        if part.filename is not None:
            if file_part is None and buf:
                file_part = (bytes(buf), part.filename, part.headers.get("Content-Type", ""))
            continue
        if part.name in _FIELDS:
            charset = part.get_charset(default="utf-8")
            try:
                fields[part.name] = bytes(buf).decode(charset, "replace")
            except LookupError:
                fields[part.name] = bytes(buf).decode("utf-8", "replace")
    return fields, file_part


async def parse_ingest_request(request: web.Request) -> IngestRequest:
    """Build an IngestRequest from any supported body; raises BadRequest."""
    ctype = request.content_type.lower()
    fields: dict[str, str] = {}
    file_part: Optional[tuple[bytes, str, str]] = None

    if ctype == "multipart/form-data":
        fields, file_part = await _read_multipart(request)
    elif ctype == "application/x-www-form-urlencoded":
        body = await _read_capped(request)
        from urllib.parse import parse_qsl

        try:
            pairs = parse_qsl(body.decode(request.charset or "utf-8", "replace"), keep_blank_values=True)
        except (LookupError, ValueError) as exc:
            raise BadRequest(422, f"malformed form body ({exc})") from exc
        fields = {k: v for k, v in pairs if k in _FIELDS}
    elif ctype == "application/json":
        body = await _read_capped(request)
        try:
            data = json.loads(body.decode(request.charset or "utf-8") if body else "{}")
        except (UnicodeDecodeError, LookupError, ValueError) as exc:
            raise BadRequest(422, f"invalid JSON ({exc})") from exc
        if not isinstance(data, dict):
            raise BadRequest(422, "the JSON body must be an object")
        fields = _fields_from(data)
    elif ctype in ("text/plain", "") or ctype.startswith("text/") and ctype != "text/html":
        body = await _read_capped(request)
        fields = {"text": body.decode(request.charset or "utf-8", "replace")}
    elif ctype == "text/html":
        body = await _read_capped(request)
        fields = {"html": body.decode(request.charset or "utf-8", "replace")}
    elif ctype in _FILE_TYPES:
        body = await _read_capped(request)
        filename = request.query.get("filename", "")
        if body and ctype == "application/octet-stream" and not filename \
                and len(body) <= _RAW_TEXT_MAX and sniff_file_type("", "", body) == "txt":
            # No usable Content-Type (aiohttp reports a missing one as octet-stream): a
            # short plain-text body is a share to classify, not a .txt file.
            fields = {"text": body.decode("utf-8", "replace")}
        elif body:
            file_part = (body, filename, ctype)
    else:
        raise BadRequest(422, f"unsupported Content-Type {ctype!r}")

    for k in _FIELDS:  # query-string fallback, body wins
        if k not in fields and k in request.query:
            fields[k] = request.query[k]

    kind = (fields.get("kind") or "auto").strip().lower()
    if kind not in _KINDS:
        raise BadRequest(422, f"unknown kind {kind!r} (use auto, url, text, html or pdf)")
    source = (fields.get("source") or "http").strip().lower()
    if source not in _SOURCES:
        source = "http"

    req = IngestRequest(kind=kind, source=source, url=fields.get("url", "").strip(), text=fields.get("text", ""),
                        html=fields.get("html", ""), title=fields.get("title", "").strip())
    if file_part is not None:
        data, filename, part_type = file_part
        ftype = sniff_file_type(filename, part_type, data)
        if ftype == "unknown":
            raise BadRequest(422, f"unsupported file {filename or part_type!r}: send a PDF, TXT, HTML or EPUB file")
        if ftype == "pdf" and not get_state(request).opts.pdf_enabled:
            raise BadRequest(422, "PDF import is turned off in the app options (pdf_enabled)")
        req.data, req.filename, req.content_type = data, filename, part_type
    elif not (req.url or req.text.strip() or req.html.strip()):
        raise BadRequest(422, "nothing to send: provide url, text, html or a file")
    return req


def result_payload(result: ProcessResult) -> dict[str, Any]:
    item = result.item
    if result.duplicate:
        message = f"Already in the queue: {item.title}"
    elif item.status == "failed":
        message = f"Saved, but could not extract the text: {item.error}" if item.error else "Saved with problems"
    else:
        message = f"Sent to X3: {item.title}"
    return {"ok": True, "id": item.id, "title": item.title, "status": item.status,
            "duplicate": result.duplicate, "error": item.error, "message": message}


def results_payload(results: list[ProcessResult]) -> dict[str, Any]:
    """The answer for a share that produced one or more items: the first item's fields
    at the top level (what the phone shortcuts read) plus ``items`` with all of them."""
    items = [result_payload(r) for r in results]
    payload = {**items[0], "items": items}
    if len(items) > 1:
        if all(r.item.status != "failed" and not r.duplicate for r in results):
            payload["message"] = f"Sent {len(items)} items to X3: " + "; ".join(r.item.title for r in results)
        else:
            payload["message"] = f"{len(items)} items: " + "; ".join(i["message"] for i in items)
    return payload


STORAGE_STATUS = "storage"  # AppState.status key shown as a warning on the admin queue page


def storage_failed(state: "AppState | None", exc: BaseException) -> str:
    """Log a storage failure, flag it for the admin UI, return the user-facing reason."""
    log.error("could not store a share: %s", exc)
    reason = f"Not saved: the server could not store it ({exc}). Try again later."
    if state is not None:
        state.set_status(STORAGE_STATUS, "error", reason)
    return reason


def storage_ok(state: "AppState | None") -> None:
    if state is not None and state.status.get(STORAGE_STATUS):
        state.status.pop(STORAGE_STATUS, None)


#: How long the HTTP request waits for processing before answering "processing".
#: iOS Shortcuts and HTTP Shortcuts give up after roughly 30-60 s.
RESPONSE_WAIT_SECONDS = 25.0

_background: set[asyncio.Task] = set()  # keeps still-running shares referenced


async def _process_all(processor: Any, req: IngestRequest) -> list[ProcessResult]:
    every = getattr(processor, "process_all", None)
    if every is not None:
        results = list(await every(req))
        if results:
            return results
    detailed = getattr(processor, "process_detailed", None)
    if detailed is not None:
        return [await detailed(req)]
    return [ProcessResult(await processor.process(req), False)]


async def run_processor(processor: Any, req: IngestRequest, wait: float | None = None, *,
                        state: "AppState | None" = None) -> Optional[list[ProcessResult]]:
    """Process in a detached task so a client that hangs up does not lose the share.

    Returns the results (one per item; a list of links gives several), or ``None`` when
    processing is still running after ``wait`` seconds (it then finishes in the
    background and the item appears in the queue) or was cancelled by the shutdown
    within the wait (it is redone at the next start). Raises :class:`StorageError` when
    the share could not be stored; a storage failure after the wait is logged (and
    shown on the admin page when ``state`` is given).
    """
    task = asyncio.ensure_future(_process_all(processor, req))
    _background.add(task)
    task.add_done_callback(_background.discard)
    done, _ = await asyncio.wait({task}, timeout=RESPONSE_WAIT_SECONDS if wait is None else wait)
    if task in done:
        if task.cancelled():
            # Cancelled by drain_background at shutdown (a cancelled request would raise
            # in the wait above instead). The spool keeps the request and the next start
            # redoes it, so answer "processing" rather than letting the CancelledError
            # drop the connection. The answer goes out before runner.cleanup() closes
            # the connections; if the client has already hung up, aiohttp drops it
            # quietly (finish_response swallows the ConnectionError).
            log.info("a share was interrupted by the shutdown; it is redone at the next start")
            return None
        return task.result()
    task.add_done_callback(lambda t: _log_background_result(t, state))
    return None


def _log_background_result(task: asyncio.Task, state: "AppState | None") -> None:
    if task.cancelled():
        return
    exc = task.exception()  # retrieving it also silences "exception was never retrieved"
    if isinstance(exc, StorageError):
        storage_failed(state, exc)
    elif exc is not None:
        log.error("processing a share failed in the background", exc_info=exc)
    else:
        storage_ok(state)


async def drain_background(timeout: float) -> int:
    """At shutdown: give shares still processing up to ``timeout`` seconds, then cancel
    the rest. Returns how many were cancelled; the processor's spool keeps those
    requests and :meth:`Processor.recover_pending` redoes them at the next start."""
    pending = {t for t in _background if not t.done()}
    if not pending:
        return 0
    _, pending = await asyncio.wait(pending, timeout=timeout)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.wait(pending, timeout=1.0)
        log.warning("%d share(s) were still processing at shutdown; they are redone at the next start",
                    len(pending))
    return len(pending)


async def ingest(request: web.Request) -> web.Response:
    state = get_state(request)
    if not tokens_equal(bearer_token(request), state.opts.ingest_token):
        return json_error(401, "invalid or missing ingest token")
    try:
        req = await parse_ingest_request(request)
    except BadRequest as exc:
        return json_error(exc.status, exc.message)
    except web.HTTPRequestEntityTooLarge:
        return json_error(413, _too_large().message)
    try:
        results = await run_processor(state.processor, req, state=state)
    except StorageError as exc:
        return json_error(503, storage_failed(state, exc))
    if results is None:
        what = req.filename or req.url or "your share"
        return json_response({"ok": True, "id": "", "title": "", "status": "processing", "duplicate": False,
                              "error": "", "message": f"Received {what}; still processing. It will appear on the X3 shortly."},
                             status=202)
    storage_ok(state)
    return json_response(results_payload(results))


def add_routes(app: web.Application) -> None:
    app.router.add_post("/api/v1/ingest", ingest)
