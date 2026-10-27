"""Fake CrossPoint File Transfer web server (stock firmware 1.6.5 or 2.0).

Emulates, byte-for-byte where it matters, the endpoints Send to X3 uses, as read in
``src/network/CrossPointWebServer.cpp`` / ``WebDAVHandler.cpp`` at tag 1.6.5 and at
2.0 (develop 3f387a9) plus the arduino-esp32 3.3.11 request parser:

- ``GET /api/status`` with the per-version field set.
- ``GET /api/files?path=`` (chunked JSON, dot names hidden unless showHiddenFiles,
  ``[]`` for a missing folder, entries >= 512 bytes of JSON dropped).
- ``GET /download?path=`` (dot names -> 403).
- ``POST /mkdir`` (form ``name``/``path``; creates parents; dot leaf -> 403).
- ``POST /upload?path=`` with the core's own multipart parser quirks (boundary after the
  first ``=``, filename must be the last quoted parameter, ``blob`` + ``?filename=``),
  the never-overwrite rule (case-insensitive), the stale-success bug (the result state is
  only reset when a file part starts), and Library dirty marking.
- ``POST /delete`` (``path`` or JSON ``paths``; per-version dot-name rule).
- ``GET``/``POST /api/settings`` (2.0 hides ``pluginsEnabled``).
- ``POST /api/plugin-fs`` (2.0 only; plugins switch, 256 KiB, atomic replace).
- WebDAV ``PUT`` (overwrite, no Library dirty marking), ``MKCOL``, ``PROPFIND``,
  ``GET``, ``DELETE``; dot segments -> 403.

The SD card is a real folder (``sd_root``). Lookups are case-insensitive like FAT.

Test controls: :meth:`FakeCrossPoint.go_offline` / :meth:`go_online` (connection
refused, same port), :meth:`inject` (canned answers, connection resets before or after
the handler ran, delays), ``vanish_after_uploads`` (the device leaves File Transfer
when an upload arrives after N successful ones), ``disk_full``, ``plugins_enabled``,
``requests`` (a log) and ``max_in_flight`` (the device serves one connection at a time;
the fake serializes requests like the real one and records how many arrived at once).

Usage::

    async with FakeCrossPoint(tmp_path / "sd", version="2.0") as dev:
        client = DeviceClient(dev.host)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

HIDDEN_ITEMS = ("System Volume Information", "XTCache")
SENSITIVE_FILES = ("/.crosspoint/wifi.json", "/.crosspoint/opds.json", "/.crosspoint/koreader.json")
PLUGIN_FS_MAX = 256 * 1024
FAT_FORBIDDEN = set('"*:<>?|')
LIBRARY_DIRTY = "/.crosspoint/library.dirty"
PLUGIN_ROOTS = ("/.crosspoint/plugins", "/plugins", "/.plugins")


# ----------------------------------------------------------------- firmware ports


def normalize_web_path(path: str) -> str:
    """``normalizeWebPath`` + ``FsHelpers::normalisePath``: pop ``..``, drop empties."""
    if not path or path == "/":
        return "/"
    parts: list[str] = []
    for comp in path.split("/"):
        if not comp:
            continue
        if comp == "..":
            if parts:
                parts.pop()
            continue
        parts.append(comp)
    return "/" + "/".join(parts) if parts else "/"


def is_safe_path_component(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")


def is_protected_item_name(name: str) -> bool:
    return name.startswith(".") or name in HIDDEN_ITEMS


def is_sensitive_path(path: str) -> bool:
    """Simplified ``protectedpaths::isSensitivePath`` (prefix, case-insensitive)."""
    canon = normalize_web_path(path).lower()
    return any(canon.startswith(s) for s in SENSITIVE_FILES)


def is_plugin_path(path: str) -> bool:
    return len(path) > 1 and path.startswith("/") and ".." not in path and not is_sensitive_path(path)


def has_ext(name: str, ext: str) -> bool:
    return name.lower().endswith(ext)


def is_book_name(name: str) -> bool:
    return any(has_ext(name, e) for e in (".epub", ".txt", ".md", ".xtc"))


def is_library_book_path(path: str) -> bool:
    """2.0 ``library::isLibraryBookFile``: no hidden segment, book extension."""
    if any(len(seg) > 1 and seg.startswith(".") for seg in path.split("/")):
        return False
    return is_book_name(path) or has_ext(path, ".xtch")


def core_url_decode(text: str) -> str:
    """``WebServer::urlDecode``: %XX and ``+`` -> space, decoded byte by byte."""
    raw = text.encode("utf-8")
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        i += 1
        if c == 0x25 and i + 1 < n:  # '%'
            try:
                out.append(int(raw[i : i + 2], 16))
            except ValueError:
                out.append(0)
            i += 2
        elif c == 0x2B:  # '+'
            out.append(0x20)
        else:
            out.append(c)
    return out.decode("utf-8", "replace")


def core_parse_args(search: str) -> list[tuple[str, str]]:
    """``WebServer::_parseArguments``: ``k=v`` pairs split on ``&``; pairs without ``=`` dropped."""
    args: list[tuple[str, str]] = []
    if not search:
        return args
    for chunk in search.split("&"):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        args.append((core_url_decode(k), core_url_decode(v)))
    return args


@dataclass
class CorePart:
    name: str
    filename: Optional[str]  # None for a plain field
    data: bytes


class MultipartAbort(Exception):
    """``_parseForm`` returned false: the core closes the connection without answering."""


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def until(self, ch: bytes) -> bytes:
        i = self.data.find(ch, self.pos)
        if i < 0:
            out, self.pos = self.data[self.pos :], len(self.data)
            return out
        out, self.pos = self.data[self.pos : i], i + 1
        return out

    def line(self) -> str:
        out = self.until(b"\r")
        self.until(b"\n")
        return out.decode("utf-8", "replace")


def core_parse_multipart(content_type: str, body: bytes, query_args: list[tuple[str, str]]) -> list[CorePart]:
    """Port of arduino-esp32 3.3.11 ``WebServer::_parseForm`` (Parsing.cpp:409-592)."""
    boundary = content_type[content_type.index("=") + 1 :].replace('"', "") if "=" in content_type else ""
    if len(boundary) > 70 or not boundary:
        raise MultipartAbort("bad boundary")
    r = _Reader(body)
    line = ""
    for _ in range(3):
        line = r.until(b"\r").decode("utf-8", "replace")
        if line:
            break
    r.until(b"\n")
    if line != "--" + boundary:
        raise MultipartAbort("form does not start with the boundary")
    parts: list[CorePart] = []
    marker = b"\r\n--" + boundary.encode()
    while r.pos < len(r.data):
        line = r.line()
        if not (len(line) > 19 and line[:19].lower() == "content-disposition"):
            if r.pos >= len(r.data):
                break
            continue
        name_start = line.find("=")
        if name_start == -1:
            continue
        arg_name = line[name_start + 2 :]
        ns = arg_name.find("=")
        filename: Optional[str] = None
        if ns == -1:
            arg_name = arg_name[:-1]
        else:
            filename = arg_name[ns + 2 : -1]
            arg_name = arg_name[: arg_name.find('"')]
            if filename == "blob":
                for k, v in query_args:
                    if k == "filename":
                        filename = v
                        break
        while r.line():  # part headers until the blank line
            pass
        if filename is None:
            value_lines = []
            while True:
                if r.pos >= len(r.data):
                    raise MultipartAbort("truncated field")
                line = r.line()
                if line.startswith("--" + boundary):
                    break
                value_lines.append(line)
            parts.append(CorePart(arg_name, None, "\n".join(value_lines).encode()))
            if line == "--" + boundary + "--":
                break
            continue
        end = r.data.find(marker, r.pos)
        if end < 0:
            raise MultipartAbort("file part without closing boundary (client stalled)")
        parts.append(CorePart(arg_name, filename, r.data[r.pos : end]))
        r.pos = end + len(marker)
        if r.line() == "--":
            break
    return parts


# ------------------------------------------------------------------------ the fake


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body_size: int


@dataclass
class _Fault:
    method: str
    path: str
    kind: str  # "respond" | "reset" | "reset_after" | "delay"
    status: int = 200
    body: str = ""
    delay: float = 0.0
    count: int = 1


@dataclass
class _UploadState:
    """``CrossPointWebServer::UploadState``: persists between requests (stale-success bug)."""

    file_name: str = ""
    path: str = "/"
    size: int = 0
    success: bool = False
    error: str = ""


@dataclass
class _Setting:
    key: str
    name: str
    category: str
    type: str
    value: Any
    min: int = 0
    max: int = 0
    step: int = 1
    options: list[str] = field(default_factory=list)
    web_hidden: bool = False

    def as_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"key": self.key, "name": self.name, "category": self.category, "type": self.type,
                             "value": self.value}
        if self.type == "value":
            d.update(min=self.min, max=self.max, step=self.step)
        if self.type == "enum":
            d["options"] = list(self.options)
        return d


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class FakeCrossPoint:
    """A CrossPoint device in File Transfer mode, backed by ``sd_root``."""

    def __init__(
        self,
        sd_root: os.PathLike | str,
        *,
        version: str = "1.6.5",
        firmware_version: Optional[str] = None,
        plugins_enabled: bool = False,
        show_hidden_files: bool = False,
        device: str = "X3",
        mode: str = "STA",
    ):
        if not (version.startswith("1.6") or version.startswith("2")):
            raise ValueError("version must be '1.6.5' or '2.0'")
        self.family = "2.0" if version.startswith("2") else "1.6.5"
        self.version_string = firmware_version or ("2.0.0-rc+3f387a9" if self.family == "2.0" else "1.6.5")
        self.sd = Path(sd_root)
        self.sd.mkdir(parents=True, exist_ok=True)
        (self.sd / ".crosspoint").mkdir(exist_ok=True)
        self.device = device
        self.mode = mode
        self.plugins_enabled = plugins_enabled
        self.disk_full = False
        self.vanish_after_uploads: Optional[int] = None
        self.uploads_ok = 0
        self.requests: list[RecordedRequest] = []
        self.max_in_flight = 0
        self._in_flight = 0
        self._serial = asyncio.Lock()
        self._faults: list[_Fault] = []
        self._upload = _UploadState()
        self._settings: list[_Setting] = [
            _Setting("sleepTimeoutMinutes", "Time to Sleep", "System", "value", 10, 1, 31, 1),
            _Setting("showHiddenFiles", "Show Hidden Files", "System", "toggle", 1 if show_hidden_files else 0),
            _Setting("libraryUseMetadata", "Use Book Metadata", "System", "toggle", 1),
            _Setting("fontFamily", "Reader Font Family", "Reader", "enum", 0,
                     options=["Bookerly", "Noto Sans", "Open Dyslexic"]),
        ]
        if self.family == "2.0":
            self._settings.append(_Setting("pluginsEnabled", "Plugins", "", "toggle", 0, web_hidden=True))
        self._runner: Optional[web.AppRunner] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self.port = 0
        self.online = False

    # ------------------------------------------------------------ lifecycle

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.host}"

    async def __aenter__(self) -> "FakeCrossPoint":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self, port: int = 0) -> None:
        app = web.Application(middlewares=[self._middleware], client_max_size=64 * 1024 * 1024)
        app.router.add_route("GET", "/api/status", self._status)
        app.router.add_route("GET", "/api/files", self._files)
        app.router.add_route("GET", "/download", self._download)
        app.router.add_route("POST", "/mkdir", self._mkdir)
        app.router.add_route("POST", "/upload", self._upload_route)
        app.router.add_route("POST", "/delete", self._delete)
        app.router.add_route("GET", "/api/settings", self._get_settings)
        app.router.add_route("POST", "/api/settings", self._post_settings)
        if self.family == "2.0":
            app.router.add_route("POST", "/api/plugin-fs", self._plugin_fs)
            app.router.add_route("GET", "/api/plugins", self._plugins)
        app.router.add_route("*", "/{tail:.*}", self._fallback)
        self._runner = web.AppRunner(app, handle_signals=False, access_log=None)
        await self._runner.setup()
        await self._listen(port)

    async def _listen(self, port: int) -> None:
        assert self._runner is not None and self._runner.server is not None
        loop = asyncio.get_running_loop()
        self._server = await loop.create_server(self._runner.server, "127.0.0.1", port, reuse_address=True)
        self.port = self._server.sockets[0].getsockname()[1]
        self.online = True

    def _abort_connections(self) -> None:
        if self._runner is not None and self._runner.server is not None:
            for handler in list(self._runner.server.connections):
                transport = handler.transport
                if transport is not None:
                    transport.abort()

    def go_offline(self) -> None:
        """The user left File Transfer: new connections are refused, open ones reset."""
        self.online = False
        if self._server is not None:
            self._server.close()
            self._server = None
        self._abort_connections()

    async def go_online(self) -> None:
        """The user opened File Transfer again (same address)."""
        if self._server is None:
            await self._listen(self.port)
        self.online = True

    async def stop(self) -> None:
        self.go_offline()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------- controls

    def inject(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        body: str = "",
        reset: bool = False,
        reset_after: bool = False,
        delay: float = 0.0,
        count: int = 1,
    ) -> None:
        """Make the next ``count`` matching requests misbehave.

        Default: answer ``status``/``body`` without running the handler. ``reset``: drop
        the connection before the handler runs. ``reset_after``: run the handler (side
        effects happen) and drop the connection instead of answering. ``delay``: wait
        before handling (to trigger client timeouts).
        """
        kind = "reset" if reset else "reset_after" if reset_after else "delay" if delay else "respond"
        self._faults.append(_Fault(method.upper(), path, kind, status, body, delay, count))

    @property
    def show_hidden_files(self) -> bool:
        return bool(self._setting("showHiddenFiles").value)

    @show_hidden_files.setter
    def show_hidden_files(self, on: bool) -> None:
        self._setting("showHiddenFiles").value = 1 if on else 0

    def _setting(self, key: str) -> _Setting:
        return next(s for s in self._settings if s.key == key)

    def requests_to(self, method: str, path: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.method == method.upper() and r.path == path]

    # ------------------------------------------------------------- SD card

    def resolve(self, sd_path: str) -> Path:
        """Host path for ``sd_path``, matching existing names case-insensitively (FAT)."""
        cur = self.sd
        for comp in normalize_web_path(sd_path).split("/"):
            if not comp or comp == ".":
                continue
            match = None
            if cur.is_dir():
                low = comp.casefold()
                for child in cur.iterdir():
                    if child.name.casefold() == low:
                        match = child
                        break
            cur = match if match is not None else cur / comp
        return cur

    def exists(self, sd_path: str) -> bool:
        return self.resolve(sd_path).exists()

    def read(self, sd_path: str) -> bytes:
        return self.resolve(sd_path).read_bytes()

    def write(self, sd_path: str, data: bytes) -> None:
        p = self.resolve(sd_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def listdir(self, sd_path: str) -> list[str]:
        p = self.resolve(sd_path)
        return sorted(c.name for c in p.iterdir()) if p.is_dir() else []

    @property
    def library_dirty(self) -> bool:
        return self.exists(LIBRARY_DIRTY)

    def clear_library_dirty(self) -> None:
        p = self.resolve(LIBRARY_DIRTY)
        if p.exists():
            p.unlink()

    def _mark_library_dirty(self) -> None:
        self.write(LIBRARY_DIRTY, b"")

    # ---------------------------------------------------------- middleware

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        body = await request.read()
        self.requests.append(
            RecordedRequest(request.method, request.path, dict(request.query), dict(request.headers), len(body))
        )
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            async with self._serial:  # the device serves one connection at a time
                if not self.online:
                    return self._drop(request)
                if (request.path == "/upload" and self.vanish_after_uploads is not None
                        and self.uploads_ok >= self.vanish_after_uploads):
                    self.vanish_after_uploads = None
                    self.go_offline()
                    return self._drop(request)
                fault = self._take_fault(request)
                if fault is not None:
                    if fault.kind == "reset":
                        return self._drop(request)
                    if fault.kind == "respond":
                        return self._finish(web.Response(status=fault.status, text=fault.body))
                    if fault.kind == "delay":
                        await asyncio.sleep(fault.delay)
                resp = await handler(request)
                if fault is not None and fault.kind == "reset_after":
                    return self._drop(request)
                return self._finish(resp)
        finally:
            self._in_flight -= 1

    def _take_fault(self, request: web.Request) -> Optional[_Fault]:
        for f in self._faults:
            if f.method in (request.method, "*") and f.path == request.path:
                f.count -= 1
                if f.count <= 0:
                    self._faults.remove(f)
                return f
        return None

    @staticmethod
    def _drop(request: web.Request) -> web.Response:
        if request.transport is not None:
            request.transport.abort()
        return web.Response(status=500)

    @staticmethod
    def _finish(resp: web.StreamResponse) -> web.StreamResponse:
        if resp.prepared:
            return resp
        resp.headers["Connection"] = "close"
        resp.headers.setdefault("Access-Control-Allow-Origin", "*")
        resp.force_close()
        return resp

    @staticmethod
    def _text(status: int, text: str) -> web.Response:
        return web.Response(status=status, body=text.encode("utf-8"), content_type="text/plain")

    @staticmethod
    def _json(status: int, obj: Any) -> web.Response:
        return web.Response(status=status, body=_compact(obj).encode("utf-8"), content_type="application/json")

    def _args(self, request: web.Request, body: bytes) -> list[tuple[str, str]]:
        """Query args plus form-urlencoded body args, like ``_parseRequest``."""
        search = request.rel_url.raw_query_string
        if request.content_type == "application/x-www-form-urlencoded" and body:
            search = (search + "&" if search else "") + body.decode("utf-8", "replace")
        return core_parse_args(search)

    @staticmethod
    def _arg(args: list[tuple[str, str]], key: str) -> Optional[str]:
        for k, v in args:
            if k == key:
                return v
        return None

    # --------------------------------------------------------------- routes

    async def _status(self, request: web.Request) -> web.Response:
        doc: dict[str, Any] = {"version": self.version_string}
        if self.family == "2.0":
            doc["firmwareFamily"] = "crosspoint"
            doc["pluginApi"] = 1
            doc["pluginCapabilities"] = (
                "catalog.lists,catalog.notices,catalog.bundle_install,catalog.version_tracking,"
                "catalog.compatibility,catalog.item_details"
            )
        doc.update(ip="127.0.0.1", mode=self.mode, rssi=0 if self.mode == "AP" else -52, freeHeap=123456, uptime=42)
        plugin = (self._arg(core_parse_args(request.rel_url.raw_query_string), "plugin") or "").lower()
        if self.family != "2.0":
            plugin = ""
        if plugin and len(plugin) <= 64:
            doc["deviceId"] = hashlib.sha256(b"fake-device-secret" + plugin.encode()).hexdigest()
        doc["device"] = self.device
        doc["serial"] = "Not found"
        return self._json(200, doc)

    async def _files(self, request: web.Request) -> web.StreamResponse:
        args = self._args(request, b"")
        path = normalize_web_path(self._arg(args, "path") or "/") if self._arg(args, "path") is not None else "/"
        resp = web.StreamResponse(status=200)
        resp.content_type = "application/json"
        resp.enable_chunked_encoding()
        self._finish(resp)
        await resp.prepare(request)
        await resp.write(b"[")
        first = True
        d = self.resolve(path)
        if d.is_dir():
            for child in sorted(d.iterdir(), key=lambda c: c.name):
                name = child.name
                if (name.startswith(".") and not self.show_hidden_files) or name in HIDDEN_ITEMS:
                    continue
                is_dir = child.is_dir()
                entry = _compact({"name": name, "size": 0 if is_dir else child.stat().st_size,
                                  "isDirectory": is_dir, "isEpub": (not is_dir) and has_ext(name, ".epub")})
                if len(entry.encode("utf-8")) >= 512:
                    continue  # "Skipping file entry with oversized JSON"
                await resp.write((b"" if first else b",") + entry.encode("utf-8"))
                first = False
        await resp.write(b"]")
        await resp.write_eof()
        return resp

    async def _download(self, request: web.Request) -> web.Response:
        args = self._args(request, b"")
        raw = self._arg(args, "path")
        if raw is None:
            return self._text(400, "Missing path")
        item = normalize_web_path(raw)
        if item == "/":
            return self._text(400, "Invalid path")
        name = item.rsplit("/", 1)[-1]
        if name.startswith(".") or (self.family == "2.0" and is_sensitive_path(item)):
            return self._text(403, "Cannot access system files")
        if name in HIDDEN_ITEMS:
            return self._text(403, "Cannot access protected items")
        target = self.resolve(item)
        if not target.exists():
            return self._text(404, "Item not found")
        if target.is_dir():
            return self._text(400, "Path is a directory")
        ctype = "application/epub+zip" if has_ext(name, ".epub") else "application/octet-stream"
        resp = web.Response(status=200, body=target.read_bytes(), content_type=ctype)
        resp.headers["Content-Disposition"] = f'attachment; filename="{target.name}"'
        return resp

    async def _mkdir(self, request: web.Request) -> web.Response:
        body = await request.read()
        args = self._args(request, body)
        name = self._arg(args, "name")
        if name is None:
            return self._text(400, "Missing folder name")
        if not name:
            return self._text(400, "Folder name cannot be empty")
        if not is_safe_path_component(name):
            return self._text(400, "Invalid folder name")
        if is_protected_item_name(name):
            return self._text(403, "Cannot create protected item")
        parent = self._arg(args, "path")
        parent = normalize_web_path(parent) if parent is not None else "/"
        folder = parent.rstrip("/") + "/" + name
        target = self.resolve(folder)
        if target.exists():
            return self._text(400, "Folder already exists")
        try:
            target.mkdir(parents=True)  # Storage.mkdir(path, pFlag = true)
        except OSError:
            return self._text(500, "Failed to create folder")
        return self._text(200, "Folder created: " + name)

    async def _upload_route(self, request: web.Request) -> web.Response:
        body = await request.read()
        st = self._upload
        query_args = core_parse_args(request.rel_url.raw_query_string)
        ctype = request.headers.get("Content-Type", "")
        if ctype.startswith("multipart/"):
            try:
                parts = core_parse_multipart(ctype, body, query_args)
            except MultipartAbort:
                return self._drop(request)
            for part in parts:
                if part.filename is None:
                    continue
                self._upload_part(st, part, query_args)
        # handleUploadPost: whatever the (possibly stale) state says
        if st.success:
            return self._text(200, "File uploaded successfully: " + st.file_name)
        return self._text(400, st.error or "Unknown error during upload")

    def _upload_part(self, st: _UploadState, part: CorePart, query_args: list[tuple[str, str]]) -> None:
        # UPLOAD_FILE_START
        st.file_name = part.filename or ""
        st.size = 0
        st.success = False
        st.error = ""
        if not is_safe_path_component(st.file_name):
            st.error = "Invalid file name"
            return
        path_arg = self._arg(query_args, "path")
        st.path = normalize_web_path(path_arg) if path_arg is not None else "/"
        file_path = st.path.rstrip("/") + "/" + st.file_name
        if self.family == "2.0" and is_sensitive_path(file_path):
            st.error = "Cannot write protected items"
            return
        target = self.resolve(file_path)
        if target.exists():
            st.error = "File already exists: " + st.file_name
            return
        bad_char = any(c in FAT_FORBIDDEN or ord(c) < 0x20 for c in st.file_name)
        if not target.parent.is_dir() or bad_char or len(st.file_name.encode("utf-8")) > 255:
            st.error = "Failed to create file on SD card"
            return
        # UPLOAD_FILE_WRITE
        if self.disk_full:
            st.error = "Failed to write to SD card - disk may be full"
            if self.family == "1.6.5":
                target.write_bytes(part.data[: len(part.data) // 2])  # closed, never removed (A:750-754)
            return
        target.write_bytes(part.data)
        st.size = len(part.data)
        # UPLOAD_FILE_END
        st.success = True
        self.uploads_ok += 1
        if self.family == "2.0":
            if is_library_book_path(file_path):
                self._mark_library_dirty()
        elif is_book_name(st.file_name):
            self._mark_library_dirty()

    async def _delete(self, request: web.Request) -> web.Response:
        body = await request.read()
        args = self._args(request, body)
        path_arg, paths_arg = self._arg(args, "path"), self._arg(args, "paths")
        if path_arg is None and paths_arg is None:
            return self._text(400, "Missing `path` or `paths` argument")
        if path_arg is not None and paths_arg is not None:
            return self._text(400, "Provide either 'path' or 'paths', not both")
        if paths_arg is not None:
            try:
                paths = json.loads(paths_arg)
            except ValueError:
                return self._text(400, "Invalid paths format")
        else:
            paths = [path_arg]
        if not isinstance(paths, list) or not paths:
            return self._text(400, "No paths provided")
        failed = ""
        for p in paths:
            item = normalize_web_path(str(p))
            if item == "/":
                failed += item + " (cannot delete root); "
                continue
            name = item.rsplit("/", 1)[-1]
            if self.family == "1.6.5" and name.startswith("."):
                failed += item + " (hidden/system file); "
                continue
            if self.family == "2.0" and is_sensitive_path(item):
                failed += item + " (protected file); "
                continue
            if name in HIDDEN_ITEMS:
                failed += item + " (protected file); "
                continue
            target = self.resolve(item)
            if not target.exists():
                failed += item + " (not found); "
                continue
            if target.is_dir():
                if any(target.iterdir()):
                    failed += item + " (folder not empty); "
                    continue
                target.rmdir()
            else:
                target.unlink()
        if failed:
            return self._text(500, "Failed to delete some items: " + failed)
        return self._text(200, "All items deleted successfully")

    async def _get_settings(self, request: web.Request) -> web.Response:
        items = [s.as_json() for s in self._settings if not s.web_hidden]
        return web.Response(status=200, body=("[" + ",".join(_compact(i) for i in items) + "]").encode(),
                            content_type="application/json")

    async def _post_settings(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not body or request.content_type in ("application/x-www-form-urlencoded",) or \
                request.content_type.startswith("multipart/"):
            return self._text(400, "Missing JSON body")
        try:
            doc = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._text(400, f"Invalid JSON: {type(exc).__name__}")
        if not isinstance(doc, dict):
            doc = {}
        applied = 0
        for s in self._settings:
            if s.web_hidden or s.key not in doc:
                continue
            raw = doc[s.key]
            if s.type == "toggle":
                s.value = 1 if _as_int(raw) else 0
                applied += 1
            elif s.type == "enum":
                v = _as_int(raw)
                if 0 <= v < len(s.options):
                    s.value = v
                    applied += 1
            elif s.type == "value":
                v = _as_int(raw)
                if s.min <= v <= s.max:
                    s.value = v
                    applied += 1
            elif s.type == "string":
                s.value = "" if raw is None else str(raw)
                applied += 1
        return self._text(200, f"Applied {applied} setting(s)")

    async def _plugin_fs(self, request: web.Request) -> web.Response:
        body = await request.read()
        query_args = core_parse_args(request.rel_url.raw_query_string)
        ctype = request.headers.get("Content-Type", "")
        started = False
        error: Optional[tuple[int, str]] = None
        written = 0
        if ctype.startswith("multipart/"):
            try:
                parts = core_parse_multipart(ctype, body, query_args)
            except MultipartAbort:
                return self._drop(request)
            for part in parts:
                if part.filename is None:
                    continue
                started, error, written = True, None, 0
                path = self._arg(query_args, "path") or ""
                plugin = self._arg(query_args, "plugin") or ""
                if not self.plugins_enabled:
                    error = (403, "plugin system disabled")
                    continue
                if not plugin or "/" in plugin or "\\" in plugin or ".." in plugin or not is_plugin_path(path):
                    error = (400, "bad path")
                    continue
                if len(part.data) > PLUGIN_FS_MAX:
                    error = (413, "too large")
                    continue
                if not part.data:
                    error = (400, "empty body")
                    continue
                target = self.resolve(path)
                target.parent.mkdir(parents=True, exist_ok=True)  # ensureDirectoryExists
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_bytes(part.data)
                os.replace(tmp, target)  # Storage.replaceFile
                written = len(part.data)
                if is_library_book_path(normalize_web_path(path)):
                    self._mark_library_dirty()
        if not self.plugins_enabled and not started:
            return self._text(403, "plugin system disabled")
        if not started:
            return self._json(400, {"error": "missing file part"})
        if error is not None:
            return self._json(error[0], {"error": error[1]})
        return self._json(200, {"ok": True, "bytes": written})

    async def _plugins(self, request: web.Request) -> web.Response:
        out = []
        if self.plugins_enabled:
            seen = set()
            for root in PLUGIN_ROOTS:
                d = self.resolve(root)
                if not d.is_dir():
                    continue
                for child in sorted(d.iterdir(), key=lambda c: c.name):
                    if not child.is_dir() or child.name.startswith(".") or child.name in seen:
                        continue
                    seen.add(child.name)
                    if (child / "plugin.js").exists():
                        out.append({"name": child.name, "dir": f"{root}/{child.name}", "title": child.name,
                                    "mount": "settings"})
        return self._json(200, out)

    # ------------------------------------------------- WebDAV / not found

    @staticmethod
    def _dav_protected(path: str) -> bool:
        return any(seg.startswith(".") or seg in HIDDEN_ITEMS for seg in path.split("/") if seg)

    async def _fallback(self, request: web.Request) -> web.StreamResponse:
        method = request.method
        path = normalize_web_path(core_url_decode(request.rel_url.raw_path))
        if method == "PUT":
            return await self._dav_put(request, path)
        if method == "MKCOL":
            return await self._dav_mkcol(request, path)
        if method == "PROPFIND":
            return self._dav_propfind(request, path)
        if method == "GET":
            target = self.resolve(path)
            if self._dav_protected(path):
                return self._text(403, "Forbidden")
            if not target.exists() or target.is_dir():
                return self._text(404, "Not Found")
            return web.Response(status=200, body=target.read_bytes(), content_type="application/octet-stream")
        if method == "DELETE":
            if self._dav_protected(path):
                return self._text(403, "Forbidden")
            target = self.resolve(path)
            if not target.exists():
                return self._text(404, "Not Found")
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return web.Response(status=204)
        if method == "OPTIONS":
            resp = web.Response(status=200)
            resp.headers["DAV"] = "1"
            return resp
        return self._text(404, f"404 Not Found\n\nURI: {request.rel_url.raw_path}\n")

    async def _dav_put(self, request: web.Request, path: str) -> web.Response:
        data = await request.read()
        if self._dav_protected(path):
            return self._text(403, "Forbidden")
        target = self.resolve(path)
        if not target.parent.is_dir() or target.is_dir():
            return self._text(500, "Write failed - incomplete upload or disk full")
        existed = target.exists()
        tmp = target.with_name(target.name + ".davtmp")
        tmp.write_bytes(data)
        if existed:
            target.unlink()
        tmp.rename(target)
        # WebDAV never calls markLibraryIndexDirty (both versions)
        return web.Response(status=204 if existed else 201)

    async def _dav_mkcol(self, request: web.Request, path: str) -> web.Response:
        body = await request.read()
        if self._dav_protected(path):
            return self._text(403, "Forbidden")
        if body:
            return self._text(415, "Unsupported Media Type")
        target = self.resolve(path)
        if target.exists():
            return self._text(405, "Already exists")
        if not target.parent.is_dir():
            return self._text(409, "Parent directory does not exist")
        target.mkdir()
        return web.Response(status=201)

    def _dav_propfind(self, request: web.Request, path: str) -> web.Response:
        target = self.resolve(path)
        if path != "/" and not target.exists():
            return self._text(404, "Not Found")
        entries = [(path, target.is_dir(), 0 if target.is_dir() else target.stat().st_size)]
        if target.is_dir() and request.headers.get("Depth", "1") != "0":
            for child in sorted(target.iterdir(), key=lambda c: c.name):
                if child.name.startswith(".") or child.name in HIDDEN_ITEMS:
                    continue
                entries.append((path.rstrip("/") + "/" + child.name, child.is_dir(),
                                0 if child.is_dir() else child.stat().st_size))
        xml = ['<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">\n']
        for href, is_dir, size in entries:
            kind = "<D:collection/>" if is_dir else ""
            xml.append(f"<D:response><D:href>{href}</D:href><D:propstat><D:prop><D:resourcetype>{kind}"
                       f"</D:resourcetype><D:getcontentlength>{size}</D:getcontentlength></D:prop>"
                       "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>\n")
        xml.append("</D:multistatus>\n")
        return web.Response(status=207, body="".join(xml).encode(), content_type="application/xml")


def _as_int(v: Any) -> int:
    """ArduinoJson ``as<int>()``: numbers truncate, true -> 1, everything else -> 0."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    return 0
