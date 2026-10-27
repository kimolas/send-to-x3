"""Home Assistant Companion app share sheet -> Send to X3.

When the user shares a page or text to the Home Assistant Companion app (iOS 2020.7+,
Android 2.5.0+), the app fires a ``mobile_app.share`` event on the HA event bus
(companion.home-assistant.io/docs/integrations/sharing). This worker subscribes to it
over the websocket API, through the Supervisor proxy at
``ws://supervisor/core/websocket`` (needs ``homeassistant_api: true`` in config.yaml),
authenticating with the app's ``SUPERVISOR_TOKEN``. No inbound exposure is needed: the
phone reaches HA however it normally does.

Event data (Companion docs):

- iOS: ``url`` or ``text`` (whatever the source app shared), ``entered`` (the share
  sheet's text box; it is pre-filled with the page title or the selected text and the
  user may edit it), ``sourceDeviceName``, ``sourceDeviceID``, ``sourceDevicePermanentID``.
- Android: ``url`` or ``text``, ``subject`` (e.g. the page title from Chrome),
  ``caller`` (``android-app://<package>``), ``device_id``.

Each event becomes an :class:`~sendtox3.models.IngestRequest` (``source="ha_share"``,
``sender`` = the phone's name) handed to ``state.processor.process``. Processing runs in
a separate task so a slow fetch never stalls the websocket (pings keep flowing). When
the store cannot be written (:class:`~sendtox3.ingest.StorageError`) the share is kept
and retried every ``STORAGE_RETRY_SECONDS``. Events fired while disconnected are not
replayed by HA, so reconnects back off only briefly (``BACKOFF_MAX``; a rejected token
backs off longer). The outcome is logged and reported in ``state.status["ha_share"]``.

State contract (duck-typed, see :class:`HAShareState`)::

    state.opts       Options     ha_share_enabled
    state.processor  Processor   async process(IngestRequest) -> Item (raises only StorageError)
    state.status     dict        this worker owns state.status["ha_share"]:
        state: str               AppState contract: "starting", "ok" (subscribed),
                                 "warning" (reconnecting), "error" (token rejected,
                                 or a share cannot be saved), "disabled"
        detail: str              one human sentence for the admin UI
        updated: int             unix time of the last status change
        enabled: bool            False when disabled or not under HA (worker returns)
        reason: str              why it is disabled ("" otherwise)
        url: str                 websocket URL
        connected: bool          authenticated and subscribed
        ha_version: str
        last_connected: int|None unix time of the last successful subscribe
        events_received: int     mobile_app.share events seen
        items_created: int       events turned into queued items
        ignored: int             events with nothing to send (no url/text)
        last_event: int|None     unix time of the last event
        last_item: dict|None     {"id","title","status","at"} of the last item
        reconnects: int
        last_error: str
        last_error_at: int|None

Environment: ``SUPERVISOR_TOKEN`` (``HASSIO_TOKEN`` accepted as a legacy name);
``SENDTOX3_HA_WS_URL`` overrides the websocket URL (tests, development against a plain
HA instance with a long-lived access token in ``SUPERVISOR_TOKEN``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import time
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol

import aiohttp

from ..extract.text import find_urls
from ..ingest import StorageError
from ..models import IngestRequest

if TYPE_CHECKING:
    from ..config import Options
    from ..models import Item

log = logging.getLogger(__name__)

DEFAULT_WS_URL = "ws://supervisor/core/websocket"
EVENT_TYPE = "mobile_app.share"

HANDSHAKE_TIMEOUT = 15.0  # connect + auth_required + auth_ok
PING_INTERVAL = 30.0  # send an application-level ping after this much silence
PONG_TIMEOUT = 15.0  # ... and give up on the connection if nothing arrives after it
BACKOFF_INITIAL = 2.0
# Shares fired while we are not subscribed are lost (HA does not replay events), and the
# Supervisor proxy is local and cheap to retry, so while Core restarts we try again
# every few seconds instead of backing off to minutes.
BACKOFF_MAX = 5.0
AUTH_BACKOFF_MIN = 30.0  # a rejected token will not fix itself in a second
AUTH_BACKOFF_MAX = 120.0
STABLE_SECONDS = 60.0  # a connection that lived this long resets the backoff
QUIET_AFTER_FAILURES = 3  # then log failed attempts about once a minute (debug otherwise)
STORAGE_RETRY_SECONDS = 60.0  # the store could not be written (disk full, I/O error)
REGISTRY_REFRESH_SECONDS = 300.0  # re-read device names at most this often
QUEUE_MAX = 200
MAX_MSG_SIZE = 16 * 1024 * 1024  # the device registry can be large
TITLE_MAX_CHARS = 150

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$", re.IGNORECASE)


class HAShareState(Protocol):
    opts: "Options"
    processor: Any  # sendtox3.ingest.Processor: async process(IngestRequest) -> Item
    status: dict[str, Any]


class HAAuthError(Exception):
    """Home Assistant (or the Supervisor proxy) rejected the token."""


class HAProtocolError(Exception):
    """Unexpected message, failed subscription, missing pong, or the socket closed."""


# ------------------------------------------------------------------------ mapping


def _field(data: Mapping[str, Any], key: str) -> str:
    v = data.get(key)
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return ""


def _title_like(text: str) -> bool:
    return bool(text) and "\n" not in text and len(text) <= TITLE_MAX_CHARS


def _comment_with_link(comment: str, url: str) -> str:
    """``comment`` and ``url`` as one shared text, or "" when the URL would not be found
    unchanged in it (a trailing ``.`` or a quote in the URL, a second link in the comment)."""
    joined = f"{comment}\n\n{url}"
    return joined if find_urls(joined) == [url] else ""


def device_names_from_registry(devices: Any) -> dict[str, str]:
    """``config/device_registry/list`` result -> {registry id / mobile_app id: name}."""
    names: dict[str, str] = {}
    if not isinstance(devices, list):
        return names
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        name = dev.get("name_by_user") or dev.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        if isinstance(dev.get("id"), str):
            names[dev["id"]] = name
        for ident in dev.get("identifiers") or []:
            if isinstance(ident, (list, tuple)) and len(ident) == 2 and ident[0] == "mobile_app":
                names[str(ident[1])] = name
    return names


def share_event_to_request(
    data: Mapping[str, Any],
    *,
    device_names: Optional[Mapping[str, str]] = None,
    received_at: Optional[int] = None,
) -> Optional[IngestRequest]:
    """Map ``mobile_app.share`` event data to an IngestRequest (None if nothing to send).

    iOS's ``entered`` box with a link: the box starts out as the page title, which the
    user may have replaced with a comment, and only the page can tell the two apart.
    So the words are sent with the link as one text ("comment + link"): the Processor
    fetches the page and keeps the words as a comment unless they are (part of) the page
    title. As a ``title`` they would only be a hint that the page's own title overrides.
    """
    if not isinstance(data, Mapping):
        return None
    url = _field(data, "url")
    text = _field(data, "text")
    entered = _field(data, "entered")  # iOS share sheet text box
    title = _field(data, "subject")  # Android EXTRA_SUBJECT, e.g. the page title

    if url and not _SCHEME_RE.match(url):
        # Not a URL after all: keep it as text rather than losing it.
        text = url if not text or text == url else f"{url}\n\n{text}"
        url = ""
    if text and text == url:
        text = ""

    if entered and entered != title and entered != url and not (text and entered in text):
        if not url and not text:
            text = entered  # the user typed a note and shared nothing else
        elif url and not text and _title_like(entered) and not title:
            joined = _comment_with_link(entered, url)
            if joined:
                text, url = joined, ""  # the page title, or the user's comment on the page
            else:
                text = entered  # kept as a note with the page as its source
        elif _title_like(entered) and not title:
            title = entered  # a title for the shared text
        else:
            text = f"{entered}\n\n{text}" if text else entered  # a longer comment: keep it
    if not url and not text:
        return None

    names = device_names or {}
    sender = (
        _field(data, "sourceDeviceName")
        or names.get(_field(data, "device_id"), "")
        or names.get(_field(data, "sourceDeviceID"), "")
    )
    return IngestRequest(
        kind="auto",
        source="ha_share",
        url=url,
        text=text,
        title=title,
        sender=sender,
        received_at=int(received_at or time.time()),
    )


# ----------------------------------------------------------------------- worker


def _init_status(state: HAShareState, url: str) -> dict[str, Any]:
    status: dict[str, Any] = {
        "state": "starting",
        "detail": "Connecting to Home Assistant",
        "updated": int(time.time()),
        "enabled": True,
        "reason": "",
        "url": url,
        "connected": False,
        "ha_version": "",
        "last_connected": None,
        "events_received": 0,
        "items_created": 0,
        "ignored": 0,
        "last_event": None,
        "last_item": None,
        "reconnects": 0,
        "last_error": "",
        "last_error_at": None,
    }
    state.status["ha_share"] = status
    return status


def _publish(status: dict[str, Any], state: str, detail: str) -> None:
    """The ``state``/``detail``/``updated`` triple the admin UI shows (see app_state.py)."""
    status.update(state=state, detail=detail, updated=int(time.time()))


def _set_error(status: dict[str, Any], message: str) -> None:
    status["last_error"] = message
    status["last_error_at"] = int(time.time())


def _listening_detail(status: dict[str, Any]) -> str:
    n = status["items_created"]
    tail = f"; {n} share(s) queued since start" if n else ""
    return f"Listening for shares from the Home Assistant Companion app{tail}"


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timed out"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class _Connection:
    """One authenticated websocket session with HA."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, queue: "asyncio.Queue[IngestRequest]",
                 status: dict[str, Any], device_names: dict[str, str]):
        self.ws = ws
        self.queue = queue
        self.status = status
        self.device_names = device_names
        self._next_id = 0
        self.sub_id = 0
        self.registry_id = 0
        self.registry_requested_at = 0.0

    async def send(self, msg: dict[str, Any]) -> int:
        self._next_id += 1
        await self.ws.send_str(json.dumps({"id": self._next_id, **msg}))
        return self._next_id

    async def receive(self, timeout: float) -> list[dict[str, Any]]:
        msg = await self.ws.receive(timeout=timeout)
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except ValueError as exc:
                raise HAProtocolError(f"invalid JSON from Home Assistant: {exc}") from exc
            items = data if isinstance(data, list) else [data]  # coalesced messages
            return [m for m in items if isinstance(m, dict)]
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
            raise HAProtocolError(f"websocket closed by Home Assistant (code {self.ws.close_code})")
        if msg.type == aiohttp.WSMsgType.ERROR:
            raise HAProtocolError(f"websocket error: {self.ws.exception()!r}")
        return []  # binary/ping/pong frames carry nothing for us

    async def receive_one(self, timeout: float) -> dict[str, Any]:
        msgs = await self.receive(timeout)
        if not msgs:
            raise HAProtocolError("unexpected empty message during the handshake")
        return msgs[0]

    async def authenticate(self, token: str) -> str:
        first = await self.receive_one(HANDSHAKE_TIMEOUT)
        if first.get("type") == "auth_ok":  # an instance without auth (dev setups)
            return str(first.get("ha_version", ""))
        if first.get("type") != "auth_required":
            raise HAProtocolError(f"expected auth_required, got {first.get('type')!r}")
        await self.ws.send_str(json.dumps({"type": "auth", "access_token": token}))
        reply = await self.receive_one(HANDSHAKE_TIMEOUT)
        if reply.get("type") == "auth_invalid":
            raise HAAuthError(str(reply.get("message") or "invalid access token"))
        if reply.get("type") != "auth_ok":
            raise HAProtocolError(f"expected auth_ok, got {reply.get('type')!r}")
        return str(reply.get("ha_version", ""))

    async def request_registry(self) -> None:
        self.registry_requested_at = time.monotonic()
        self.registry_id = await self.send({"type": "config/device_registry/list"})

    async def serve(self) -> None:
        """Subscribe, then dispatch messages until the connection fails."""
        self.sub_id = await self.send({"type": "subscribe_events", "event_type": EVENT_TYPE})
        await self.request_registry()
        ping_outstanding = False
        while True:
            try:
                msgs = await self.receive(PONG_TIMEOUT if ping_outstanding else PING_INTERVAL)
            except TimeoutError:
                if ping_outstanding:
                    raise HAProtocolError("Home Assistant stopped answering pings") from None
                await self.send({"type": "ping"})
                ping_outstanding = True
                continue
            ping_outstanding = False  # anything received proves the connection is alive
            for m in msgs:
                await self._dispatch(m)

    async def _dispatch(self, m: dict[str, Any]) -> None:
        mtype, mid = m.get("type"), m.get("id")
        if mtype == "result" and mid == self.sub_id:
            if not m.get("success"):
                raise HAProtocolError(f"subscribe_events failed: {m.get('error')}")
            self.status.update(connected=True, last_connected=int(time.time()))
            _publish(self.status, "ok", _listening_detail(self.status))
            log.info("HA share: subscribed to %s", EVENT_TYPE)
        elif mtype == "result" and mid == self.registry_id:
            if m.get("success"):
                self.device_names.update(device_names_from_registry(m.get("result")))
            else:
                log.debug("HA share: device registry unavailable: %s", m.get("error"))
        elif mtype == "event" and mid == self.sub_id:
            await self._on_event(m.get("event"))

    async def _on_event(self, event: Any) -> None:
        if not isinstance(event, dict) or event.get("event_type") != EVENT_TYPE:
            return
        raw = event.get("data")
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        self.status["events_received"] += 1
        self.status["last_event"] = int(time.time())
        device_id = _field(data, "device_id")
        if (device_id and device_id not in self.device_names
                and time.monotonic() - self.registry_requested_at > REGISTRY_REFRESH_SECONDS):
            await self.request_registry()  # a phone registered after we connected
        req = share_event_to_request(data, device_names=self.device_names)
        if req is None:
            self.status["ignored"] += 1
            log.info("HA share: ignored a share with no URL or text (keys: %s)", sorted(data))
            return
        log.info("HA share: received %s from %s", "a link" if req.url or find_urls(req.text) else "text",
                 req.sender or "a phone")
        try:
            self.queue.put_nowait(req)
        except asyncio.QueueFull:
            _set_error(self.status, "too many shares waiting; one was dropped")
            log.error("HA share: processing queue full, share dropped")


