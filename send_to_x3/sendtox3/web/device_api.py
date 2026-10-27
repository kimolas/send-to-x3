"""Endpoints for the CrossPoint 2.x plugin (``Authorization: Bearer <device_token>``).

- ``GET /device/v1/catalog?page=P&limit=L``  catalog rows (JSON)
- ``GET /device/v1/items/<id>.epub|.txt``    one item; marks ``delivered.catalog``
- ``GET /device/v1/deliver?ev=&ts=``         zero-tap ``sleep.enter`` delivery or 404 (always 404
  when the auto_delivery option is off)

The firmware never follows redirects on these, treats any 2xx as success (an empty 200
would become an empty file), needs Content-Length for progress, and never decodes gzip.
"""

from __future__ import annotations

import asyncio
import re

from aiohttp import web

from .. import delivery
from .common import bytes_response, get_state, json_bytes, json_error, parse_int, text_response, tokens_equal, bearer_token

CONTENT_TYPES = {"epub": "application/epub+zip", "txt": "text/plain; charset=utf-8"}
ITEM_ID_RE = r"[a-z2-7]{10}"
# Event ids are "%08lx-%lu" and ts a unix time; accept any short URL-safe token.
_EVENT_PARAM = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")
AUTO_DELIVERY_OFF_WARNING = ("The X3 still requests sleep delivery although auto_delivery is off: reinstall the "
                             "plugin to stop it (it also joins Wi-Fi at each sleep).")


def _authorized(request: web.Request) -> bool:
    return tokens_equal(bearer_token(request), get_state(request).opts.device_token)


def _unauthorized() -> web.Response:
    return json_error(401, "invalid or missing device token")


def _seen(request: web.Request, detail: str) -> None:
    get_state(request).set_status("device_api", "ok", detail)


async def catalog(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _unauthorized()
    state = get_state(request)
    page = parse_int(request.query.get("page"), 1)
    limit = parse_int(request.query.get("limit"), 9)
    body = delivery.catalog_page(state.store, page, limit)
    _seen(request, f"catalog page {max(1, page)}")
    return bytes_response(json_bytes(body), "application/json; charset=utf-8")


async def item_file(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _unauthorized()
    state = get_state(request)
    item_id = request.match_info["item_id"]
    fmt = request.match_info["fmt"]
    item = state.store.get(item_id)
    if item is None:
        return json_error(404, "no such item")
    try:
        data = await asyncio.to_thread(state.store.file_bytes, item_id, fmt)
    except FileNotFoundError:
        return json_error(404, "item file missing")
    state.store.mark_delivered(item_id, "catalog")
    _seen(request, f"downloaded {item.title!r}")
    return bytes_response(data, CONTENT_TYPES[fmt])


async def deliver(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _unauthorized()
    state = get_state(request)
    ev = request.query.get("ev", "")
    ts = request.query.get("ts", "")
    if not _EVENT_PARAM.match(ev) or not _EVENT_PARAM.match(ts):
        return json_error(400, "ev and ts query parameters are required")
    if not state.opts.auto_delivery:
        # A device.json installed before auto_delivery was turned off still asks at every
        # sleep. Answer like "nothing new" (the firmware keeps the event queued and its
        # outbox is capped) and never mark items "auto", which would hide them from LAN
        # push and the catalog's "New" label.
        state.set_status("device_api", "warning", AUTO_DELIVERY_OFF_WARNING)
        return text_response("nothing new\n", status=404)
    data = await asyncio.to_thread(delivery.build_auto_delivery, state.store, ev, ts)
    if data is None:
        _seen(request, "sleep check: nothing new")
        return text_response("nothing new\n", status=404)
    _seen(request, f"sleep delivery {len(data):,} bytes")
    return bytes_response(data, CONTENT_TYPES["epub"])


def add_routes(app: web.Application) -> None:
    app.router.add_get("/device/v1/catalog", catalog, allow_head=False)
    app.router.add_get(f"/device/v1/items/{{item_id:{ITEM_ID_RE}}}.{{fmt:(epub|txt)}}", item_file, allow_head=False)
    app.router.add_get("/device/v1/deliver", deliver, allow_head=False)
