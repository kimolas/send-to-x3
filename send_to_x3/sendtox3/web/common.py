"""Shared pieces of the web layer: app keys, token checks, framing-safe responses.

Every response this app sends has an explicit Content-Length, is never compressed, is
never 204/304/3xx (CrossPoint's HTTP client mishandles those), and device-facing
responses carry ``Connection: close``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import socket
import struct
import sys
from typing import Any, Optional

from aiohttp import web

from ..app_state import AppState

STATE_KEY: web.AppKey[AppState] = web.AppKey("sendtox3_state", AppState)

#: Paths whose clients are the X3 itself.
DEVICE_PREFIXES: tuple[str, ...] = ("/device/", "/opds")


def get_state(request: web.Request) -> AppState:
    return request.app[STATE_KEY]


def tokens_equal(given: str, expected: str) -> bool:
    """Constant-time comparison; an empty expected token never matches."""
    if not expected or not given:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def bearer_token(request: web.Request) -> str:
    """Token from ``Authorization: Bearer <t>`` (case-insensitive scheme) or ``?token=``."""
    auth = request.headers.get("Authorization", "")
    scheme, _, value = auth.strip().partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.query.get("token", "")


def basic_password(request: web.Request) -> Optional[str]:
    """Password from an ``Authorization: Basic`` header (the user name is ignored)."""
    auth = request.headers.get("Authorization", "")
    scheme, _, value = auth.strip().partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    _, sep, password = decoded.partition(":")
    return password if sep else None


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def json_response(data: Any, status: int = 200, headers: Optional[dict[str, str]] = None) -> web.Response:
    resp = web.Response(body=json_bytes(data), status=status, headers=headers)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def json_error(status: int, error: str, headers: Optional[dict[str, str]] = None) -> web.Response:
    return json_response({"ok": False, "error": error}, status=status, headers=headers)


def bytes_response(data: bytes, content_type: str, *, status: int = 200,
                   headers: Optional[dict[str, str]] = None) -> web.Response:
    """A body with an explicit Content-Length and no validators (no ETag/Last-Modified)."""
    resp = web.Response(body=data, status=status, headers=headers)
    resp.headers["Content-Type"] = content_type
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


#: How long :func:`send_whole` waits, after the last byte, for the client to acknowledge
#: everything: a base plus 50 ms per KB (the X3 reads a 900 KB book in a few seconds).
ACK_WAIT_BASE_SECONDS = 20.0
_SO_NWRITE = 0x1024  # macOS/BSD: bytes in the send queue not yet acknowledged


def _unacked_bytes(transport: Any) -> Optional[int]:
    """Bytes the peer has not acknowledged yet (Linux SIOCOUTQ, BSD SO_NWRITE), or None."""
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is None:
        return None
    try:
        fd = sock.fileno()
        if fd < 0:
            return None
        if sys.platform.startswith("linux"):
            import fcntl
            import termios

            return struct.unpack("i", fcntl.ioctl(fd, termios.TIOCOUTQ, b"\0\0\0\0"))[0]
        if sys.platform == "darwin":
            return int(sock.getsockopt(socket.SOL_SOCKET, _SO_NWRITE))
    except (OSError, ValueError, AttributeError):
        return None
    return None


class _CloseWatch(asyncio.Protocol):
    """Sits in front of aiohttp's protocol while a bundle is sent, to see how the client
    leaves. aiohttp reports a clean close and a reset alike ("Connection lost"); here a
    FIN (``eof_received``, the socket still open) records how many bytes were then still
    unacknowledged, so "read the whole body, then closed" is told from "gave up"."""

    def __init__(self, inner: Any, transport: Any) -> None:
        self.inner = inner
        self.transport = transport
        self.eof_unacked: Optional[int] = None
        self.lost = False
        self.changed = asyncio.Event()  # wakes the wait in _acknowledged at once

    def connection_made(self, transport: Any) -> None:
        self.inner.connection_made(transport)

    def data_received(self, data: bytes) -> None:
        self.inner.data_received(data)

    def eof_received(self) -> Optional[bool]:
        left = _unacked_bytes(self.transport)
        self.eof_unacked = 0 if left is None else left
        self.changed.set()
        return self.inner.eof_received()

    def connection_lost(self, exc: Optional[BaseException]) -> None:
        self.lost = True
        self.changed.set()
        self.inner.connection_lost(exc)

    def pause_writing(self) -> None:
        self.inner.pause_writing()

    def resume_writing(self) -> None:
        self.inner.resume_writing()


async def _acknowledged(transport: Any, watch: Optional[_CloseWatch], size: int) -> bool:
    """True once the client acknowledged every byte (or closed cleanly after that), False
    when it went away first. Where the kernel cannot say (other systems), the emptied
    write buffer has to do."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ACK_WAIT_BASE_SECONDS + size / 20_000
    while True:
        if watch is not None and watch.eof_unacked is not None:
            return watch.eof_unacked == 0
        if transport is None or transport.is_closing() or (watch is not None and watch.lost):
            return False
        if transport.get_write_buffer_size() == 0:
            left = _unacked_bytes(transport)
            if left is None or left == 0:
                return True
        if loop.time() > deadline:
            return False
        if watch is None:
            await asyncio.sleep(0.01)
            continue
        try:  # the client's FIN or reset ends the wait at once
            await asyncio.wait_for(watch.changed.wait(), 0.01)
        except asyncio.TimeoutError:
            pass


async def send_whole(request: web.Request, data: bytes, content_type: str) -> tuple[web.StreamResponse, bool]:
    """Send ``data`` framed like :func:`bytes_response` (explicit Content-Length, no
    validators, ``Connection: close`` on device paths) and report whether the client
    received all of it: its TCP stack acknowledged every byte. Handing the bytes to the
    socket is not enough, because the kernel buffers a whole small book at once, so a
    download cancelled right away would otherwise count as received.

    Relies on aiohttp's default ``handler_cancellation=False`` (``__main__``): with
    cancellation on, a client that closes first cancels the handler here.
    """
    resp = web.StreamResponse(status=200)
    resp.content_length = len(data)
    resp.headers["Content-Type"] = content_type
    resp.headers["Cache-Control"] = "no-store"
    if is_device_path(request.path):
        resp.force_close()  # before prepare(): the Connection header is written there
    transport = request.transport
    watch: Optional[_CloseWatch] = None
    inner = transport.get_protocol() if transport is not None else None
    if inner is not None and hasattr(transport, "set_protocol"):
        watch = _CloseWatch(inner, transport)
        transport.set_protocol(watch)
    try:
        try:
            await resp.prepare(request)
            await resp.write(data)
            await resp.write_eof()
        except ConnectionError:  # includes aiohttp's ClientConnectionResetError
            return resp, False
        return resp, await _acknowledged(transport, watch, len(data))
    finally:
        if watch is not None and not transport.is_closing():
            transport.set_protocol(inner)


def text_response(text: str, status: int = 200, headers: Optional[dict[str, str]] = None) -> web.Response:
    return bytes_response(text.encode("utf-8"), "text/plain; charset=utf-8", status=status, headers=headers)


def is_device_path(path: str) -> bool:
    return path.startswith(DEVICE_PREFIXES)


def parse_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