async def _process(req: IngestRequest, processor: Any, status: dict[str, Any]) -> "Item":
    """``processor.process(req)``, retried until the store can be written again."""
    while True:
        try:
            return await processor.process(req)
        except StorageError as exc:
            message = f"could not save a share, retrying in {STORAGE_RETRY_SECONDS:.0f} s: {exc}"
            _set_error(status, message)
            _publish(status, "error", message[:1].upper() + message[1:])
            log.error("HA share: %s", message)
            await asyncio.sleep(STORAGE_RETRY_SECONDS)


async def _consume(queue: "asyncio.Queue[IngestRequest]", processor: Any, status: dict[str, Any]) -> None:
    while True:
        req = await queue.get()
        try:
            item = await _process(req, processor, status)
            status["items_created"] += 1
            status["last_item"] = {
                "id": item.id, "title": item.title, "status": item.status, "at": int(time.time())
            }
            if status["connected"]:
                _publish(status, "ok", _listening_detail(status))
            log.info("HA share: queued %r (%s, %s)", item.title, item.id, item.status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the processor should never raise; survive if it does
            log.exception("HA share: processing a share failed")
            _set_error(status, f"processing failed: {exc!r}")
        finally:
            queue.task_done()


async def _session(
    session: aiohttp.ClientSession,
    url: str,
    token: str,
    queue: "asyncio.Queue[IngestRequest]",
    status: dict[str, Any],
    device_names: dict[str, str],
) -> None:
    ws = await asyncio.wait_for(
        session.ws_connect(url, autoping=True, heartbeat=None, max_msg_size=MAX_MSG_SIZE,
                           timeout=aiohttp.ClientWSTimeout(ws_close=5.0)),
        HANDSHAKE_TIMEOUT,
    )
    try:
        conn = _Connection(ws, queue, status, device_names)
        status["ha_version"] = await conn.authenticate(token)
        await conn.serve()
    finally:
        status["connected"] = False
        with contextlib.suppress(Exception):
            await ws.close()


async def run(state: HAShareState) -> None:
    """Long-running worker; returns at once when disabled or not running under HA."""
    url = os.environ.get("SENDTOX3_HA_WS_URL", "").strip() or DEFAULT_WS_URL
    status = _init_status(state, url)
    token = (os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or "").strip()
    processor = getattr(state, "processor", None)
    reason = ""
    if not state.opts.ha_share_enabled:
        reason = "disabled in the app options"
    elif not token:
        reason = "not running under Home Assistant (no SUPERVISOR_TOKEN)"
    elif processor is None:
        reason = "no processor available"
    if reason:
        status.update(enabled=False, reason=reason)
        _publish(status, "disabled", reason[:1].upper() + reason[1:] + ".")
        log.info("HA share: %s", reason)
        return

    queue: asyncio.Queue[IngestRequest] = asyncio.Queue(maxsize=QUEUE_MAX)
    consumer = asyncio.create_task(_consume(queue, processor, status), name="ha_share-process")
    device_names: dict[str, str] = {}
    delay = BACKOFF_INITIAL
    failures = 0  # attempts in a row that did not give a stable session
    timeout = aiohttp.ClientTimeout(total=None, connect=HANDSHAKE_TIMEOUT, sock_connect=HANDSHAKE_TIMEOUT)
    log.info("HA share: connecting to %s", url)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                started = time.monotonic()
                level = "warning"  # transient (HA restarting, network); auth problems are errors
                auth = False
                try:
                    await _session(session, url, token, queue, status, device_names)
                    message = "connection to Home Assistant ended"
                except asyncio.CancelledError:
                    raise
                except HAAuthError as exc:
                    message = f"Home Assistant rejected the token: {exc}"
                    auth = True
                    level = "error"
                except (aiohttp.ClientError, OSError, TimeoutError, HAProtocolError) as exc:
                    message = f"Home Assistant connection failed: {_describe(exc)}"
                except Exception as exc:  # never let the worker die
                    log.exception("HA share: unexpected error")
                    message = f"unexpected error: {exc!r}"
                    level = "error"
                if status["last_connected"] and time.monotonic() - started >= STABLE_SECONDS:
                    delay, failures = BACKOFF_INITIAL, 0
                # A rejected token backs off from AUTH_BACKOFF_MIN; anything else retries
                # quickly again, also right after a token problem.
                cap = AUTH_BACKOFF_MAX if auth else BACKOFF_MAX
                delay = max(delay, AUTH_BACKOFF_MIN) if auth else min(delay, BACKOFF_MAX)
                failures += 1
                _set_error(status, message)
                status["reconnects"] += 1
                wait = delay * random.uniform(0.9, 1.1)
                _publish(status, level, f"{message}; retrying in {wait:.0f} s")
                # Retries come every few seconds while Core is down: log the first few,
                # then about once a minute.
                loud = failures <= QUIET_AFTER_FAILURES or failures % max(1, round(60 / cap)) == 0
                log.log(logging.WARNING if loud else logging.DEBUG, "HA share: %s; retrying in %.0f s",
                        message, wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2, cap)
    finally:
        status["connected"] = False
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await consumer
