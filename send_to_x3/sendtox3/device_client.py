"""Client for the CrossPoint File Transfer web server on the Xteink X3 (stock firmware).

The web server only runs while the user has *File Transfer* (Join a Network / Create
Hotspot) or *Calibre Wireless* open on the device. It is a single-threaded Arduino
``WebServer``: it serves one TCP connection at a time, parses the whole request body
inside one ``handleClient()`` call and answers every response with
``Connection: close``. This client therefore sends one request at a time per device
(a lock shared by every client for the same host), opens a fresh connection for each
request, uses short timeouts and retries idempotent requests on connection errors with
a small backoff.

Firmware facts this module relies on. ``A`` = 1.6.5 ``src/network/CrossPointWebServer.cpp``,
``B`` = 2.0 (develop 3f387a9) ``src/network/CrossPointWebServer.cpp``, ``W`` =
``src/network/WebDAVHandler.cpp`` (same logic in both), ``P`` = arduino-esp32 3.3.11
``libraries/WebServer/src/Parsing.cpp``:

``GET /api/status`` (A:412-453, B:527-587)
    ``{"version","ip","mode":"STA"|"AP","rssi","freeHeap","uptime","device":"X3","serial"}``.
    2.x adds ``"firmwareFamily":"crosspoint"``, ``"pluginApi":1``, ``"pluginCapabilities"``
    and, with ``?plugin=<name>``, ``"deviceId"``. Version strings look like ``1.6.5``,
    ``2.0.0``, ``2.0.0-rc+<sha>``, ``2.0.0-slim`` or ``1.1.0-dev-<branch>-<sha>``.
``GET /api/files?path=<dir>`` (A:519-559, B:653-693)
    Chunked JSON array of ``{"name","size","isDirectory","isEpub"}``. Names starting with
    ``.`` are hidden unless the *showHiddenFiles* setting is on; ``System Volume
    Information`` and ``XTCache`` are always hidden; an entry whose JSON is 512 bytes or
    more is silently dropped. A missing folder, or a file path, gives ``[]``, exactly
    like an empty folder.
``POST /mkdir`` form ``name=<leaf>&path=<parent>`` (A:819-871, B:987-1039)
    ``200 Folder created: <name>``; ``400 Folder already exists`` (also when a *file* has
    that name); ``400 Missing folder name`` / ``Folder name cannot be empty`` / ``Invalid
    folder name``; ``403 Cannot create protected item`` for a leaf starting with ``.`` (so
    ``.crosspoint`` cannot be created) or named ``System Volume Information``/``XTCache``;
    ``500 Failed to create folder``. ``Storage.mkdir`` creates missing parents
    (``HalStorage.h`` ``mkdir(path, pFlag = true)``) and only the leaf is checked, so
    ``name=sendtox3&path=/.crosspoint/plugins`` works.
``POST /upload?path=<dir>`` multipart, one file part (A:664-817, B:815-985)
    The folder comes from the query string only and must already exist. ``200 File
    uploaded successfully: <name>``; ``400 File already exists: <name>`` in **both**
    versions (case-insensitive, FAT; uploads never overwrite); ``400 Invalid file name``,
    ``Failed to create file on SD card`` (missing folder or FAT-illegal characters),
    ``Failed to write to SD card - disk may be full`` (1.6.5 leaves the partial file,
    A:750-754; 2.x removes it), ``Upload aborted``, ``Unknown error during upload``; 2.x
    also ``Cannot write protected items`` for the credential stores. *Stale success*: the
    result is reset only when a file part starts (A:684), so a request without a file
    part repeats the previous answer; always send exactly one file part and confirm with
    ``/api/files``. Parser quirks (P:439-455): ``Content-Disposition: form-data;
    name="file"; filename="<name>"`` with ``filename`` last and quoted, no RFC 2231
    ``filename*=``, no escaping; the boundary is everything after the first ``=`` of the
    Content-Type (no further parameters) and at most 70 characters (P:174-181). Book
    uploads (``.epub .txt .md .xtc``) mark the Library index dirty (A:792, B:962).
``GET /download?path=<file>`` (A:561-638, B:724-780)
    The raw file with an explicit Content-Length; ``403`` for a name starting with
    ``.`` (and the credential stores on 2.x), ``404 Item not found``.
``POST /delete`` form ``path=<p>`` or ``paths=<json array>`` (A:1048-1163, B:1226-1340)
    ``200 All items deleted successfully``; ``500 Failed to delete some items: ...``.
    1.6.5 refuses dot-named items; 2.x allows them but refuses the credential stores.
``GET``/``POST /api/settings`` (A:1170-1339, B:1347-1531)
    GET: JSON array of ``{"key","name","category","type":"toggle"|"enum"|"value"|
    "string","value",...}``. POST: JSON object ``{key: value}`` (toggles, enums and values
    are integers), answered ``200 Applied <n> setting(s)``. 2.x marks ``pluginsEnabled``
    ``webHidden`` (``SettingsList.cpp:194``): it is neither listed nor accepted, so the
    Plugins switch can only be changed on the device (Settings > Connectivity > Plugins).
``POST /api/plugin-fs?plugin=<name>&path=<abs path>`` multipart (2.x only, B:2403-2498)
    Needs the Plugins switch on (``403 plugin system disabled``). ``path`` absolute,
    without ``..`` and not a credential store (``400 bad path``); at most 256 KiB
    (``413``); non-empty (``400 empty body``); parents are created; the body goes to
    ``<path>.tmp`` and then atomically replaces ``path`` (it overwrites). Answers
    ``200 {"ok":true,"bytes":N}``. Without a file part it answers ``400 missing file part``
    when plugins are on and ``403`` when off, with no side effect, which
    :meth:`DeviceClient.plugins_enabled` uses as a probe. 1.6.5 has no such route (404).
WebDAV ``PUT /<path>`` (W:47-116, W:389-409)
    Overwrites (temp file, then rename; ``201`` new, ``204`` replaced); the parent must
    exist (``500``); any path segment starting with ``.`` gets ``403``. It does **not**
    mark the Library index dirty in either version, so a book written this way appears
    only after a manual index rebuild.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import weakref
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional
from urllib.parse import quote

import aiohttp
from yarl import URL

from . import __version__

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0  # seconds, for small API requests
STATUS_TIMEOUT = 2.5  # status polls must fail fast while the device is away
CONNECT_TIMEOUT = 3.0
PLUGIN_FS_MAX_BYTES = 256 * 1024  # B:2408
MAX_NAME_BYTES = 255  # FAT long names; the Library skips longer names
MAX_BOUNDARY_LEN = 70  # P:176

USER_AGENT = f"sendtox3/{__version__}"

# Characters FAT refuses in a long file name (the device answers "Failed to create file").
_FAT_FORBIDDEN = frozenset('"*:<>?|')
# Always hidden by the device and refused by /mkdir (A:35, A:81-91).
_PROTECTED_NAMES = ("System Volume Information", "XTCache")
# Credential stores no network request may touch on 2.x (ProtectedPaths.h:15-16).
_SENSITIVE_PATHS = ("/.crosspoint/wifi.json", "/.crosspoint/opds.json", "/.crosspoint/koreader.json")
# Readable substitutes for characters a FAT name cannot hold (ARCHITECTURE §8).
_NAME_SUBSTITUTES = {"/": "-", "\\": "-", ":": " -", "?": "", '"': "'", "<": "", ">": "", "|": "-", "*": ""}

_VERSION_RE = re.compile(r"^\s*[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_APPLIED_RE = re.compile(r"Applied\s+(\d+)")

_FORM = "application/x-www-form-urlencoded"
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (aiohttp.ClientError, TimeoutError, OSError)


# ---------------------------------------------------------------------------- errors


class DeviceError(Exception):
    """Base class. ``status`` is the HTTP status when the device answered."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class DeviceUnreachable(DeviceError):
    """No HTTP answer: connection refused/reset, timeout, or the device left File Transfer."""


