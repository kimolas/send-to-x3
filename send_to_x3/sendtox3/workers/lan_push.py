"""LAN push: deliver queued items to an X3 whose File Transfer server is open.

Works with stock CrossPoint 1.6.5 and 2.x (both run the same File Transfer server; see
:mod:`sendtox3.device_client` for the firmware facts). The device never announces
itself and only serves while the user has *File Transfer* or *Calibre Wireless* open,
so this worker polls ``GET /api/status`` every ``push_poll_seconds`` with a short
timeout. While the device answers, every pending item (never delivered on any channel)
is uploaded as ``<title>.<delivery_format>`` into ``push_folder``, one request at a
time, and marked ``delivered.push`` only after the device's file listing shows the file
with the right size. Uploads stop as soon as the device stops answering; the rest go
out the next time it appears.

Guarantees:

- No double push: an item is marked right after its upload verified; an identical copy
  already in the folder (same name or a numbered variant, same bytes, e.g. because the
  bookkeeping was lost in a crash or the item was requeued) is marked without uploading
  it again.
- Name collisions: the firmware never overwrites, so a taken name becomes
  ``Title (2).epub``, ``Title (3).epub``, ... The name used stays reserved
  (``Store.issue_title``), so no later item gets it as its title.
- An item the device refuses (e.g. disk full, unwritable name) is not retried until the
  device next reappears, and never blocks the other items.
- The worker never raises (except ``CancelledError``); errors land in the status dict.

State contract (duck-typed, see :class:`LanPushState`)::

    state.opts     Options  device_host, push_poll_seconds, push_folder, delivery_format
    state.store    Store    pending_items(), get(), file_bytes(), mark_delivered(), issue_title()
    state.status   dict     this worker owns state.status["lan_push"]:
        state: str           AppState contract: "waiting" (device away), "ok", "warning"
                             (some items refused), "error", "disabled"
        detail: str          one human sentence for the admin UI
        updated: int         unix time of the last status change
        enabled: bool        False when device_host is empty (the worker then returns)
        host: str            device_host as configured
        folder: str          destination folder on the device
        format: str          "epub" | "txt"
        online: bool         the device answered the last poll
        last_seen: int|None  unix time of the last answer
        device_version: str  firmware version from /api/status ("" until seen)
        device: str          device model from /api/status ("X3")
        pending: int         items waiting for delivery at the last poll while online
        last_push: int|None  unix time of the last delivered item
        last_pushed: str     device path of the last delivered item
        pushed_count: int    items delivered by this worker since start
        last_error: str      last problem ("" if none yet)
        last_error_at: int|None
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Protocol

from .. import delivery
from ..device_client import (
    DeviceClient,
    DeviceError,
    DeviceFile,
    DeviceRejected,
    DeviceUnreachable,
    normalize_sd_path,
    numbered_name,
    sanitize_filename,
    sd_join,
    split_ext,
)

if TYPE_CHECKING:
    from ..config import Options
    from ..models import Item
    from ..store import Store

log = logging.getLogger(__name__)

STATUS_TIMEOUT = 2.5  # seconds per status poll (the device answers in well under 1 s)
MIN_POLL_INTERVAL = 1.0  # floor for push_poll_seconds
ITEM_GAP_SECONDS = 0.2  # breathing room for the device between uploads
REQUEST_TIMEOUT = 8.0  # small API calls while pushing
IDENTICAL_COPY_LOOKUP = 20  # how many numbered variants count as "already there"
_DISK_FULL_HINTS = ("disk may be full", "failed to write final data")


class LanPushState(Protocol):
    opts: "Options"
    store: "Store"
    status: dict[str, Any]


@dataclass
class PushResult:
    """Outcome of one :func:`push_items` batch."""

    delivered: list[tuple[str, str]] = field(default_factory=list)  # (item id, device path)
    uploaded: int = 0  # how many of ``delivered`` were actual uploads
    failed: dict[str, str] = field(default_factory=dict)  # item id -> reason
    skipped: list[str] = field(default_factory=list)  # delivered elsewhere meanwhile / deleted
    device_lost: bool = False
    error: str = ""  # batch-level problem (device lost, folder not creatable, disk full)


def push_filename(item: "Item", fmt: str) -> str:
    """Device file name for ``item``: its (already firmware-safe) title plus extension."""
    return sanitize_filename(item.title, fmt, fallback=f"Note {item.id}")


def _reserve_name(store: "Store", name: str) -> None:
    """Keep ``name`` (``Title.epub`` or a numbered ``Title (2).epub``) from ever becoming
    a new item's title: a catalog download of that item would land on this file (and on
    the Send to X3 firmware open it instead). ``mark_delivered`` reserves the title only."""
    store.issue_title(split_ext(name)[0])


async def _identical_copy(
    client: DeviceClient, folder: str, listing: dict[str, DeviceFile], filename: str, data: bytes
) -> Optional[DeviceFile]:
    """A file in ``folder`` named ``filename`` (or a numbered variant) with exactly ``data``.

    Name and size select candidates; the bytes are compared after downloading, since a
    different book can have the same size. Raises DeviceUnreachable if the device left.
    """
    for n in range(1, IDENTICAL_COPY_LOOKUP + 1):
        entry = listing.get(numbered_name(filename, n).casefold())
        if entry is None or entry.is_dir or entry.size != len(data):
            continue
        try:
            if await client.download(sd_join(folder, entry.name), size_hint=len(data)) == data:
                return entry
        except DeviceRejected:
            continue
    return None


async def push_items(
    client: DeviceClient,
    store: "Store",
    items: list["Item"],
    *,
    folder: str,
    fmt: str,
) -> PushResult:
    """Upload ``items`` (in order) to ``folder``; mark each ``push`` once verified."""
    result = PushResult()
    if not items:
        return result
    folder = normalize_sd_path(folder)
    try:
        if not await client.makedirs(folder):
            result.error = f"could not create {folder} on the device"
            return result
        listing = {e.name.casefold(): e for e in await client.list_files(folder)}
    except DeviceUnreachable as exc:
        result.device_lost, result.error = True, str(exc)
        return result
    except (DeviceRejected, ValueError) as exc:
        result.error = f"cannot use {folder} on the device: {exc}"
        return result

    for index, item in enumerate(items):
        current = store.get(item.id)
        if current is None or current.delivered:
            result.skipped.append(item.id)  # deleted, or delivered through another channel
            continue
        try:
            data = store.file_bytes(item.id, fmt)
        except (OSError, ValueError) as exc:
            result.failed[item.id] = f"no {fmt} file: {exc}"
            continue
        filename = push_filename(current, fmt)

        try:
            copy = await _identical_copy(client, folder, listing, filename, data)
        except DeviceUnreachable as exc:
            result.device_lost, result.error = True, str(exc)
            break
        if copy is not None:
            store.mark_delivered(item.id, "push")
            _reserve_name(store, copy.name)
            result.delivered.append((item.id, sd_join(folder, copy.name)))
            log.info("LAN push: %s already on the device as %s", item.id, copy.name)
            continue

        if index and result.uploaded:
            await asyncio.sleep(ITEM_GAP_SECONDS)
        try:
            name = await client.upload(folder, filename, data)
        except DeviceUnreachable as exc:
            result.device_lost, result.error = True, str(exc)
            break
        except DeviceRejected as exc:
            result.failed[item.id] = str(exc)
            if any(h in str(exc).lower() for h in _DISK_FULL_HINTS):
                result.error = f"device storage problem: {exc}"
                break
            continue
        except ValueError as exc:  # a file name the device cannot store
            result.failed[item.id] = str(exc)
            continue
        store.mark_delivered(item.id, "push")
        _reserve_name(store, name)
        listing[name.casefold()] = DeviceFile(name, len(data), False, name.lower().endswith(".epub"))
        result.delivered.append((item.id, sd_join(folder, name)))
        result.uploaded += 1
        log.info("LAN push: delivered %s as %s", item.id, sd_join(folder, name))
    return result


def _init_status(state: LanPushState, host: str, folder: str, fmt: str) -> dict[str, Any]:
    status: dict[str, Any] = {
        "state": "waiting" if host else "disabled",
        "detail": "",
        "updated": int(time.time()),
        "enabled": bool(host),
        "host": host,
        "folder": folder,
        "format": fmt,
        "online": False,
        "last_seen": None,
        "device_version": "",
        "device": "",
        "pending": 0,
        "last_push": None,
        "last_pushed": "",
        "pushed_count": 0,
        "last_error": "",
        "last_error_at": None,
    }
    state.status["lan_push"] = status
    return status


def _publish(status: dict[str, Any], state: str, detail: str) -> None:
    """The ``state``/``detail``/``updated`` triple the admin UI shows (see app_state.py)."""
    status.update(state=state, detail=detail, updated=int(time.time()))


def _set_error(status: dict[str, Any], message: str) -> None:
    status["last_error"] = message
    status["last_error_at"] = int(time.time())


def _waiting_detail(host: str) -> str:
    return f"Waiting for the X3 at {host}: open File Transfer on the device to receive new items."


class _Pusher:
    """Poll loop state: whether the device was online, and items refused this session."""

    def __init__(self, state: LanPushState, client: DeviceClient, status: dict[str, Any], folder: str, fmt: str):
        self.state = state
        self.client = client
        self.status = status
        self.folder = folder
        self.fmt = fmt
        self.online = False
        self.refused: dict[str, str] = {}  # item id -> reason, since the device (re)appeared

    def went_offline(self, detail: str = "") -> None:
        self.online = False
        self.status["online"] = False
        _publish(self.status, "waiting", detail or _waiting_detail(self.client.host))

    async def tick(self) -> None:
        info = await self.client.status(timeout=STATUS_TIMEOUT)
        if info is None:
            if self.online:
                log.info("LAN push: device %s went away", self.client.host)
            if self.online or self.status["state"] != "waiting":
                self.went_offline()  # keeps a more specific "waiting" detail while away
            return
        if not self.online:
            log.info("LAN push: device %s is online (CrossPoint %s)", self.client.host, info.get("version"))
            self.refused.clear()  # a new File Transfer session: retry everything
        self.online = True
        version = str(info.get("version", ""))
        self.status.update(
            online=True, last_seen=int(time.time()), device_version=version, device=str(info.get("device", ""))
        )
        # Items the X3 already received in a "Get N new" and will confirm are not pushed again.
        waiting = delivery.awaiting_confirmation(self.state.store)
        pending = [i for i in self.state.store.pending_items() if i.id not in self.refused and i.id not in waiting]
        self.status["pending"] = len(pending)
        if pending:
            result = await push_items(self.client, self.state.store, pending, folder=self.folder, fmt=self.fmt)
            self._record(result)
            if result.device_lost or result.error:
                return  # _record published "waiting" / "error"
        self._publish_online(version)

    def _publish_online(self, version: str) -> None:
        name = f"X3 online (CrossPoint {version})" if version else "X3 online"
        if self.refused:
            item_id, reason = next(reversed(self.refused.items()))
            _publish(self.status, "warning",
                     f"{name}; {len(self.refused)} item(s) not delivered, e.g. {item_id}: {reason}")
        elif self.status["last_push"]:
            _publish(self.status, "ok", f"{name}; last delivered {self.status['last_pushed']}")
        else:
            _publish(self.status, "ok", f"{name}; nothing to deliver")

    def _record(self, result: PushResult) -> None:
        now = int(time.time())
        if result.delivered:
            self.status["pushed_count"] += len(result.delivered)
            self.status["last_push"] = now
            self.status["last_pushed"] = result.delivered[-1][1]
        self.status["pending"] = max(0, self.status["pending"] - len(result.delivered) - len(result.skipped))
        for item_id, reason in result.failed.items():
            self.refused[item_id] = reason
            log.warning("LAN push: item %s not delivered: %s", item_id, reason)
            _set_error(self.status, f"item {item_id}: {reason}")
        if result.error:
            log.warning("LAN push: %s", result.error)
            _set_error(self.status, result.error)
        if result.device_lost:
            left = self.status["pending"]
            self.went_offline(f"The X3 left File Transfer during the push; {left} item(s) wait for next time.")
        elif result.error:
            _publish(self.status, "error", result.error)


async def _idle_forever() -> None:
    """Options are read once at start: a bad device_host stays bad until a restart, so
    stay quiet instead of returning (the supervisor would restart us in a loop)."""
    await asyncio.Event().wait()


async def run(state: LanPushState) -> None:
    """Long-running worker; returns at once when ``opts.device_host`` is empty."""
    opts = state.opts
    host = (opts.device_host or "").strip()
    fmt = opts.delivery_format if opts.delivery_format in ("epub", "txt") else "epub"
    folder = normalize_sd_path(opts.push_folder or "/Inbox")
    status = _init_status(state, host, folder, fmt)
    if not host:
        _publish(status, "disabled", "Set device_host in the options to push files to the X3.")
        log.info("LAN push disabled (no device_host configured)")
        return
    try:
        client = DeviceClient(host, timeout=REQUEST_TIMEOUT)
    except ValueError as exc:
        status["enabled"] = False
        _set_error(status, f"invalid device_host: {exc}")
        _publish(status, "error", f"Invalid device_host {host!r}: {exc}")
        log.error("LAN push disabled: invalid device_host %r: %s", host, exc)
        await _idle_forever()
        return
    interval = max(MIN_POLL_INTERVAL, float(opts.push_poll_seconds or 0))
    pusher = _Pusher(state, client, status, folder, fmt)
    _publish(status, "waiting", _waiting_detail(client.host))
    log.info("LAN push: watching %s every %.0f s (folder %s, %s)", client.host, interval, folder, fmt)
    try:
        while True:
            try:
                await pusher.tick()
            except asyncio.CancelledError:
                raise
            except DeviceError as exc:
                _set_error(status, str(exc))
                pusher.went_offline()
                log.warning("LAN push: %s", exc)
            except Exception as exc:  # never let the worker die
                log.exception("LAN push: unexpected error")
                _set_error(status, f"unexpected error: {exc!r}")
                _publish(status, "error", f"Unexpected error: {exc!r}"[:300])
            await asyncio.sleep(interval)
    finally:
        status["online"] = False
        await client.close()
