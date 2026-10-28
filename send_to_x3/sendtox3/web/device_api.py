"""Endpoints for the CrossPoint 2.x plugin (``Authorization: Bearer <device_token>``).

- ``GET /device/v1/catalog?page=P&limit=L&offset=O``  catalog rows (JSON); the first row is
  "Get N new" (or "Nothing new right now"); ``offset`` only from newer firmware
- ``GET /device/v1/items/<id>.epub|.txt``    one item; marks ``delivered.catalog``
- ``GET /device/v1/items/all-<key>.epub|.txt`` the "Get N new" bundle; marks ``delivered.fetch``
  once all of it was sent, or, with ``?confirm=1`` (the plugin reports saved files), when the
  X3 confirms it. ``none.epub`` and replaced bundles answer 404.
- ``POST /device/v1/fetched`` body = the SD path the X3 saved (its ``book.downloaded`` event);
  always 200, so the X3's event queue never stalls on it
- ``GET /device/v1/deliver?ev=&ts=``         zero-tap ``sleep.enter`` delivery or 404 (always 404
  when the auto_delivery option is off, the default)

The firmware never follows redirects on these, treats any 2xx as success (an empty 200
would become an empty file), needs Content-Length for progress, and never decodes gzip.
"""

from __future__ import annotations

import asyncio
import re

from aiohttp import web

from .. import delivery
from ..plugin_bundle import PLUGIN_NAME
from .common import (bearer_token, bytes_response, get_state, json_bytes, json_error, json_response, parse_int,
                     send_whole, text_response, tokens_equal)

CONTENT_TYPES = {"epub": "application/epub+zip", "txt": "text/plain; charset=utf-8"}
ITEM_ID_RE = r"[a-z2-7]{10}"
# Event ids are "%08lx-%lu" and ts a unix time; accept any short URL-safe token.
_EVENT_PARAM = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")
AUTO_DELIVERY_OFF_WARNING = ("The X3 still requests sleep delivery although auto_delivery is off: reinstall the "
                             "plugin to stop it (it also joins Wi-Fi at each sleep).")
OLD_PLUGIN_WARNING = ("The X3 uses the plugin from Send to X3 0.2.0 or older: install it again from Setup (needed for "
                      "Open on the Send to X3 firmware, and for paging on new CrossPoint builds).")


def _authorized(request: web.Request) -> bool:
    return tokens_equal(bearer_token(request), get_state(request).opts.device_token)


def _unauthorized() -> web.Response:
    return json_error(401, "invalid or missing device token")


def _seen(request: web.Request, detail: str) -> None:
    state = get_state(request)
    if (state.status.get("device_api") or {}).get("old_plugin"):
        # The download right after the catalog must not hide it: kept until a catalog
        # request shows the current device.json.
        state.set_status("device_api", "warning", f"{OLD_PLUGIN_WARNING} Last request: {detail}.", old_plugin=True)
    else:
        state.set_status("device_api", "ok", detail)


async def catalog(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _unauthorized()
    state = get_state(request)
    page = parse_int(request.query.get("page"), 1)
    limit = parse_int(request.query.get("limit"), delivery.PAGE_SIZE + 1)
    # 2.0.0rc and develop 3f387a9 do not know {offset} and send it as the literal text.
    raw_offset = request.query.get("offset", "")
    if raw_offset.isascii() and raw_offset.isdigit():
        # int() refuses more than 4300 digits; anything over 19 is past the end anyway.
        offset = int(raw_offset) if len(raw_offset) <= 19 else delivery.START_MAX
    else:
        offset = None
    body = await asyncio.to_thread(delivery.catalog_page, state.store, page, limit, offset=offset,
                                   fmt=state.opts.delivery_format)
    # Only develop c3b16da and later (the Send to X3 firmware too) send limit = page_size,
    # and only with a device.json that has no {offset}: one from 0.2.0 or older.
    old_plugin = "offset" not in request.query and limit == delivery.PAGE_SIZE
    if body["items"] and body["items"][0].get("title") == delivery.FETCH_FAILED_TITLE:
        state.set_status("device_api", "warning", "\"Get N new\" could not be prepared; see the log",
                         old_plugin=old_plugin)
    elif old_plugin:
        state.set_status("device_api", "warning", OLD_PLUGIN_WARNING, old_plugin=True)
    else:
        state.set_status("device_api", "ok", f"catalog page {max(1, page)}")
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


async def fetch_file(request: web.Request) -> web.StreamResponse:
    if not _authorized(request):
        return _unauthorized()
    state = get_state(request)
    fmt = request.match_info["fmt"]
    bundle = await asyncio.to_thread(delivery.fetch_lookup, state.store, request.match_info["bundle_id"], fmt)
    if bundle is None:
        # "Nothing new right now", or a bundle replaced since the X3 loaded its list.
        _seen(request, "fetch: nothing new")
        return text_response("nothing new: open Send to X3 again\n", status=404)
    confirm = request.query.get("confirm") == "1"
    if confirm:  # recorded first, so a report can always be matched, whatever happens below
        await asyncio.to_thread(delivery.fetch_sent, state.store, bundle, complete=False, confirm=True)
    resp, whole = await send_whole(request, bundle.data, CONTENT_TYPES[fmt])
    await asyncio.to_thread(delivery.fetch_sent, state.store, bundle, complete=whole, confirm=confirm)
    if not whole:
        _seen(request, f"the X3 stopped downloading {bundle.title!r}; it stays new")
    else:
        _seen(request, f"sent {bundle.title!r} ({len(bundle.data):,} bytes)"
                       + ("; waiting for the X3 to confirm it" if confirm else ""))
    return resp


#: The firmware fills the body from ``{event.path}``; anything longer is not a path.
MAX_CONFIRM_BYTES = 1024


async def fetched(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _unauthorized()
    state = get_state(request)
    plugin = request.query.get("plugin", "")
    if plugin and plugin != PLUGIN_NAME:
        # book.downloaded fires for every plugin's catalog downloads; theirs are not ours.
        return json_response({"ok": True, "confirmed": 0})
    path = (await request.content.read(MAX_CONFIRM_BYTES)).decode("utf-8", "replace").strip()
    ids = await asyncio.to_thread(delivery.fetch_confirmed, state.store, path)
    if ids:
        _seen(request, f"the X3 confirmed {path.rsplit('/', 1)[-1]!r} ({len(ids)} item(s))")
    return json_response({"ok": True, "confirmed": len(ids)})


def add_routes(app: web.Application) -> None:
    app.router.add_get("/device/v1/catalog", catalog, allow_head=False)
    app.router.add_get(f"/device/v1/items/{{item_id:{ITEM_ID_RE}}}.{{fmt:(epub|txt)}}", item_file, allow_head=False)
    app.router.add_get(f"/device/v1/items/{{bundle_id:{delivery.FETCH_ID_RE}|{delivery.NOTHING_NEW_ID}}}"
                       ".{fmt:(epub|txt)}", fetch_file, allow_head=False)
    app.router.add_get("/device/v1/deliver", deliver, allow_head=False)
    app.router.add_post("/device/v1/fetched", fetched)