class DeviceRejected(DeviceError):
    """The device answered, but refused the request or the result did not verify."""


# ---------------------------------------------------------------------- data types


@dataclass(frozen=True)
class DeviceFile:
    """One entry of ``GET /api/files``."""

    name: str
    size: int
    is_dir: bool
    is_epub: bool


@dataclass(frozen=True)
class _Reply:
    status: int
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace").strip()

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


# ------------------------------------------------------------------ pure helpers


def parse_version(version: Any) -> Optional[tuple[int, int, int]]:
    """``"2.0.0-rc+3f387a9"`` -> ``(2, 0, 0)``; ``"1.6.5"`` -> ``(1, 6, 5)``; junk -> None."""
    if not isinstance(version, str):
        return None
    m = _VERSION_RE.match(version)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def is_plugin_firmware(status: Optional[Mapping[str, Any]]) -> bool:
    """True when ``/api/status`` comes from firmware with the SD plugin system (2.x)."""
    if not status:
        return False
    if "pluginApi" in status:
        return True
    v = parse_version(status.get("version"))
    return v is not None and v >= (2, 0, 0)


def normalize_host(host: str) -> str:
    """``"http://192.168.1.5/"``, ``"192.168.1.5:80"``, ``"fe80::1"`` -> an authority."""
    h = (host or "").strip()
    if not h:
        raise ValueError("empty device host")
    if "://" in h:
        u = URL(h)
        if u.scheme != "http":
            raise ValueError(f"the device web server speaks plain http, not {u.scheme!r}")
        h = u.raw_authority
    h = h.split("/", 1)[0]
    if h.count(":") > 1 and not h.startswith("["):
        h = f"[{h}]"  # bare IPv6 literal
    if not h:
        raise ValueError(f"invalid device host {host!r}")
    return h


