"""Admin UI: queue, previews, paste/upload, channel health, tokens, setup and plugin install.

Authentication:

- Through Home Assistant ingress (peer ``172.30.32.2`` **and** an ``X-Ingress-Path``
  header) the request is trusted: HA already authenticated the user. Links are prefixed
  with the ingress path.
- On the LAN port, ``?token=<admin_token>`` once (or the login form) sets an HttpOnly,
  SameSite=Strict session cookie signed with a key derived from the admin token.

Every POST form carries a CSRF token bound to the session (HMAC of the session id with
a server secret). Responses are never redirects: POST handlers render the page directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, quote

import jinja2
from aiohttp import web

from .. import __version__, delivery, plugin_bundle
from ..app_state import CHANNEL_LABELS, AppState, rotate_secret
from ..ingest import StorageError
from ..models import IngestRequest
from .common import bytes_response, get_state, text_response, tokens_equal
from .ingest_api import (MAX_INGEST_BYTES, STORAGE_STATUS, results_payload, run_processor, storage_failed,
                         storage_ok)

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"
ANDROID_IMPORT_FILE = "send-to-x3-http-shortcuts.json"
#: The login form carries one short field; never buffer more from an unauthenticated client.
LOGIN_MAX_BYTES = 4096
LOGIN_READ_TIMEOUT = 10.0

SESSION_COOKIE = "sendtox3_admin"
SESSION_MAX_AGE = 30 * 86400
PAGE_SIZE = 50
DEFAULT_INGRESS_IPS = frozenset({"172.30.32.2"})

INGRESS_IPS_KEY: web.AppKey[frozenset] = web.AppKey("sendtox3_ingress_ips", frozenset)
SECRET_KEY: web.AppKey[str] = web.AppKey("sendtox3_admin_secret", str)
JINJA_KEY: web.AppKey[jinja2.Environment] = web.AppKey("sendtox3_jinja", jinja2.Environment)

_STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
}
_PREFIX_RE = re.compile(r"^/[A-Za-z0-9/_.~-]*$")

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",  # never leak ?token= to another site
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'self'; form-action 'self'; base-uri 'none'; object-src 'none'"
    ),
}


# --------------------------------------------------------------------------------------
# Sessions and CSRF
# --------------------------------------------------------------------------------------


@dataclass
class AdminContext:
    base: str  # link prefix: "" on the LAN port, the X-Ingress-Path under ingress
    via_ingress: bool
    session_id: str
    csrf: str
    new_cookie: Optional[str] = None


def _mac(secret: str, msg: str) -> str:
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _session_key(request: web.Request) -> str:
    # Rotating the admin token invalidates every session.
    return _mac(request.app[SECRET_KEY], "session:" + get_state(request).opts.admin_token)


def make_session_cookie(request: web.Request, sid: str, issued: Optional[int] = None) -> str:
    issued = int(issued or time.time())
    payload = f"{issued}.{sid}"
    return f"{payload}.{_mac(_session_key(request), payload)}"


def verify_session_cookie(request: web.Request, value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        issued_s, sid, sig = value.split(".")
        issued = int(issued_s)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _mac(_session_key(request), f"{issued}.{sid}")):
        return None
    if not (0 <= time.time() - issued <= SESSION_MAX_AGE):
        return None
    return sid


def csrf_token(request: web.Request, session_id: str) -> str:
    return _mac(request.app[SECRET_KEY], "csrf:" + session_id)


def _ingress_prefix(request: web.Request) -> Optional[str]:
    """The ingress path if this request really came through HA ingress, else None."""
    path = request.headers.get("X-Ingress-Path")
    if path is None or request.remote not in request.app[INGRESS_IPS_KEY]:
        return None
    path = path.strip().rstrip("/")
    return path if (path == "" or _PREFIX_RE.match(path)) else ""


def authenticate(request: web.Request) -> Optional[AdminContext]:
    state = get_state(request)
    prefix = _ingress_prefix(request)
    if prefix is not None:
        sid = "ingress:" + request.headers.get("X-Remote-User-Id", "")
        return AdminContext(prefix, True, sid, csrf_token(request, sid))
    sid = verify_session_cookie(request, request.cookies.get(SESSION_COOKIE))
    if sid:
        return AdminContext("", False, sid, csrf_token(request, sid))
    token = request.query.get("token", "")
    if token and tokens_equal(token, state.opts.admin_token):
        sid = secrets.token_urlsafe(16).replace(".", "_")
        return AdminContext("", False, sid, csrf_token(request, sid), new_cookie=make_session_cookie(request, sid))
    return None


def _csrf_ok(request: web.Request, ctx: AdminContext, form: Any) -> bool:
    given = str(form.get("csrf", "")) if form is not None else ""
    if not given or not hmac.compare_digest(given, ctx.csrf):
        return False
    if not ctx.via_ingress:  # defense in depth: a cross-site POST carries a foreign Origin
        origin = request.headers.get("Origin")
        if origin and origin != "null" and origin.split("://", 1)[-1] != request.host:
            return False
    return True


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def _fmt_time(ts: Any) -> str:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts > 0 else ""


def _fmt_size(n: Any) -> str:
    n = int(n or 0)
    if n < 1000:
        return f"{n} B"
    if n < 1_000_000:
        return f"{n / 1000:.0f} KB"
    return f"{n / 1_000_000:.1f} MB"


def make_jinja() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
    )
    env.filters["ts"] = _fmt_time
    env.filters["size"] = _fmt_size
    return env


def render(request: web.Request, ctx: AdminContext, template: str, *, status: int = 200,
           flash: str = "", flash_kind: str = "info", **values: Any) -> web.Response:
    env = request.app[JINJA_KEY]
    state = get_state(request)
    html = env.get_template(template).render(
        base=ctx.base, csrf=ctx.csrf, via_ingress=ctx.via_ingress, version=__version__,
        flash=flash, flash_kind=flash_kind, opts=state.opts, **values,
    )
    resp = bytes_response(html.encode("utf-8"), "text/html; charset=utf-8", status=status)
    if ctx.new_cookie:
        resp.set_cookie(SESSION_COOKIE, ctx.new_cookie, max_age=SESSION_MAX_AGE, httponly=True,
                        samesite="Strict", path="/")
    return resp


def login_page(request: web.Request, *, status: int = 401, error: str = "") -> web.Response:
    ctx = AdminContext("", False, "", "")
    return render(request, ctx, "login.html", status=status, flash=error, flash_kind="error" if error else "info")


def _admin_handler(fn):
    """Decorator: authenticate, then call ``fn(request, ctx)``."""

    async def wrapper(request: web.Request) -> web.StreamResponse:
        ctx = authenticate(request)
        if ctx is None:
            return login_page(request)
        return await fn(request, ctx)

    wrapper.__name__ = fn.__name__
    return wrapper


def _admin_post(fn):
    """Decorator for POST forms: authenticate, read the form, check CSRF."""

    async def wrapper(request: web.Request) -> web.StreamResponse:
        ctx = authenticate(request)
        if ctx is None:
            return login_page(request)
        try:
            form = await request.post()
        except web.HTTPRequestEntityTooLarge:
            return await queue_view(request, ctx, flash=f"That upload is larger than "
                                    f"{MAX_INGEST_BYTES // (1024 * 1024)} MB.", flash_kind="error", status=413)
        if not _csrf_ok(request, ctx, form):
            return render(request, ctx, "message.html", status=403, heading="Form expired",
                          flash="This form is no longer valid (security check failed). Reload the page and try again.",
                          flash_kind="error", lines=[])
        return await fn(request, ctx, form)

    wrapper.__name__ = fn.__name__
    return wrapper


# --------------------------------------------------------------------------------------
# Data for the pages
# --------------------------------------------------------------------------------------


def channel_rows(state: AppState) -> list[dict[str, Any]]:
    supervisor = plugin_bundle.under_supervisor()
    enabled = {
        "device_api": True,
        "opds": True,
        "lan_push": bool(state.opts.device_host),
        "imap": state.opts.imap_enabled,
        "ha_share": state.opts.ha_share_enabled and supervisor,
    }
    hints = {
        "device_api": "No request from the X3 plugin yet.",
        "opds": "No request from an OPDS client yet.",
        "lan_push": "Set device_host in the options to push files to the X3." if not state.opts.device_host else "",
        "imap": "Turn on imap_enabled in the options to receive email." if not state.opts.imap_enabled else "",
        "ha_share": ("Only available when running as a Home Assistant app." if not supervisor
                     else "Turned off (ha_share_enabled)." if not state.opts.ha_share_enabled else ""),
    }
    rows = []
    for key, label in CHANNEL_LABELS.items():
        st = state.channel_status(key)
        if not enabled[key]:
            st = {"state": "disabled", "detail": hints[key], "updated": 0}
        elif st["state"] == "unknown" and not st["detail"]:
            st = {**st, "state": "waiting" if key in ("device_api", "opds") else "starting",
                  "detail": hints[key] or "Starting."}
        rows.append({"key": key, "label": label, **st})
    return rows


def delivery_warnings(state: AppState) -> list[str]:
    out = []
    storage = state.status.get(STORAGE_STATUS)
    if isinstance(storage, dict) and storage.get("detail"):
        out.append(f"{storage['detail']} ({_fmt_time(storage.get('updated'))}). Check the free space of the "
                   "Home Assistant data disk.")
    for d in state.store.delivery_stats():
        if int(d.get("hits") or 0) > 1:
            out.append(
                f"The X3 asked {d['hits']} times for the delivery of {_fmt_time(d['created_at'])}: it may be "
                f"failing to save it. Check that {state.opts.inbox_folder} exists on the SD card and has free space."
            )
            break
    return out


def _email_address(user: str, tag: str) -> str:
    """``name+tag@domain`` when a plus-tag filter is configured."""
    user = (user or "").strip()
    if not tag or "@" not in user:
        return user
    local, _, domain = user.rpartition("@")
    return f"{local.split('+', 1)[0]}+{tag}@{domain}"


def _qr_svg(data: str) -> str:
    import segno

    return segno.make(data, error="m").svg_inline(scale=4, dark="#000", light="#fff", svgclass="qr")


def bundle_problem(state: AppState) -> str:
    """Why no file carrying the server address (plugin bundle, phone shortcut) can be
    built for the X3 or a phone right now, or ""."""
    if not state.base_url:
        if state.base_url_source == plugin_bundle.INVALID_SERVER_HOST:
            return ("the server_host option is not a usable address; correct it in the app options "
                    "(for example 192.168.1.50) and restart the app.")
        return "the server address is unknown; set server_host in the app options."
    if (state.base_url_source == "local address" and plugin_bundle.under_supervisor()
            and plugin_bundle.looks_container_internal(state.base_url)):
        # Under Home Assistant: a guess from inside the container (the Supervisor did not
        # answer at start); __main__ keeps retrying. Never write it into the X3's
        # device.json. Without the Supervisor, 172.16/12 may be a real LAN address, so the
        # Setup page only warns (base_url_internal).
        return (f"{state.base_url} is an address inside Docker that the X3 and your phone cannot reach; "
                "set server_host in the app options to the LAN IP address of your Home Assistant machine.")
    return ""


def internal_address_warning(state: AppState) -> str:
    """Which Setup-page warning a 172.16.0.0/12 server address gets: "docker" under Home
    Assistant (its internal network; the X3 cannot reach it), "maybe" for a detected
    address without Home Assistant (plain Docker, or a LAN that really uses that range),
    or "" (no warning: not in that range, reported by the Supervisor as the host's own
    address, or typed into server_host outside Home Assistant)."""
    base_url = state.base_url
    if not base_url or not plugin_bundle.looks_container_internal(base_url):
        return ""
    if plugin_bundle.under_supervisor():
        return "docker" if state.base_url_source in ("local address", "options") else ""
    return "maybe" if state.base_url_source == "local address" else ""


def _setup_values(request: web.Request) -> dict[str, Any]:
    from .. import phone

    state = get_state(request)
    base_url = state.base_url
    values: dict[str, Any] = {
        "base_url": base_url,
        "base_url_source": state.base_url_source,
        "base_url_internal": internal_address_warning(state),
        "bundle_problem": bundle_problem(state),
        "device_token_problem": plugin_bundle.device_token_problem(state.opts.device_token),
        "device_token_rotatable": state.configured_secrets.get("device_token") is False,
        "ios_shortcut_note": phone.IOS_SHORTCUT_NOTE,
        "ingest_url": f"{base_url}/api/v1/ingest" if base_url else "",
        "opds_url": f"{base_url}/opds" if base_url else "",
        "admin_url": f"{base_url}/admin" if base_url else "",
        "tokens": [
            ("Ingest token", "ingest_token", state.opts.ingest_token,
             "Phone shortcuts and other senders: Authorization: Bearer <token>"),
            ("Device token", "device_token", state.opts.device_token,
             "The X3: built into the plugin bundle; OPDS password"),
            ("Admin token", "admin_token", state.opts.admin_token,
             "This page on the LAN: open /admin?token=<token> once"),
        ],
        "email_address": _email_address(state.opts.imap_user, state.opts.imap_plus_tag),
        "qr": {},
    }
    if base_url:
        values["qr"] = {
            "ingest_url": _qr_svg(values["ingest_url"]),
            "ingest_token": _qr_svg(state.opts.ingest_token),
            "opds_url": _qr_svg(values["opds_url"]),
        }
    return values


# --------------------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------------------


async def queue_view(request: web.Request, ctx: AdminContext, *, flash: str = "", flash_kind: str = "info",
                     status: int = 200) -> web.Response:
    state = get_state(request)
    try:
        page = max(1, int(request.query.get("page", "1")))
    except ValueError:
        page = 1
    total = state.store.count()
    items = state.store.list_items(offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE, newest_first=True)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    pending = len(state.store.pending_items())
    return render(request, ctx, "index.html", status=status, flash=flash, flash_kind=flash_kind,
                  items=items, page=page, pages=pages, total=total, pending=pending,
                  unconfirmed=delivery.unconfirmed_fetches(state.store),
                  channels=channel_rows(state), warnings=delivery_warnings(state),
                  deliveries=state.store.delivery_stats()[:5], max_upload_mb=MAX_INGEST_BYTES // (1024 * 1024))


@_admin_handler
async def index(request: web.Request, ctx: AdminContext) -> web.Response:
    return await queue_view(request, ctx)


async def login_get(request: web.Request) -> web.Response:
    ctx = authenticate(request)
    if ctx is not None:
        return await queue_view(request, ctx)
    return login_page(request, status=200)


async def login_post(request: web.Request) -> web.Response:
    """Unauthenticated: read at most LOGIN_MAX_BYTES, with a deadline, before checking."""
    state = get_state(request)
    length = request.content_length
    if length is None or length > LOGIN_MAX_BYTES:
        return login_page(request, status=413, error="That sign-in request is too large.")
    try:
        body = await asyncio.wait_for(request.content.readexactly(length), LOGIN_READ_TIMEOUT)
    except (asyncio.IncompleteReadError, TimeoutError):
        return login_page(request, status=400, error="The sign-in request was incomplete. Try again.")
    form = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    token = str(form.get("token", "")).strip()
    if not tokens_equal(token, state.opts.admin_token):
        await asyncio.sleep(0.5)  # slow down guessing
        return login_page(request, error="That is not the admin token.")
    sid = secrets.token_urlsafe(16).replace(".", "_")
    ctx = AdminContext("", False, sid, csrf_token(request, sid), new_cookie=make_session_cookie(request, sid))
    return await queue_view(request, ctx, flash="Signed in.")


@_admin_post
async def logout(request: web.Request, ctx: AdminContext, form: Any) -> web.Response:
    resp = login_page(request, status=200, error="")
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


@_admin_handler
async def item_view(request: web.Request, ctx: AdminContext) -> web.Response:
    state = get_state(request)
    item = state.store.get(request.match_info["item_id"])
    if item is None:
        return render(request, ctx, "message.html", status=404, heading="Not found",
                      flash="That item does not exist (it may have been deleted).", flash_kind="error", lines=[])
    try:
        preview = (await asyncio.to_thread(state.store.file_bytes, item.id, "txt")).decode("utf-8", "replace")
    except FileNotFoundError:
        preview = ""
    sizes = {}
    for fmt in ("epub", "txt"):
        try:
            sizes[fmt] = state.store.file_path(item.id, fmt).stat().st_size
        except FileNotFoundError:
            sizes[fmt] = 0
    truncated = len(preview) > 200_000
    return render(request, ctx, "item.html", item=item, preview=preview[:200_000], truncated=truncated, sizes=sizes,
                  unconfirmed=delivery.unconfirmed_fetches(state.store))


@_admin_handler
async def item_download(request: web.Request, ctx: AdminContext) -> web.Response:
    state = get_state(request)
    item = state.store.get(request.match_info["item_id"])
    fmt = request.match_info["fmt"]
    if item is None:
        return text_response("no such item\n", status=404)
    try:
        data = await asyncio.to_thread(state.store.file_bytes, item.id, fmt)
    except FileNotFoundError:
        return text_response("file missing\n", status=404)
    filename = f"{item.title}.{fmt}"
    ascii_name = re.sub(r"[^A-Za-z0-9 ._()-]", "_", filename)
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
    ctype = "application/epub+zip" if fmt == "epub" else "text/plain; charset=utf-8"
    return bytes_response(data, ctype, headers={"Content-Disposition": disposition, **_no_frame_headers()})


def _no_frame_headers() -> dict[str, str]:
    return {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


@_admin_post
async def item_delete(request: web.Request, ctx: AdminContext, form: Any) -> web.Response:
    state = get_state(request)
    item = state.store.get(request.match_info["item_id"])
    if item is None:
        return await queue_view(request, ctx, flash="That item was already deleted.")
    state.store.delete(item.id)
    return await queue_view(request, ctx, flash=f"Deleted “{item.title}”.", flash_kind="ok")


@_admin_post
async def item_requeue(request: web.Request, ctx: AdminContext, form: Any) -> web.Response:
    state = get_state(request)
    item = state.store.get(request.match_info["item_id"])
    if item is None:
        return await queue_view(request, ctx, flash="That item no longer exists.", flash_kind="error")
    delivery.requeue(state.store, item.id)
    return await queue_view(request, ctx, flash=f"“{item.title}” will be delivered again.", flash_kind="ok")


@_admin_post
async def send(request: web.Request, ctx: AdminContext, form: Any) -> web.Response:
    state = get_state(request)
    content = str(form.get("content", ""))
    title = str(form.get("title", "")).strip()
    if not content.strip():
        return await queue_view(request, ctx, flash="Paste some text or a link first.", flash_kind="error", status=422)
    req = IngestRequest(kind="auto", source="admin", text=content, title=title)
    return await _process_and_show(request, ctx, req)


@_admin_post
async def upload(request: web.Request, ctx: AdminContext, form: Any) -> web.Response:
    state = get_state(request)
    field = form.get("file")
    if not isinstance(field, web.FileField):
        return await queue_view(request, ctx, flash="Choose a file first.", flash_kind="error", status=422)
    data = await asyncio.to_thread(field.file.read)
    if not data:
        return await queue_view(request, ctx, flash="That file is empty.", flash_kind="error", status=422)
    from ..ingest import sniff_file_type

    ftype = sniff_file_type(field.filename, field.content_type, data)
    if ftype == "unknown":
        return await queue_view(request, ctx, flash="Unsupported file: send a PDF, TXT, HTML or EPUB file.",
                                flash_kind="error", status=422)
    if ftype == "pdf" and not state.opts.pdf_enabled:
        return await queue_view(request, ctx, flash="PDF import is turned off in the app options (pdf_enabled).",
                                flash_kind="error", status=422)
    req = IngestRequest(kind="auto", source="admin", data=data, filename=field.filename or "",
                        content_type=field.content_type or "", title=str(form.get("title", "")).strip())
    return await _process_and_show(request, ctx, req)


async def _process_and_show(request: web.Request, ctx: AdminContext, req: IngestRequest) -> web.Response:
    state = get_state(request)
    try:
        results = await run_processor(state.processor, req, state=state)
    except StorageError as exc:
        return await queue_view(request, ctx, flash=storage_failed(state, exc), flash_kind="error", status=503)
    if results is None:
        return await queue_view(request, ctx, flash="Still processing; it will appear in the queue shortly.",
                                flash_kind="info")
    storage_ok(state)
    payload = results_payload(results)
    if all(r.item.status == "failed" for r in results):
        kind = "error"
    elif all(r.duplicate for r in results):
        kind = "info"
    else:
        kind = "ok"
    return await queue_view(request, ctx, flash=payload["message"], flash_kind=kind)


@_admin_handler
async def setup(request: web.Request, ctx: AdminContext) -> web.Response:
    return render(request, ctx, "setup.html", **_setup_values(request), install_lines=[])


@_admin_handler
async def bundle_zip(request: web.Request, ctx: AdminContext) -> web.Response:
    state = get_state(request)
    problem = bundle_problem(state)
    if problem:
        return text_response(f"Cannot build the bundle: {problem}\n", status=409)
    try:
        data = plugin_bundle.build_bundle_zip(base_url=state.base_url, device_token=state.opts.device_token,
                                              opts=state.opts)
    except ValueError as exc:
        return text_response(f"Cannot build the bundle: {exc}\n", status=409)
    return bytes_response(data, "application/zip", headers={
        "Content-Disposition": 'attachment; filename="sendtox3-sd-card.zip"', **_no_frame_headers()})


@_admin_post
async def install_plugin(request: web.Request, ctx: AdminContext, form: Any) -> web.Response:
    state = get_state(request)
    host = str(form.get("device_host", "")).strip() or state.opts.device_host
    lines = await _install(state, host)
    ok = bool(lines) and lines[-1].startswith("Done")
    return render(request, ctx, "setup.html", **_setup_values(request), install_lines=lines,
                  install_host=host, flash="Plugin installed." if ok else "The plugin was not installed.",
                  flash_kind="ok" if ok else "error")


async def _install(state: AppState, host: str) -> list[str]:
    problem = bundle_problem(state)
    if problem:
        return [f"Error: {problem[0].upper()}{problem[1:]}"]
    return await plugin_bundle.install_to_device(host, base_url=state.base_url,
                                                 device_token=state.opts.device_token, opts=state.opts)


@_admin_post
async def rotate_device_token(request: web.Request, ctx: AdminContext, form: Any) -> web.Response:
    """Replace a generated device token (it can be read from the X3 while File Transfer
    is open) and, when an X3 address is given, reinstall the plugin with the new one."""
    state = get_state(request)
    host = str(form.get("device_host", "")).strip()
    if state.configured_secrets.get("device_token") is not False:
        return render(request, ctx, "setup.html", **_setup_values(request), install_lines=[], install_host=host,
                      flash="The device token is set in the app options (device_token). Change it there, restart "
                            "the app, then reinstall the plugin and update the OPDS password on the X3.",
                      flash_kind="error", status=409)
    rotate_secret(state.opts, state.store, "device_token")
    log.info("device token replaced from the admin Setup page")
    lines = await _install(state, host) if host else []
    installed = bool(lines) and lines[-1].startswith("Done")
    if installed:
        flash, kind = "New device token. The plugin on the X3 now uses it.", "ok"
    else:
        flash, kind = ("New device token. The X3 cannot use the old one any more: "
                       + ("the install below failed, so " if lines else "")
                       + "install the plugin again (button or SD-card bundle below)."), "warning"
    flash += " On a 1.6.5 X3, also enter it as the OPDS password."
    return render(request, ctx, "setup.html", **_setup_values(request), install_lines=lines, install_host=host,
                  flash=flash, flash_kind=kind)


@_admin_handler
async def android_import(request: web.Request, ctx: AdminContext) -> web.Response:
    """HTTP Shortcuts (Android) import file prefilled with this server's URL and token."""
    from .. import phone

    state = get_state(request)
    problem = bundle_problem(state)
    if problem:
        return text_response(f"Cannot build the file: {problem}\n", status=409)
    # With email ingest on, the shortcut offers to email the share when the app is out of reach.
    email = _email_address(state.opts.imap_user, state.opts.imap_plus_tag) if state.opts.imap_enabled else ""
    data = phone.android_import_json(state.base_url, state.opts.ingest_token, email=email)
    return bytes_response(data, "application/json; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{ANDROID_IMPORT_FILE}"', **_no_frame_headers()})


async def static_file(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    path = STATIC_DIR / name
    ext = os.path.splitext(name)[1].lower()
    if ext not in _STATIC_TYPES or not path.is_file():
        return text_response("not found\n", status=404)
    data = await asyncio.to_thread(path.read_bytes)
    headers = {"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"}
    return bytes_response(data, _STATIC_TYPES[ext], headers=headers)


def add_routes(app: web.Application) -> None:
    app[JINJA_KEY] = make_jinja()
    app.router.add_get("/", index)
    app.router.add_get("/admin", index)
    app.router.add_get("/admin/", index)
    app.router.add_get("/admin/login", login_get)
    app.router.add_post("/admin/login", login_post)
    app.router.add_post("/admin/logout", logout)
    app.router.add_get("/admin/items/{item_id:[a-z2-7]{10}}", item_view)
    app.router.add_get("/admin/items/{item_id:[a-z2-7]{10}}/download/{fmt:(epub|txt)}", item_download)
    app.router.add_post("/admin/items/{item_id:[a-z2-7]{10}}/delete", item_delete)
    app.router.add_post("/admin/items/{item_id:[a-z2-7]{10}}/requeue", item_requeue)
    app.router.add_post("/admin/send", send)
    app.router.add_post("/admin/upload", upload)
    app.router.add_get("/admin/setup", setup)
    app.router.add_get("/admin/setup/android.json", android_import)
    app.router.add_post("/admin/rotate-device-token", rotate_device_token)
    app.router.add_get("/admin/plugin-bundle.zip", bundle_zip)
    app.router.add_post("/admin/install-plugin", install_plugin)
    app.router.add_get("/admin/static/{name:[A-Za-z0-9][A-Za-z0-9._-]*}", static_file)
