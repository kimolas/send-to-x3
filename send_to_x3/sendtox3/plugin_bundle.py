"""CrossPoint 2.x SD-card plugin: ``device.json`` generation, zip bundle, LAN install.

Firmware facts (docs/ARCHITECTURE.md §10, research report plugins-catalog.md):

- The plugin lives in ``/.crosspoint/plugins/sendtox3/`` (hidden root, so the Library
  does not list its README). Folder names must be <= 23 bytes for event subscriptions.
- ``device.json`` must be strict JSON (ArduinoJson, no comments) and <= 8 KiB, or the
  plugin shows up as "Web-only plugin".
- Server URL and token are hard-coded: with no ``config.json`` an unresolved
  ``{cfg.server}`` can never break the URLs.
- A JSON catalog never falls back to ``fields.url``: ``download.url`` is required.
- Event downloads do not create folders, so ``/Inbox`` must exist (``Inbox/.keep``).
"""

from __future__ import annotations

import io
import ipaddress
import json
import logging
import os
import re
import socket
import time
import zipfile
from typing import Any, Optional
from urllib.parse import urlsplit

from . import __version__
from .config import Options

log = logging.getLogger(__name__)

PLUGIN_NAME = "sendtox3"
PLUGIN_DIR = f"/.crosspoint/plugins/{PLUGIN_NAME}"
PLUGIN_TITLE = "Send to X3"
PLUGIN_DESCRIPTION = "Articles and notes you shared"
MANIFEST_MAX_BYTES = 8 * 1024
DELIVERY_TOAST = "Send to X3: new reading delivered"
#: Where the (off by default) plugin switch lives on the X3 (SettingsList.cpp:193-194).
PLUGINS_SWITCH = "Settings > Connectivity > Plugins"
#: CrossPoint 2.0.0rc looks up the saved Wi-Fi password at sleep without loading
#: wifi.json (main.cpp:405; only WifiSelectionActivity loads it), and every wake is a
#: reboot, so a plain wake -> read -> sleep only queues the event. Fixed upstream after
#: the tag by develop commit facaa87 (2026-10-23). The queued event is delivered at the
#: next Wi-Fi join (every join drains the plugin outbox) or at a sleep taken with Wi-Fi on.
SLEEP_DELIVERY_CAVEAT = (
    "This needs a CrossPoint build newer than 2.0.0rc (with the fix \"Load WiFi settings\n"
    "  from file before sleep mode\", develop commit facaa87). On 2.0.0rc new items arrive\n"
    "  the next time the X3 joins Wi-Fi instead: opening Home > Plugins > Send to X3, OPDS,\n"
    "  File Transfer > Join a Network or KOReader sync (or a sleep taken while Wi-Fi is\n"
    "  still on)."
)

SUPERVISOR_URL = "http://supervisor"
#: ``resolve_base_url`` source when ``server_host`` is set but cannot be used.
INVALID_SERVER_HOST = "invalid server_host"
_HOST_PORT_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::(?P<port>\d{1,5}))?$")

assert len(PLUGIN_NAME.encode()) <= 23 and len(PLUGIN_DIR.encode()) <= 63


# --------------------------------------------------------------------------------------
# device.json
# --------------------------------------------------------------------------------------


def normalize_base_url(base_url: str) -> str:
    """``http(s)://host[:port][/prefix]`` without a trailing slash; raises ValueError."""
    url = (base_url or "").strip().rstrip("/")
    if not re.match(r"^https?://", url, re.I):
        raise ValueError(f"server address must start with http:// or https:// (got {base_url!r})")
    if re.search(r"[\s{}\"\\<>]", url) or "?" in url or "#" in url:
        raise ValueError(f"server address contains characters the X3 cannot use: {base_url!r}")
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError(f"server address has no host: {base_url!r}")
    if "@" in parts.netloc:
        # The X3 would send "user@host" as the host name; the same check as the
        # host-only form of server_host, which has no user part either.
        raise ValueError(f"server address must not contain a user name or password: {base_url!r}")
    try:
        parts.port  # noqa: B018 - raises for "host:abc", out-of-range or an unbracketed IPv6 address
    except ValueError as exc:
        raise ValueError(f"server address has an invalid port ({exc}): {base_url!r}") from None
    if parts.netloc.endswith(":"):
        raise ValueError(f"server address has an empty port: {base_url!r}")
    return url


def _check_token(token: str) -> str:
    if not token or not re.fullmatch(r"[\x21-\x7e]+", token) or re.search(r'[{}"\\]', token):
        # "{" would be expanded by the firmware's template engine; quotes/backslashes and
        # whitespace would break the header or the JSON.
        raise ValueError("device token must be non-empty printable ASCII without {, }, quotes or spaces")
    return token