def normalize_sd_path(path: str) -> str:
    """Mirror of the firmware's ``normalizeWebPath``: absolute, ``..`` popped, no trailing ``/``."""
    parts: list[str] = []
    for comp in (path or "").split("/"):
        if not comp or comp == ".":
            continue
        if comp == "..":
            if parts:
                parts.pop()
            continue
        parts.append(comp)
    return "/" + "/".join(parts)


def sd_join(folder: str, name: str) -> str:
    folder = normalize_sd_path(folder)
    return (folder if folder.endswith("/") else folder + "/") + name


def split_sd_path(path: str) -> tuple[str, str]:
    """``"/a/b/c.json"`` -> ``("/a/b", "c.json")``; ``"/x"`` -> ``("/", "x")``."""
    path = normalize_sd_path(path)
    parent, _, name = path.rpartition("/")
    return parent or "/", name


def has_hidden_segment(path: str) -> bool:
    return any(seg.startswith(".") for seg in normalize_sd_path(path).split("/") if seg)


def is_sensitive_path(path: str) -> bool:
    p = normalize_sd_path(path).casefold()
    return any(p.startswith(s) for s in _SENSITIVE_PATHS)


def split_ext(filename: str) -> tuple[str, str]:
    """``"Title.epub"`` -> ``("Title", ".epub")``. A "suffix" with spaces is not an extension."""
    dot = filename.rfind(".")
    if dot <= 0:
        return filename, ""
    ext = filename[dot:]
    if len(ext) > 10 or " " in ext:
        return filename, ""
    return filename[:dot], ext


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Cut ``text`` to at most ``max_bytes`` UTF-8 bytes on a character boundary."""
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", "ignore")


def check_filename(name: str, *, allow_hidden: bool = False) -> None:
    """Raise ValueError unless ``name`` is stored by the device exactly as given.

    The device itself only refuses ``/``, ``\\``, ``.`` and ``..``; FAT refuses
    ``" * : < > ? |`` and control characters; SdFat skips leading spaces and drops
    trailing dots and spaces (so the stored name would differ and verification would
    fail); a leading ``.`` hides the file from the Library and the listing.
    """
    if not isinstance(name, str) or not name or name in (".", ".."):
        raise ValueError(f"invalid file name {name!r}")
    if "/" in name or "\\" in name:
        raise ValueError(f"file name {name!r} contains a path separator")
    bad = sorted({c for c in name if c in _FAT_FORBIDDEN or ord(c) < 0x20 or ord(c) == 0x7F})
    if bad:
        raise ValueError(f"file name {name!r} contains characters FAT cannot store: {bad!r}")
    if name != name.strip(" ") or name.endswith("."):
        raise ValueError(f"file name {name!r} has leading/trailing spaces or a trailing dot")
    if name.startswith(".") and not allow_hidden:
        raise ValueError(f"file name {name!r} would be hidden on the device")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError(f"file name {name!r} is longer than {MAX_NAME_BYTES} bytes")


def _check_folder_name(name: str) -> None:
    check_filename(name)
    if name in _PROTECTED_NAMES:
        raise ValueError(f"the device refuses to create a folder named {name!r}")


def sanitize_filename(stem: str, ext: str = "", *, fallback: str = "Untitled", max_bytes: int = MAX_NAME_BYTES) -> str:
    """Best-effort FAT/firmware-safe ``<stem>.<ext>`` (titles are normally pre-sanitized)."""
    ext = (ext or "").lstrip(".")
    suffix = f".{ext}" if ext else ""
    chars: list[str] = []
    for c in stem or "":
        if ord(c) < 0x20 or ord(c) == 0x7F:
            chars.append(" ")
        else:
            chars.append(_NAME_SUBSTITUTES.get(c, c))
    s = re.sub(r"\s+", " ", "".join(chars)).strip()
    s = s.lstrip(". ").rstrip(". ")
    s = truncate_utf8(s, max_bytes - len(suffix.encode("utf-8"))).rstrip(". ")
    if not s:
        s = truncate_utf8(fallback, max_bytes - len(suffix.encode("utf-8"))).strip(". ") or "Untitled"
    return s + suffix


def numbered_name(filename: str, n: int, *, max_bytes: int = MAX_NAME_BYTES) -> str:
    """``n``-th candidate name: ``Title.epub``, ``Title (2).epub``, ``Title (3).epub``, ..."""
    if n <= 1:
        return filename
    stem, ext = split_ext(filename)
    suffix = f" ({n}){ext}"
    stem = truncate_utf8(stem, max_bytes - len(suffix.encode("utf-8"))).rstrip(" .") or "file"
    return stem + suffix


def build_multipart(filename: str, data: bytes, *, field: str = "file") -> tuple[bytes, str]:
    """One multipart file part in the exact shape the firmware's parser accepts."""
    if any(c in filename for c in '"\r\n'):
        raise ValueError(f"file name {filename!r} cannot be sent in a multipart header")
    while True:
        boundary = "sendtox3-" + secrets.token_hex(16)
        if b"--" + boundary.encode() not in data:
            break
    assert len(boundary) <= MAX_BOUNDARY_LEN
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n"
        "\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + data + tail, f"multipart/form-data; boundary={boundary}"


