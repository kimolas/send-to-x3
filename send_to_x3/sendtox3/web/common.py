"""Shared pieces of the web layer: app keys, token checks, framing-safe responses.

Every response this app sends has an explicit Content-Length, is never compressed, is
never 204/304/3xx (CrossPoint's HTTP client mishandles those), and device-facing
responses carry ``Connection: close``.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
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


def text_response(text: str, status: int = 200, headers: Optional[dict[str, str]] = None) -> web.Response:
    return bytes_response(text.encode("utf-8"), "text/plain; charset=utf-8", status=status, headers=headers)


def is_device_path(path: str) -> bool:
    return path.startswith(DEVICE_PREFIXES)


def parse_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