def device_token_problem(token: str) -> str:
    """Why ``token`` cannot be used in device.json (and so not by the X3 plugin), or ""."""
    try:
        _check_token(token)
    except ValueError as exc:
        return str(exc)
    return ""


def _inbox(opts: Options) -> str:
    folder = "/" + (opts.inbox_folder or "/Inbox").strip().strip("/")
    if folder == "/" or ".." in folder or re.search(r"[{}\\:*?\"<>|]", folder):
        raise ValueError(f"invalid inbox folder {opts.inbox_folder!r}")
    return folder


#: Where the plugin reports files it saved (its ``book.downloaded`` event), so a "Get N
#: new" bundle counts as delivered only once it is really on the SD card.
CONFIRM_PATH = "/device/v1/fetched"
#: The plugin's event queue on the X3 (PluginEvents.cpp outboxPath).
OUTBOX_PATH = f"{PLUGIN_DIR}/events.jsonl"


def build_device_json(*, base_url: str, device_token: str, opts: Options) -> dict:
    """The plugin manifest exactly as in §10.

    Default (``auto_delivery`` off): downloads carry ``?confirm=1`` and a
    ``book.downloaded`` request reports each saved file; no event joins Wi-Fi by itself
    (only ``sleep.enter`` does), so the report goes out the next time the X3 uses Wi-Fi,
    which for the plugin is before its list loads. With ``auto_delivery`` the events are
    ``sleep.enter`` only: its "nothing new" answers keep its line queued, which would hold
    back the reports queued behind it, so downloads are marked as they are sent instead.
    """
    base = normalize_base_url(base_url)
    token = _check_token(device_token)
    inbox = _inbox(opts)
    fmt = opts.delivery_format if opts.delivery_format in ("epub", "txt") else "epub"
    auth = f"Bearer {token}"
    doc: dict[str, Any] = {
        "title": PLUGIN_TITLE,
        "description": PLUGIN_DESCRIPTION,
        "browse": {
            "format": "json",
            "url": f"{base}/device/v1/catalog?page={{page}}&limit={{limit}}",
            "headers": {"Authorization": auth},
            "items": "items",
            "fields": {"title": "title", "author": "subtitle", "id": "id"},
            "page_size": 8,
        },
        "download": {
            "url": f"{base}/device/v1/items/{{id}}.{fmt}" + ("" if opts.auto_delivery else "?confirm=1"),
            "headers": {"Authorization": auth},
            "dest_dir": inbox,
            "filename": f"{{title}}.{fmt}",
        },
    }
    if opts.auto_delivery:
        doc["events"] = {
            "sleep.enter": {
                "download": {
                    "url": f"{base}/device/v1/deliver?ev={{event.id}}&ts={{event.ts}}",
                    "headers": {"Authorization": auth},
                    "dest": f"{inbox}/{{event.ts}}-{{event.id}}.epub",
                },
                "toast": DELIVERY_TOAST,
            }
        }
    else:
        doc["events"] = {
            "book.downloaded": {
                "request": {
                    # {event.plugin}: every plugin's catalog downloads fire this event.
                    "url": f"{base}{CONFIRM_PATH}?plugin={{event.plugin}}",
                    "method": "POST",
                    "headers": {"Authorization": auth, "Content-Type": "text/plain; charset=utf-8"},
                    "body": "{event.path}",
                },
            }
        }
    return doc