def upload_timeout(size: int) -> float:
    """Whole-request budget for a body of ``size`` bytes (SD writes run at ~100+ KB/s)."""
    return max(20.0, 10.0 + size / 25_000)


def _quote(value: str) -> str:
    # The firmware's urlDecode turns "+" into a space, so "+" must be sent as %2B.
    return quote(value, safe="/")


def _form(fields: Mapping[str, str]) -> bytes:
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in fields.items()).encode("utf-8")


def _describe(exc: Optional[BaseException]) -> str:
    if exc is None:
        return "no answer"
    if isinstance(exc, TimeoutError):
        return "timed out"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# One request at a time per device, shared by every DeviceClient in the same event loop.
_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = weakref.WeakKeyDictionary()
_LOCKS_FALLBACK: dict[int, dict[str, asyncio.Lock]] = {}


def _device_lock(host: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    try:
        per_loop = _LOCKS.setdefault(loop, {})
    except TypeError:  # an event loop implementation without weakref support
        per_loop = _LOCKS_FALLBACK.setdefault(id(loop), {})
    lock = per_loop.get(host.lower())
    if lock is None:
        lock = per_loop[host.lower()] = asyncio.Lock()
    return lock


# --------------------------------------------------------------------------- client


class DeviceClient:
    """Talks to one CrossPoint device's File Transfer server.

    Use as ``async with DeviceClient("192.168.1.42") as dev: ...`` or call
    :meth:`close` when done. All methods raise :class:`DeviceUnreachable` when the device
    does not answer (except :meth:`status`, which returns None) and
    :class:`DeviceRejected` when it answers with an error.
    """

    def __init__(
        self,
        host: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 2,
        backoff: float = 0.4,
    ):
        self.host = normalize_host(host)
        self.base_url = f"http://{self.host}"
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.backoff = max(0.0, float(backoff))
        self.last_status: Optional[dict[str, Any]] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "DeviceClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------ transport

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True, limit=1),
                headers={"User-Agent": USER_AGENT, "Connection": "close", "Accept-Encoding": "identity"},
            )
        return self._session

    def _url(self, path: str, query: Optional[Mapping[str, str]]) -> URL:
        qs = ""
        if query:
            qs = "?" + "&".join(f"{quote(k, safe='')}={_quote(v)}" for k, v in query.items())
        return URL(self.base_url + _quote(path) + qs, encoded=True)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> _Reply:
        """One HTTP exchange; idempotent callers get ``retries`` extra attempts."""
        url = self._url(path, query)
        headers = {"Content-Type": content_type} if content_type else {}
        total = float(timeout or self.timeout)
        client_timeout = aiohttp.ClientTimeout(
            total=total, connect=min(CONNECT_TIMEOUT, total), sock_connect=min(CONNECT_TIMEOUT, total)
        )
        if body is None and method in ("POST", "PUT"):
            body = b""
        attempts = 1 + (self.retries if retries is None else max(0, retries))
        last: Optional[BaseException] = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                async with _device_lock(self.host):
                    session = self._get_session()
                    async with session.request(
                        method, url, data=body, headers=headers, timeout=client_timeout, allow_redirects=False
                    ) as resp:
                        return _Reply(resp.status, await resp.read())
            except _TRANSPORT_ERRORS as exc:
                last = exc
                log.debug("%s %s %s failed (attempt %d/%d): %s",
                          self.host, method, path, attempt + 1, attempts, _describe(exc))
        raise DeviceUnreachable(f"{self.host} did not answer {method} {path}: {_describe(last)}") from last

    # --------------------------------------------------------------- status

    async def status(
        self, *, timeout: float = STATUS_TIMEOUT, plugin: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """``/api/status`` as a dict, or None when the device is not reachable/not CrossPoint."""
        try:
            reply = await self._request(
                "GET", "/api/status", query={"plugin": plugin} if plugin else None, timeout=timeout, retries=0
            )
        except DeviceUnreachable:
            return None
        if reply.status != 200:
            return None
        try:
            data = reply.json()
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("version"), str):
            return None
        self.last_status = data
        return data

    async def firmware_version(self) -> Optional[tuple[int, int, int]]:
        """``(major, minor, patch)`` of the running firmware (uses the last status if any)."""
        st = self.last_status or await self.status()
        return parse_version(st.get("version")) if st else None

    # ---------------------------------------------------------------- files

    async def list_files(self, path: str = "/") -> list[DeviceFile]:
        """Entries of a folder. A missing folder lists as ``[]`` (device behaviour)."""
        folder = normalize_sd_path(path)
        reply = await self._request("GET", "/api/files", query={"path": folder})
        if reply.status != 200:
            raise DeviceRejected(f"listing {folder} failed: HTTP {reply.status} {reply.text[:200]}",
                                 status=reply.status)
        try:
            raw = reply.json()
        except ValueError as exc:
            raise DeviceRejected(f"listing {folder} is not JSON") from exc
        if not isinstance(raw, list):
            raise DeviceRejected(f"listing {folder} is not a JSON array")
        out: list[DeviceFile] = []
        for e in raw:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                out.append(
                    DeviceFile(
                        name=e["name"],
                        size=_as_int(e.get("size")),
                        is_dir=bool(e.get("isDirectory")),
                        is_epub=bool(e.get("isEpub")),
                    )
                )
        return out

    async def find(self, folder: str, name: str) -> Optional[DeviceFile]:
        """The entry called ``name`` in ``folder``, compared case-insensitively like FAT."""
        key = name.casefold()
        for entry in await self.list_files(folder):
            if entry.name.casefold() == key:
                return entry
        return None

    async def mkdir(self, parent: str, name: str) -> bool:
        """Create ``parent/name``. True if it was created or already exists (verified by listing).

        Missing parents are created by the firmware. Names starting with ``.`` are refused
        by the device (403), so they raise ValueError here.
        """
        _check_folder_name(name)
        parent = normalize_sd_path(parent)
        body = _form({"name": name, "path": parent})
        reply = await self._request("POST", "/mkdir", body=body, content_type=_FORM)
        if reply.status == 400 and reply.text in ("Missing folder name", "Folder name cannot be empty"):
            # The form we sent always has a name, so the server lost it: try once more.
            # (Seen with the crosspoint-simulator, whose worker thread races the
            # firmware's releaseRequestArguments(); the single-threaded device cannot.)
            await asyncio.sleep(self.backoff)
            reply = await self._request("POST", "/mkdir", body=body, content_type=_FORM)
        if not (reply.status == 200 or (reply.status == 400 and "already exists" in reply.text.lower())):
            log.warning("%s: mkdir %s refused: HTTP %d %s", self.host, sd_join(parent, name), reply.status, reply.text)
            return False
        entry = await self.find(parent, name)
        if entry is None or not entry.is_dir:
            log.warning("%s: %s is not a folder after mkdir (%s)", self.host, sd_join(parent, name), reply.text)
            return False
        return True

    async def makedirs(self, path: str) -> bool:
        """Ensure the folder ``path`` exists (one ``/mkdir``; the firmware creates parents)."""
        path = normalize_sd_path(path)
        if path == "/":
            return True
        parent, name = split_sd_path(path)
        return await self.mkdir(parent, name)

    async def download(self, path: str, *, size_hint: int = 0) -> bytes:
        """``GET /download?path=`` (both versions; dot-named files are refused with 403)."""
        path = normalize_sd_path(path)
        reply = await self._request("GET", "/download", query={"path": path}, timeout=upload_timeout(size_hint))
        if reply.status != 200:
            raise DeviceRejected(f"download of {path} failed: HTTP {reply.status} {reply.text[:200]}",
                                 status=reply.status)
        return reply.body

    async def delete(self, path: str) -> bool:
        """Delete one file (or empty folder). True when it is gone."""
        path = normalize_sd_path(path)
        reply = await self._request("POST", "/delete", body=_form({"path": path}), content_type=_FORM)
        if reply.status == 200:
            return True
        if "(not found)" in reply.text:
            return True  # a retried request after the first attempt already deleted it
        log.warning("%s: delete %s refused: HTTP %d %s", self.host, path, reply.status, reply.text)
        return False

    async def upload(self, folder: str, filename: str, data: bytes, *, max_names: int = 100) -> str:
        """Upload ``data`` as a new file in ``folder``; return the name it was stored under.

        The firmware never overwrites, so a taken name (case-insensitive) is retried as
        ``Name (2).ext``, ``Name (3).ext``, ... The result is confirmed by listing the
        folder (name and size); a connection lost mid-upload is resolved the same way, so
        a file the device did store is never uploaded twice.
        """
        check_filename(filename)
        folder = normalize_sd_path(folder)
        taken = {e.name.casefold() for e in await self.list_files(folder)}
        for n in range(1, max_names + 1):
            candidate = numbered_name(filename, n)
            if candidate.casefold() in taken:
                continue
            if await self._upload_as(folder, candidate, data) == "stored":
                return candidate
            taken.add(candidate.casefold())
        raise DeviceRejected(f"no free name for {filename!r} in {folder} after {max_names} tries")

    async def _upload_as(self, folder: str, name: str, data: bytes) -> Literal["stored", "exists"]:
        """Upload under exactly ``name`` (believed free). Raises on any other outcome."""
        body, content_type = build_multipart(name, data)
        transport_failures = 0
        while True:
            try:
                reply = await self._request(
                    "POST",
                    "/upload",
                    query={"path": folder},
                    body=body,
                    content_type=content_type,
                    timeout=upload_timeout(len(data)),
                    retries=0,  # not idempotent: resolved below by listing
                )
            except DeviceUnreachable:
                # The device may have stored the whole file before the connection broke.
                entry = await self.find(folder, name)
                if entry is not None and entry.is_dir:
                    return "exists"
                if entry is not None and entry.size == len(data):
                    log.info("%s: %s stored although the connection broke", self.host, sd_join(folder, name))
                    return "stored"
                if entry is not None:
                    await self.delete(sd_join(folder, name))  # our own partial file
                transport_failures += 1
                if transport_failures > self.retries:
                    raise
                await asyncio.sleep(self.backoff * transport_failures)
                continue
            break
        text = reply.text
        if reply.status == 200:
            if text != f"File uploaded successfully: {name}":
                log.warning("%s: unexpected upload answer %r for %r; verifying", self.host, text, name)
            await self._verify(folder, name, len(data))
            return "stored"
        if reply.status == 400 and text.startswith("File already exists"):
            return "exists"
        # A failed write leaves a truncated file behind on 1.6.5 (A:750-754); remove it.
        with contextlib.suppress(DeviceError):
            entry = await self.find(folder, name)
            if entry is not None and not entry.is_dir:
                await self.delete(sd_join(folder, name))
        raise DeviceRejected(f"upload of {name!r} to {folder} refused: HTTP {reply.status} {text}", status=reply.status)

    async def _verify(self, folder: str, name: str, size: int) -> None:
        if name.startswith("."):
            return  # hidden from /api/files unless showHiddenFiles is on: cannot verify
        entry = await self.find(folder, name)
        if entry is None:
            # One more look before concluding (a listing is cheap, a false failure is not:
            # the item would stay queued while its file is already on the card).
            await asyncio.sleep(self.backoff)
            entry = await self.find(folder, name)
        if entry is None or entry.is_dir:
            raise DeviceRejected(f"{sd_join(folder, name)} is not on the device after a successful upload")
        if entry.size != size:
            with contextlib.suppress(DeviceError):
                await self.delete(sd_join(folder, name))
            raise DeviceRejected(f"{sd_join(folder, name)} has {entry.size} bytes on the device, expected {size}")

    async def webdav_put(self, path: str, data: bytes) -> bool:
        """WebDAV ``PUT`` (overwrites). True when 201 created, False when 204 replaced.

        Paths with a dot-named segment are refused by the device (403), and the Library
        index is *not* marked dirty, so prefer :meth:`upload` for books.
        """
        path = normalize_sd_path(path)
        if has_hidden_segment(path):
            raise ValueError(f"WebDAV refuses paths with dot-named segments: {path}")
        reply = await self._request(
            "PUT", path, body=data, content_type="application/octet-stream", timeout=upload_timeout(len(data))
        )
        if reply.status in (200, 201, 204):
            return reply.status == 201
        raise DeviceRejected(f"WebDAV PUT {path} refused: HTTP {reply.status} {reply.text[:200]}", status=reply.status)

    # -------------------------------------------------------------- plugins

    async def plugins_enabled(self) -> Optional[bool]:
        """True/False for 2.x with Plugins on/off; None when the firmware has no plugin API.

        Probes ``POST /api/plugin-fs`` without a file part, which writes nothing (B:2479-2498).
        """
        reply = await self._request("POST", "/api/plugin-fs")
        if reply.status == 404:
            return None
        if reply.status == 403:
            return False
        return True

    async def write_plugin_file(self, plugin_name: str, sd_path: str, data: bytes) -> str:
        """Write (create or replace) one small file for a 2.x SD plugin.

        Returns the transport used: ``"plugin-fs"`` (2.x with Plugins on: atomic, creates
        parents), else a fallback because ``/api/plugin-fs`` is missing (1.6.5), disabled
        (Plugins off, which cannot be switched remotely) or the file exceeds 256 KiB:
        ``"webdav"`` (overwrite via temp file + rename; only for paths without dot-named
        segments, which WebDAV refuses) or ``"upload"`` (``/delete`` then ``/upload``:
        works in dot folders such as ``/.crosspoint/plugins``, but is not atomic).
        """
        if not plugin_name or any(s in plugin_name for s in ("/", "\\", "..")):
            raise ValueError(f"invalid plugin name {plugin_name!r}")
        if not sd_path.startswith("/") or ".." in sd_path:
            raise ValueError(f"plugin file path must be absolute without '..': {sd_path!r}")
        path = normalize_sd_path(sd_path)
        if is_sensitive_path(path):
            raise ValueError(f"{path} is a credential store the device protects")
        if not data:
            raise ValueError("refusing to write an empty plugin file (the device rejects empty bodies; write '{}')")
        parent, name = split_sd_path(path)
        check_filename(name, allow_hidden=True)

        if len(data) <= PLUGIN_FS_MAX_BYTES:
            body, content_type = build_multipart("data", data)
            reply = await self._request(
                "POST",
                "/api/plugin-fs",
                query={"plugin": plugin_name, "path": path},
                body=body,
                content_type=content_type,
                timeout=upload_timeout(len(data)),
            )
            if reply.status == 200:
                try:
                    result = reply.json()
                except ValueError:
                    result = {}
                if isinstance(result, dict) and result.get("ok") and _as_int(result.get("bytes")) == len(data):
                    return "plugin-fs"
                raise DeviceRejected(f"plugin-fs write of {path} answered {reply.text[:200]}", status=200)
            if reply.status == 403:
                reason = "the Plugins switch is off on the device"
            elif reply.status == 404:
                reason = "this firmware has no plugin API"
            elif reply.status == 413:
                reason = "the file is over the plugin-fs limit"
            else:
                raise DeviceRejected(f"plugin-fs write of {path} refused: HTTP {reply.status} {reply.text[:200]}",
                                     status=reply.status)
        else:
            reason = "the file is over the plugin-fs limit"
        log.info("%s: /api/plugin-fs unavailable (%s); writing %s through the file manager", self.host, reason, path)

        if not await self._ensure_folder(parent):
            raise DeviceRejected(f"could not create {parent} on the device")
        if has_hidden_segment(path):
            # WebDAV refuses dot segments (W:779-801); /upload refuses existing names.
            if name.startswith(".") or await self.find(parent, name) is not None:
                await self.delete(path)
            if await self._upload_as(parent, name, data) != "stored":
                raise DeviceRejected(f"could not replace {path} on the device")
            return "upload"
        await self.webdav_put(path, data)
        await self._verify(parent, name, len(data))
        return "webdav"

    async def _ensure_folder(self, folder: str) -> bool:
        folder = normalize_sd_path(folder)
        if folder == "/":
            return True
        _, leaf = split_sd_path(folder)
        if leaf.startswith("."):
            # /mkdir refuses dot-named leaves and /api/files hides them, so such a folder
            # can neither be created nor checked here. /.crosspoint always exists on a
            # device that has booted CrossPoint (settings live there).
            return True
        return await self.makedirs(folder)

    # ------------------------------------------------------------- settings

    async def get_settings(self) -> list[dict[str, Any]]:
        """``GET /api/settings``: the web-visible settings with their current values."""
        reply = await self._request("GET", "/api/settings")
        if reply.status != 200:
            raise DeviceRejected(f"settings read failed: HTTP {reply.status} {reply.text[:200]}", status=reply.status)
        try:
            raw = reply.json()
        except ValueError as exc:
            raise DeviceRejected("settings are not JSON") from exc
        if not isinstance(raw, list):
            raise DeviceRejected("settings are not a JSON array")
        return [s for s in raw if isinstance(s, dict) and isinstance(s.get("key"), str)]

    async def settings_values(self) -> dict[str, Any]:
        return {s["key"]: s.get("value") for s in await self.get_settings()}

    async def set_settings(self, values: Mapping[str, Any]) -> int:
        """``POST /api/settings``; returns how many settings the device applied.

        Toggles, enums and values are integers on the wire (booleans are converted);
        unknown or web-hidden keys are ignored by the device and not counted.
        """
        payload = {k: (int(v) if isinstance(v, bool) else v) for k, v in values.items()}
        reply = await self._request(
            "POST",
            "/api/settings",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
        if reply.status != 200:
            raise DeviceRejected(f"settings write failed: HTTP {reply.status} {reply.text[:200]}", status=reply.status)
        m = _APPLIED_RE.search(reply.text)
        return int(m.group(1)) if m else 0

    async def ensure_plugins_enabled(self) -> bool:
        """True if the SD plugin system is on.

        Stock 2.x hides ``pluginsEnabled`` from ``/api/settings`` (``SettingsList.cpp:194``,
        ``withWebHidden()``), so it cannot be switched on remotely: this returns False and
        the user has to turn on Settings > Connectivity > Plugins on the device. A firmware that
        exposes the key is switched through the API. 1.6.5 has no plugin system (False).
        """
        state = await self.plugins_enabled()
        if state is True:
            return True
        if state is None:
            return False
        try:
            keys = {s["key"] for s in await self.get_settings()}
        except DeviceRejected:
            return False
        if "pluginsEnabled" not in keys:
            log.info("%s: Plugins is off and cannot be enabled over the network", self.host)
            return False
        await self.set_settings({"pluginsEnabled": 1})
        return bool(await self.plugins_enabled())
