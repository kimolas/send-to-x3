"""Build and inspect a simulated SD card folder for the CrossPoint simulator.

The simulator maps ``$CROSSPOINT_SIM_SD`` to the card root (crosspoint-simulator
``src/HalStorage.cpp``). The on-card formats below are the firmware's own, read at
both supported tags (2.0.0rc and 1.6.5 use the same shapes):

``/.crosspoint/settings.json``
    flat object, keys from ``SettingsList`` (2.0.0rc ``src/SettingsList.cpp``, 1.6.5
    ``src/SettingsList.h``); missing keys keep their defaults and the firmware resaves the
    file in its current format. ``pluginsEnabled`` (default 0 on a clean card, 2.x only),
    ``pluginHubPromptHidden`` (hide the synthetic Plugin Hub row), ``opdsDownloadFolder``
    (default "" = SD root), ``opdsFilenameFormat`` (0 Author - Title, 1 Title - Author,
    2 Title).
``/.crosspoint/wifi.json``
    ``{"lastConnectedSsid", "credentials": [{"ssid", "password"}]}``. The firmware writes
    ``password_obf`` (XOR with the MAC, base64) plus ``password_len``/``password_crc32``,
    but still reads a legacy plaintext ``password`` and resaves it obfuscated
    (``lib/Serialization/PersistableStore.cpp:43-66``, ``src/WifiCredentialStore.cpp``).
    The sleep-time Wi-Fi join needs a credential for ``lastConnectedSsid``
    (2.0.0rc ``src/main.cpp:405-406``).
``/.crosspoint/opds.json``
    ``{"servers": [{"name", "url", "username", "password"}]}`` with the same legacy
    plaintext password rule (``src/OpdsServerStore.cpp``); 2.0.0rc adds optional
    ``headers: [{"name", "value_obf"}]``.
Plugins (2.x)
    ``/.crosspoint/plugins/<name>/device.json`` (+ ``manifest.json``, ``README.md``); the
    event outbox is ``<plugin dir>/events.jsonl`` (``src/util/PluginEvents.cpp:27``).
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Union

from .firmware import SIM_SSID, Firmware

PLUGIN_NAME = "sendtox3"
PLUGIN_DIR = f"/.crosspoint/plugins/{PLUGIN_NAME}"
INBOX = "/Inbox"

BytesLike = Union[bytes, bytearray, memoryview]


@dataclass(frozen=True)
class OpdsServer:
    """One entry of ``/.crosspoint/opds.json``."""

    name: str
    url: str
    username: str = ""
    password: str = ""


@dataclass(frozen=True)
class FileInfo:
    size: int
    sha256: str


@dataclass
class SdDiff:
    added: dict[str, FileInfo] = field(default_factory=dict)
    removed: dict[str, FileInfo] = field(default_factory=dict)
    changed: dict[str, tuple[FileInfo, FileInfo]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


# ---------------------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------------------


def sd_path(root: Path, path: str) -> Path:
    """Host path for an absolute SD path, refusing anything that escapes ``root``."""
    p = PurePosixPath(path)
    if not p.is_absolute():
        raise ValueError(f"SD paths must be absolute: {path!r}")
    parts = [x for x in p.parts[1:] if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise ValueError(f"SD path escapes the card: {path!r}")
    return Path(root, *parts)


def write_file(root: Path, path: str, data: Union[BytesLike, str]) -> Path:
    """Write ``data`` at SD ``path`` (parents created)."""
    host = sd_path(root, path)
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_bytes(data.encode("utf-8") if isinstance(data, str) else bytes(data))
    return host


def _json_bytes(doc: Any) -> bytes:
    # Strict JSON (ArduinoJson 7 rejects comments, NaN, trailing commas).
    return json.dumps(doc, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------------------
# Plugin bundle
# ---------------------------------------------------------------------------------------


def spec_device_json(base_url: str, device_token: str, *, delivery_format: str = "epub",
                     auto_delivery: bool = True, inbox: str = INBOX) -> dict:
    """``device.json`` exactly as docs/ARCHITECTURE.md §10 specifies it.

    Used when the app's own ``sendtox3.plugin_bundle`` is not importable, so the
    harness can run against any backend that implements the device API.
    """
    base = base_url.rstrip("/")
    auth = {"Authorization": f"Bearer {device_token}"}
    doc: dict[str, Any] = {
        "title": "Send to X3",
        "description": "Articles and notes you shared",
        "browse": {
            "format": "json",
            "url": f"{base}/device/v1/catalog?page={{page}}&limit={{limit}}",
            "headers": dict(auth),
            "items": "items",
            "fields": {"title": "title", "author": "subtitle", "id": "id"},
            "page_size": 8,
        },
        "download": {
            "url": f"{base}/device/v1/items/{{id}}.{delivery_format}",
            "headers": dict(auth),
            "dest_dir": inbox,
            "filename": f"{{title}}.{delivery_format}",
        },
    }
    if auto_delivery:
        doc["events"] = {
            "sleep.enter": {
                "download": {
                    "url": f"{base}/device/v1/deliver?ev={{event.id}}&ts={{event.ts}}",
                    "headers": dict(auth),
                    "dest": f"{inbox}/{{event.ts}}-{{event.id}}.epub",
                },
                "toast": "Send to X3: new reading delivered",
            }
        }
    return doc


def spec_bundle_zip(base_url: str, device_token: str, **kwargs: Any) -> bytes:
    """A bundle zip with the same layout as ``plugin_bundle.build_bundle_zip``."""
    inbox = kwargs.get("inbox", INBOX).strip("/")
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{PLUGIN_DIR.lstrip('/')}/device.json",
                   json.dumps(spec_device_json(base_url, device_token, **kwargs), indent=2) + "\n")
        z.writestr(f"{PLUGIN_DIR.lstrip('/')}/manifest.json",
                   json.dumps({"title": "Send to X3", "description": "Articles and notes you shared"}) + "\n")
        z.writestr(f"{inbox}/.keep", b"")
    return bio.getvalue()


def extract_bundle(root: Path, bundle: Union[BytesLike, Path, str]) -> list[str]:
    """Extract a plugin bundle zip at the card root; returns the SD paths written.

    Refuses absolute names, ``..`` segments, symlinks and duplicate entries, like a
    careful user copying the folder over would end up with.
    """
    data = Path(bundle).read_bytes() if isinstance(bundle, (str, Path)) else bytes(bundle)
    written: list[str] = []
    seen: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or any(seg == ".." for seg in name.split("/")):
                raise ValueError(f"unsafe bundle entry {info.filename!r}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"symlink in bundle: {info.filename!r}")
            if name in seen:
                raise ValueError(f"duplicate bundle entry {info.filename!r}")
            seen.add(name)
            target = "/" + name.rstrip("/")
            if name.endswith("/"):
                sd_path(root, target).mkdir(parents=True, exist_ok=True)
                continue
            write_file(root, target, z.read(info))
            written.append(target)
    return written


# ---------------------------------------------------------------------------------------
# Card builder
# ---------------------------------------------------------------------------------------


@dataclass
class SdCard:
    """A built card: its host folder and what was put on it."""

    root: Path
    firmware: Firmware
    plugin_dir: Optional[str] = None
    opds_servers: list[OpdsServer] = field(default_factory=list)
    inbox: str = INBOX

    def path(self, sd: str) -> Path:
        return sd_path(self.root, sd)

    def snapshot(self, *, include_hidden: bool = True) -> dict[str, FileInfo]:
        return snapshot(self.root, include_hidden=include_hidden)

    def inbox_files(self) -> dict[str, FileInfo]:
        """Visible files in the inbox folder (``.keep`` and dot files excluded)."""
        folder = self.path(self.inbox)
        out: dict[str, FileInfo] = {}
        if folder.is_dir():
            for p in sorted(folder.iterdir()):
                if p.is_file() and not p.name.startswith("."):
                    out[p.name] = _info(p)
        return out

    def outbox(self) -> list[dict]:
        """Queued plugin events (``events.jsonl``), oldest first."""
        if not self.plugin_dir:
            return []
        p = self.path(self.plugin_dir + "/events.jsonl")
        if not p.is_file():
            return []
        lines = []
        for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if raw:
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    lines.append({"_unparsed": raw})
        return lines

    def settings(self) -> dict:
        p = self.path("/.crosspoint/settings.json")
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def build_sd(
    root: Union[Path, str],
    fw: Firmware,
    *,
    plugins_enabled: bool = True,
    hide_plugin_hub: bool = True,
    wifi: bool = True,
    ssid: str = SIM_SSID,
    wifi_password: str = "",
    bundle: Union[BytesLike, Path, str, None] = None,
    plugin_files: Optional[Mapping[str, Union[BytesLike, str]]] = None,
    opds_servers: Iterable[OpdsServer] = (),
    opds_download_folder: str = INBOX,
    opds_filename_format: Optional[int] = None,
    inbox: Optional[str] = INBOX,
    files: Optional[Mapping[str, Union[BytesLike, str]]] = None,
    settings: Optional[Mapping[str, Any]] = None,
    clean: bool = True,
) -> SdCard:
    """Create a fresh simulated card at ``root``.

    ``bundle`` is a plugin bundle zip (bytes or path) extracted at the root;
    ``plugin_files`` maps absolute SD paths to content as an alternative. ``files``
    pre-places books or other content. ``settings`` adds or overrides settings keys.
    ``inbox`` (default ``/Inbox``) is created empty unless None: sleep-time event
    downloads never create folders (2.0.0rc ``PluginEvents.cpp:336-366``).
    """
    root = Path(root)
    if clean and root.exists():
        shutil.rmtree(root)
    (root / ".crosspoint").mkdir(parents=True, exist_ok=True)

    cfg: dict[str, Any] = {}
    if fw.plugins:
        cfg["pluginsEnabled"] = 1 if plugins_enabled else 0
        cfg["pluginHubPromptHidden"] = 1 if hide_plugin_hub else 0
    if opds_download_folder:
        if not opds_download_folder.startswith("/") or len(opds_download_folder.encode()) > 63:
            raise ValueError("opds_download_folder must start with / and fit 63 bytes")
        cfg["opdsDownloadFolder"] = opds_download_folder
    if opds_filename_format is not None:
        cfg["opdsFilenameFormat"] = int(opds_filename_format)
    cfg.update(settings or {})
    write_file(root, "/.crosspoint/settings.json", _json_bytes(cfg))

    if wifi:
        write_file(root, "/.crosspoint/wifi.json", _json_bytes(
            {"lastConnectedSsid": ssid, "credentials": [{"ssid": ssid, "password": wifi_password}]}))

    servers = list(opds_servers)
    if servers:
        if len(servers) > 8:
            raise ValueError("the firmware keeps at most 8 OPDS servers")
        write_file(root, "/.crosspoint/opds.json", _json_bytes({"servers": [
            {"name": s.name, "url": s.url, "username": s.username, "password": s.password} for s in servers]}))

    plugin_dir: Optional[str] = None
    written: list[str] = []
    if bundle is not None:
        if not fw.plugins:
            raise ValueError(f"{fw.ref} has no plugin system; do not pass a bundle")
        written += extract_bundle(root, bundle)
    for path, data in (plugin_files or {}).items():
        write_file(root, path, data)
        written.append(path)
    for path in written:
        if path.endswith("/device.json"):
            plugin_dir = path.rsplit("/", 1)[0]
            break

    if inbox:
        sd_path(root, inbox).mkdir(parents=True, exist_ok=True)
    for path, data in (files or {}).items():
        write_file(root, path, data)

    return SdCard(root=root, firmware=fw, plugin_dir=plugin_dir, opds_servers=servers, inbox=inbox or INBOX)


# ---------------------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------------------


def _info(p: Path) -> FileInfo:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return FileInfo(size=p.stat().st_size, sha256=h.hexdigest())


def snapshot(root: Union[Path, str], *, include_hidden: bool = True) -> dict[str, FileInfo]:
    """``{"/sd/path": FileInfo}`` for every regular file on the card."""
    root = Path(root)
    out: dict[str, FileInfo] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(root)
        if not include_hidden and any(part.startswith(".") for part in rel.parts):
            continue
        out["/" + rel.as_posix()] = _info(p)
    return out


def diff(before: Mapping[str, FileInfo], after: Mapping[str, FileInfo]) -> SdDiff:
    d = SdDiff()
    for k, v in after.items():
        if k not in before:
            d.added[k] = v
        elif before[k] != v:
            d.changed[k] = (before[k], v)
    for k, v in before.items():
        if k not in after:
            d.removed[k] = v
    return d


# ---------------------------------------------------------------------------------------
# File browser order (FsHelpers::sortFileList, identical in 1.6.5 and 2.0.0rc)
# ---------------------------------------------------------------------------------------


def natural_less(a: str, b: str) -> bool:
    """Port of ``FsHelpers::naturalLess`` (lib/FsHelpers/FsHelpers.cpp:85-130).

    Byte-wise, ASCII-case-insensitive, digit runs compared numerically after skipping
    leading zeros.
    """
    s1, s2 = a.encode("utf-8"), b.encode("utf-8")
    i = j = 0
    isdig = lambda c: 48 <= c <= 57  # noqa: E731
    lower = lambda c: c + 32 if 65 <= c <= 90 else c  # noqa: E731
    while i < len(s1) and j < len(s2):
        if isdig(s1[i]) and isdig(s2[j]):
            while i < len(s1) and s1[i] == 48:
                i += 1
            while j < len(s2) and s2[j] == 48:
                j += 1
            l1 = 0
            while i + l1 < len(s1) and isdig(s1[i + l1]):
                l1 += 1
            l2 = 0
            while j + l2 < len(s2) and isdig(s2[j + l2]):
                l2 += 1
            if l1 != l2:
                return l1 < l2
            for k in range(l1):
                if s1[i + k] != s2[j + k]:
                    return s1[i + k] < s2[j + k]
            i += l1
            j += l2
        else:
            c1, c2 = lower(s1[i]), lower(s2[j])
            if c1 != c2:
                return c1 < c2
            i += 1
            j += 1
    return i >= len(s1) and j < len(s2)


BROWSER_EXTENSIONS = {
    # 2.0.0rc FileBrowserActivity.cpp:134-135 (reflowable + xtc + images);
    # 1.6.5 :135-137 (epub, xtc, txt, md, bmp, png).
    "2": (".epub", ".txt", ".md", ".xtc", ".xtch", ".bmp", ".png", ".jpg", ".jpeg"),
    "1": (".epub", ".txt", ".md", ".xtc", ".xtch", ".bmp", ".png"),
}


def browser_listing(root: Union[Path, str], folder: str, fw: Firmware, *, show_hidden: bool = False) -> list[str]:
    """Rows of the File Browser for ``folder``: directories (with a trailing ``/``)
    first, then supported files, each group in the firmware's natural order."""
    host = sd_path(Path(root), folder)
    exts = BROWSER_EXTENSIONS["2" if fw.version.startswith("2") else "1"]
    rows: list[str] = []
    for p in host.iterdir():
        name = p.name
        if (not show_hidden and name.startswith(".")) or name == "System Volume Information":
            continue
        if p.is_dir():
            rows.append(name + "/")
        elif name.lower().endswith(exts):
            rows.append(name)

    def cmp(x: str, y: str) -> int:
        dx, dy = x.endswith("/"), y.endswith("/")
        if dx != dy:
            return -1 if dx else 1
        return -1 if natural_less(x, y) else (1 if natural_less(y, x) else 0)

    return sorted(rows, key=functools.cmp_to_key(cmp))