def device_json_bytes(*, base_url: str, device_token: str, opts: Options) -> bytes:
    """Strict JSON, UTF-8, <= 8 KiB (raises ValueError otherwise)."""
    data = (json.dumps(build_device_json(base_url=base_url, device_token=device_token, opts=opts),
                       indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MANIFEST_MAX_BYTES:
        raise ValueError(f"device.json is {len(data)} bytes; the firmware reads at most {MANIFEST_MAX_BYTES}")
    return data


def manifest_json_bytes() -> bytes:
    return (json.dumps({"title": PLUGIN_TITLE, "description": PLUGIN_DESCRIPTION, "version": __version__},
                       indent=2) + "\n").encode("utf-8")


def readme_bytes(*, base_url: str, opts: Options) -> bytes:
    inbox = _inbox(opts)
    auto = (
        f"- When the X3 goes to sleep on your home Wi-Fi it fetches everything new as one\n"
        f"  EPUB into {inbox} (a short \"Loading\" popup, then the toast \"{DELIVERY_TOAST}\").\n"
        f"  {SLEEP_DELIVERY_CAVEAT}\n"
        if opts.auto_delivery
        else "- Nothing is downloaded by itself (delivery at sleep, option auto_delivery, is off).\n"
    )
    confirm = (
        "" if opts.auto_delivery else
        " The X3 tells the app which files it saved\n"
        "  the next time it joins Wi-Fi (after you go back to Home and open Send to X3\n"
        "  again), which is why Home > Plugins says \"Receives: downloads\". A cancelled\n"
        "  download is offered again."
    )
    text = f"""# Send to X3

Reading shared from your phone or computer, served by the Send to X3 app on Home
Assistant at {normalize_base_url(base_url)}.

## Use it

- Turn on {PLUGINS_SWITCH} on the X3 (it is off by default).
- Home > Plugins > Send to X3, then Confirm on the first row, "Get 3 new": everything
  shared since the last fetch is saved as one book into {inbox}. With nothing new the
  row reads "Nothing new right now".{confirm}
- The rows below list everything you shared, newest first. Select one to download it
  on its own. The subtitle says whether the X3 already received it.
{auto}
## Files

- device.json: catalog and download settings (and sleep delivery when it is on). The
  server address and the device token are written into it; download a new bundle from
  the app's Setup page if either changes.
- manifest.json: name and version.

Version {__version__}.
"""
    return text.encode("utf-8")


def build_bundle_zip(*, base_url: str, device_token: str, opts: Options) -> bytes:
    """Zip to extract at the SD card root: the plugin folder plus an empty Inbox."""
    files = plugin_files(base_url=base_url, device_token=device_token, opts=opts)
    inbox = _inbox(opts).strip("/")
    stamp = time.localtime()[:6]
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as z:
        dirs = [".crosspoint/", ".crosspoint/plugins/", f".crosspoint/plugins/{PLUGIN_NAME}/"]
        parts = inbox.split("/")
        dirs += ["/".join(parts[: i + 1]) + "/" for i in range(len(parts))]
        for d in dirs:
            zi = zipfile.ZipInfo(d, date_time=stamp)
            # drwxr-xr-x plus the MS-DOS directory bit: without a mode, ZipInfo defaults
            # to 0600 and Info-ZIP unzip creates folders nobody can open.
            zi.external_attr = (0o40755 << 16) | 0x10
            z.writestr(zi, b"")
        for sd_path, data in files.items():
            zi = zipfile.ZipInfo(sd_path.lstrip("/"), date_time=stamp)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o100644 << 16  # -rw-r--r--
            z.writestr(zi, data)
        keep = zipfile.ZipInfo(f"{inbox}/.keep", date_time=stamp)
        keep.external_attr = 0o100644 << 16
        z.writestr(keep, b"")
        # An empty event queue: an older plugin's queued sleep.enter lines would otherwise
        # take up the X3's 4-events-per-Wi-Fi-join budget ahead of the download reports.
        outbox = zipfile.ZipInfo(OUTBOX_PATH.lstrip("/"), date_time=stamp)
        outbox.external_attr = 0o100644 << 16
        z.writestr(outbox, b"")
    return bio.getvalue()


def plugin_files(*, base_url: str, device_token: str, opts: Options) -> dict[str, bytes]:
    """``{absolute SD path: bytes}`` for the plugin folder."""
    return {
        f"{PLUGIN_DIR}/device.json": device_json_bytes(base_url=base_url, device_token=device_token, opts=opts),
        f"{PLUGIN_DIR}/manifest.json": manifest_json_bytes(),
        f"{PLUGIN_DIR}/README.md": readme_bytes(base_url=base_url, opts=opts),
    }


# --------------------------------------------------------------------------------------
# Install over the LAN (X3 in File Transfer mode)
# --------------------------------------------------------------------------------------


def _firmware_major(version: str) -> Optional[int]:
    m = re.match(r"\s*v?(\d+)\.", version or "")
    return int(m.group(1)) if m else None


def _err_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


async def install_to_device(device_host: str, *, base_url: str, device_token: str, opts: Options,
                            client: Any = None, timeout: float = 15.0) -> list[str]:
    """Write the plugin onto an X3 whose File Transfer screen is open. Returns log lines;
    a line starting with ``Error:`` means the install did not complete.

    Uses :class:`sendtox3.device_client.DeviceClient`: ``status()`` (None when
    unreachable), ``mkdir()``, ``write_plugin_file()`` (atomic ``/api/plugin-fs`` when
    Plugins is on, else its own delete+upload fallback) and ``plugins_enabled()``.
    Device settings are never changed (the Plugins switch cannot be set remotely).
    """
    lines: list[str] = []
    host = (device_host or "").strip()
    if not host:
        return ["Error: no X3 address given. Enter the IP address the X3 shows in File Transfer > Join a Network."]
    try:
        files = plugin_files(base_url=base_url, device_token=device_token, opts=opts)
        inbox = _inbox(opts)
    except ValueError as exc:
        return [f"Error: {exc}"]

    if client is None:
        try:
            from .device_client import DeviceClient

            client = DeviceClient(host, timeout=timeout)
        except (ImportError, ValueError) as exc:
            return [f"Error: cannot talk to {host!r} ({_err_text(exc)})."]

    try:
        lines.append(f"Connecting to the X3 at {host} ...")
        try:
            status = await client.status()
        except Exception as exc:  # noqa: BLE001
            status, reason = None, _err_text(exc)
        else:
            reason = "no answer"
        if not isinstance(status, dict):
            lines.append(f"Error: could not reach the X3 at {host} ({reason}). On the X3 open File Transfer > "
                         "Join a Network, keep that screen open, and try again.")
            return lines
        version = str(status.get("version", ""))
        model = str(status.get("device", "")) or "a CrossPoint device"
        lines.append(f"Found {model}" + (f" running CrossPoint {version}." if version else "."))
        major = _firmware_major(version)
        if "pluginApi" not in status and major is not None and major < 2:
            lines.append(f"Error: CrossPoint {version} has no plugin support. Use the OPDS feed instead "
                         "(see Setup), or update the X3 to CrossPoint 2.0.")
            return lines

        inbox_parent, inbox_name = inbox.rsplit("/", 1)
        try:
            if await client.mkdir(inbox_parent or "/", inbox_name) is False:
                lines.append(f"Warning: could not create {inbox}; create it on the SD card, or deliveries "
                             "at sleep will fail.")
            else:
                lines.append(f"{inbox} is ready.")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"Warning: could not create {inbox} ({_err_text(exc)}).")

        for sd_path, data in files.items():
            try:
                how = await client.write_plugin_file(PLUGIN_NAME, sd_path, data)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"Error: writing {sd_path} failed ({_err_text(exc)}).")
                return lines
            via = f" via {how}" if isinstance(how, str) and how else ""
            lines.append(f"Wrote {sd_path} ({len(data)} bytes{via}).")
        # Events queued for the old manifest (0.1.0's sleep.enter lines) would hold back
        # the download reports for many Wi-Fi joins; the device refuses empty uploads, so
        # delete the queue instead of writing an empty one.
        remove = getattr(client, "delete", None)
        if remove is not None:
            try:
                if await remove(OUTBOX_PATH) is False:
                    lines.append(f"Warning: could not clear {OUTBOX_PATH}; downloads may be confirmed late.")
            except Exception as exc:  # noqa: BLE001 - informational
                lines.append(f"Warning: could not clear {OUTBOX_PATH} ({_err_text(exc)}).")

        enabled: Optional[bool] = None
        probe = getattr(client, "plugins_enabled", None)
        if probe is not None:
            try:
                enabled = await probe()
            except Exception:  # noqa: BLE001 - informational only
                enabled = None
        if enabled is False:
            lines.append("Done: the plugin is installed, but Plugins is off on the X3. Turn it on in "
                         f"{PLUGINS_SWITCH}, then open Home > Plugins > Send to X3.")
        else:
            lines.append("Done: the plugin is installed. Leave File Transfer, then open Home > Plugins > "
                         f"Send to X3 (if Plugins is missing, turn it on in {PLUGINS_SWITCH}).")
        return lines
    finally:
        await _close(client)


