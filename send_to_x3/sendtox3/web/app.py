"""aiohttp application factory: routes, framing rules, error handling, access log.

Routes::

    GET  /healthz                         open; HA watchdog
    POST /api/v1/ingest                   ingest token            (ingest_api.py)
    GET  /device/v1/catalog|items|deliver device token (Bearer)   (device_api.py)
    GET  /opds, /opds/items/<id>.epub     device token (Basic)    (opds.py)
    GET  /, /admin/...                    HA ingress or admin token (admin.py)
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from .. import __version__
from ..app_state import AppState, resolve_secrets
from ..config import Options
from ..store import Store
from . import admin, device_api, ingest_api, opds
from .common import STATE_KEY, is_device_path, json_error, json_response, text_response

log = logging.getLogger(__name__)
access_log = logging.getLogger("sendtox3.access")

_ADMIN_PREFIXES = ("/admin",)


def _is_admin_path(path: str) -> bool:
    return path == "/" or path.startswith(_ADMIN_PREFIXES)


def _error_response(request: web.Request, status: int, reason: str) -> web.Response:
    if is_device_path(request.path) or request.path.startswith("/api/"):
        return json_error(status, reason.lower() if reason else "error")
    return text_response(f"{status} {reason}\n", status=status)


@web.middleware
async def framing_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Small explicit-length bodies for every error, ``Connection: close`` for the X3,
    security headers for the admin UI, and no redirect ever leaves this app."""
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        if exc.status in (204, 304) or 300 <= exc.status < 400:
            log.error("suppressed a %s response for %s", exc.status, request.path)
            resp = _error_response(request, 500, "Internal Server Error")
        else:
            resp = _error_response(request, exc.status, exc.reason)
            if exc.status == 405 and "Allow" in exc.headers:
                resp.headers["Allow"] = exc.headers["Allow"]
    except Exception:  # noqa: BLE001
        log.exception("unhandled error for %s %s", request.method, request.path)
        resp = _error_response(request, 500, "Internal Server Error")
    if is_device_path(request.path):
        resp.force_close()
    if _is_admin_path(request.path) and not request.path.startswith("/admin/static/"):
        for k, v in admin.SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
    return resp


async def healthz(request: web.Request) -> web.Response:
    """Open liveness probe (HA watchdog). Touches the database so a broken store shows up."""
    request.app[STATE_KEY].store.kv_get("healthz")
    return json_response({"ok": True, "version": __version__})


class AccessLogger(AbstractAccessLogger):
    """Debug-level access log without query strings (they may carry ``?token=``)."""

    def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
        if access_log.isEnabledFor(logging.DEBUG):
            access_log.debug("%s %s %s %s %.0fms", request.remote, request.method, request.path,
                             response.status, time * 1000)

    @property
    def enabled(self) -> bool:
        return access_log.isEnabledFor(logging.DEBUG)


def create_app(opts: Options, store: Store, processor: Any, *, state: Optional[AppState] = None) -> web.Application:
    """Build the app. Without ``state`` a default one is created (tokens resolved from the
    store, ``base_url`` from ``opts.server_host`` if set)."""
    if state is None:
        configured = resolve_secrets(opts, store)
        state = AppState(opts=opts, store=store, processor=processor, configured_secrets=configured)
        if opts.server_host:
            from ..plugin_bundle import INVALID_SERVER_HOST, _from_server_host

            try:
                state.base_url, state.base_url_source = _from_server_host(opts.server_host, opts.port), "options"
            except ValueError as exc:
                log.warning("server_host is invalid: %s", exc)
                state.base_url_source = INVALID_SERVER_HOST
    for name in ("ingest_token", "device_token", "admin_token"):
        if not getattr(opts, name):
            raise ValueError(f"{name} is empty; call app_state.resolve_secrets first")

    app = web.Application(client_max_size=ingest_api.MAX_INGEST_BYTES + 1024 * 1024,
                          middlewares=[framing_middleware])
    app[STATE_KEY] = state
    app[admin.INGRESS_IPS_KEY] = admin.DEFAULT_INGRESS_IPS
    app[admin.SECRET_KEY] = store.secret("admin_session_secret") or secrets.token_urlsafe(24)
    app.router.add_get("/healthz", healthz)
    ingest_api.add_routes(app)
    device_api.add_routes(app)
    opds.add_routes(app)
    admin.add_routes(app)
    return app