async def _close(client: Any) -> None:
    for name in ("aclose", "close"):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        try:
            res = fn()
            if hasattr(res, "__await__"):
                await res
        except Exception:  # noqa: BLE001
            pass
        return


# --------------------------------------------------------------------------------------
# Server address the X3 uses
# --------------------------------------------------------------------------------------


async def detect_base_url(opts: Options, *, supervisor_url: str = SUPERVISOR_URL,
                          supervisor_token: Optional[str] = None, timeout: float = 3.0) -> str:
    """``http://<host>:<port>`` for the X3: see :func:`resolve_base_url`."""
    return (await resolve_base_url(opts, supervisor_url=supervisor_url,
                                   supervisor_token=supervisor_token, timeout=timeout))[0]


async def resolve_base_url(opts: Options, *, supervisor_url: str = SUPERVISOR_URL,
                           supervisor_token: Optional[str] = None,
                           timeout: float = 3.0) -> tuple[str, str]:
    """(base_url, how) from, in order: ``opts.server_host`` (may include a port, or a
    full ``http(s)://`` URL); the Supervisor API (host IP + mapped host port); the first
    non-loopback IPv4 of this machine. ``how`` is "options", "supervisor" or
    "local address"; ("", "") if nothing worked, and ("", INVALID_SERVER_HOST) when
    ``server_host`` is set but unusable (logged; never raises, so startup continues)."""
    configured = (opts.server_host or "").strip()
    if configured:
        try:
            return _from_server_host(configured, opts.port), "options"
        except ValueError as exc:
            log.error("server_host %r is invalid: %s", configured, exc)
            return "", INVALID_SERVER_HOST
    token = supervisor_token if supervisor_token is not None else (
        os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or "")
    if token:
        try:
            ip, port = await _supervisor_address(supervisor_url, token, opts.port, timeout)
            if ip:
                return f"http://{ip}:{port}", "supervisor"
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the host address from the Supervisor: %s", exc)
    ip = local_ipv4()
    if ip:
        return f"http://{ip}:{opts.port}", "local address"
    return "", ""


def _from_server_host(value: str, default_port: int) -> str:
    """``http://host:port`` from a ``server_host`` option; raises ValueError."""
    if re.match(r"^https?://", value, re.I):
        url = normalize_base_url(value)
        # A typed port 0 is refused here, as in the host-only form below; not in
        # normalize_base_url, which also gets the app's own port (0 for an ephemeral
        # port, as the tests use).
        if urlsplit(url).port == 0:
            raise ValueError(f"server_host has an invalid port (must be 1-65535): {value!r}")
        return url
    value = value.strip().strip("/")
    m = _HOST_PORT_RE.match(value)
    if not m or (m.group("port") is not None and not 0 < int(m.group("port")) < 65536):
        raise ValueError("server_host must be an IP address or host name, optionally with :port "
                         f"(for example 192.168.1.50 or 192.168.1.50:8765), or a full http:// URL; got {value!r}")
    # IPv6 literals ([...]) are kept as given, although the X3 cannot parse them.
    return normalize_base_url(f"http://{value}" if m.group("port") else f"http://{value}:{default_port}")


async def _supervisor_address(base: str, token: str, container_port: int, timeout: float) -> tuple[str, int]:
    import aiohttp

    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=headers) as s:
        async with s.get(f"{base}/network/info") as r:
            r.raise_for_status()
            info = (await r.json(content_type=None)).get("data") or {}
        ip = _pick_interface_ip(info.get("interfaces") or [])
        port = container_port
        try:
            async with s.get(f"{base}/addons/self/info") as r:
                r.raise_for_status()
                network = ((await r.json(content_type=None)).get("data") or {}).get("network") or {}
            mapped = network.get(f"{container_port}/tcp")
            if mapped:
                port = int(mapped)
            elif f"{container_port}/tcp" in network:
                log.warning("the app's network port %s/tcp is disabled in Home Assistant; the X3 cannot "
                            "reach it until a host port is set", container_port)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the mapped host port from the Supervisor: %s", exc)
    return ip, port


def _pick_interface_ip(interfaces: list[dict]) -> str:
    def first_ipv4(iface: dict) -> str:
        for addr in (iface.get("ipv4") or {}).get("address") or []:
            ip = str(addr).split("/")[0]
            try:
                if ipaddress.ip_address(ip).version == 4 and not ipaddress.ip_address(ip).is_loopback:
                    return ip
            except ValueError:
                continue
        return ""

    usable = [i for i in interfaces if i.get("enabled", True) and i.get("connected", True)]
    for iface in sorted(usable, key=lambda i: not i.get("primary", False)):
        ip = first_ipv4(iface)
        if ip:
            return ip
    return ""


def local_ipv4() -> str:
    """First non-loopback IPv4 (UDP connect trick: no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed anywhere
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                return ip
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return ""


def under_supervisor() -> bool:
    """True when running as a Home Assistant app (the Supervisor gave us a token)."""
    return bool(os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN"))


def looks_container_internal(base_url: str) -> bool:
    """True for addresses in 172.16.0.0/12: Docker's and Home Assistant's internal
    networks, which the X3 cannot reach. Some home LANs use that range too, so only
    treat a detected address as unreachable when :func:`under_supervisor`."""
    try:
        host = urlsplit(base_url).hostname or ""
        return ipaddress.ip_address(host) in ipaddress.ip_network("172.16.0.0/12")
    except ValueError:
        return False
