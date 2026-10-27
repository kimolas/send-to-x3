"""Process-wide runtime state shared by the web app and the background workers.

Workers receive the :class:`AppState` (``async def run(app_state)``) and use:

- ``app_state.opts``       resolved :class:`~sendtox3.config.Options` (tokens filled in)
- ``app_state.store``      the :class:`~sendtox3.store.Store`
- ``app_state.processor``  the :class:`~sendtox3.ingest.Processor`
- ``app_state.base_url``   ``http://<host>:<port>`` the X3 uses to reach this app
- ``app_state.status``     channel health shown in the admin UI

``status`` maps a channel name (``imap``, ``ha_share``, ``lan_push``, ``device_api``,
``opds``) to a dict ``{"state": "ok"|"error"|"warning"|"waiting"|"starting"|"disabled",
"detail": str, "updated": unix_ts, ...}``. Prefer :meth:`AppState.set_status`; the admin
UI also tolerates plain strings or dicts with ``ok``/``error`` keys.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .config import Options
from .store import Store

if TYPE_CHECKING:  # pragma: no cover
    from .ingest import Processor

#: Option names of the three secrets; the same names are used as ``Store.secret`` keys.
SECRET_NAMES: tuple[str, ...] = ("ingest_token", "device_token", "admin_token")

#: Human labels for the channels shown on the admin page, in display order.
CHANNEL_LABELS: dict[str, str] = {
    "device_api": "X3 plugin (CrossPoint 2.x)",
    "opds": "OPDS feed (CrossPoint 1.6.5+)",
    "lan_push": "LAN push to the X3",
    "imap": "Email (IMAP)",
    "ha_share": "Home Assistant share sheet",
}

STATES: tuple[str, ...] = ("ok", "warning", "error", "waiting", "starting", "disabled", "unknown")


def resolve_secrets(opts: Options, store: Store) -> dict[str, bool]:
    """Fill empty tokens in ``opts`` from the store (generating them on first start).

    Returns ``{option_name: was_configured}`` so callers can log whether each token
    came from the options or was generated, without ever logging the value.
    """
    configured: dict[str, bool] = {}
    for name in SECRET_NAMES:
        value = (getattr(opts, name) or "").strip()
        configured[name] = bool(value)
        setattr(opts, name, store.secret(name, value))
    return configured


def rotate_secret(opts: Options, store: Store, name: str) -> str:
    """Replace a *generated* token with a new random one, persisted where
    :meth:`Store.secret` keeps it, and set it in ``opts``. (A token set in the options
    cannot be rotated here: the options value wins again at the next start.)"""
    if name not in SECRET_NAMES:
        raise ValueError(f"unknown secret {name!r}")
    value = secrets.token_urlsafe(24)
    store.kv_set(f"secret:{name}", value)
    setattr(opts, name, value)
    return value


@dataclass
class AppState:
    opts: Options
    store: Store
    processor: "Processor | Any"
    base_url: str = ""
    base_url_source: str = ""  # "options", "supervisor", "local address", "invalid server_host" or "" (unknown)
    #: ``resolve_secrets`` result: {token name: True if set in the options, False if generated}.
    configured_secrets: dict[str, bool] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)
    started_at: int = field(default_factory=lambda: int(time.time()))

    def set_status(self, channel: str, state: str, detail: str = "", **extra: Any) -> None:
        """Record the health of a channel for the admin UI."""
        self.status[channel] = {"state": state, "detail": detail, "updated": int(time.time()), **extra}

    def channel_status(self, channel: str) -> dict[str, Any]:
        """Normalized status of a channel: always a dict with state/detail/updated."""
        return normalize_status(self.status.get(channel))


# Worker-specific "state" values (workers/imap.py) mapped onto the badge states.
_STATE_ALIASES = {
    "connected": "ok", "syncing": "ok", "idle": "ok", "polling": "ok", "online": "ok", "running": "ok",
    "connecting": "starting", "backoff": "error", "not_configured": "error", "stopped": "disabled",
    "offline": "waiting",
}
_UPDATED_KEYS = ("updated", "ts", "last_ok", "last_seen", "last_sync_at", "last_event", "last_push",
                 "last_connected", "connected_since", "last_error_at")


def _ts(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _infer_state(raw: dict[str, Any]) -> str:
    if raw.get("ok") is True:
        return "ok"
    if raw.get("ok") is False or raw.get("error"):
        return "error"
    if raw.get("enabled") is False:
        return "disabled"
    last_error_at = _ts(raw.get("last_error_at"))
    last_good = max(_ts(raw.get(k)) for k in ("last_seen", "last_connected", "last_sync_at", "connected_since",
                                                "last_push", "last_event"))
    for flag in ("connected", "online"):
        if flag in raw:
            if raw[flag]:
                return "ok"
            if raw.get("last_error") and last_error_at >= last_good:
                return "error"
            return "waiting"
    if raw.get("last_error"):
        return "error"
    return "unknown"


def _summary(raw: dict[str, Any], state: str) -> str:
    if state in ("error", "waiting") and raw.get("last_error"):
        return str(raw["last_error"])
    if raw.get("warning"):
        return str(raw["warning"])
    parts = []
    if raw.get("reason"):
        parts.append(str(raw["reason"]))
    if "online" in raw and raw.get("host"):
        parts.append(f"X3 at {raw['host']} " + ("online" if raw["online"] else "not in File Transfer mode"))
    if raw.get("device_version"):
        parts.append(f"CrossPoint {raw['device_version']}")
    for key, label in (("items_created", "items created"), ("pushed_count", "files pushed"),
                       ("messages_processed", "messages"), ("events_received", "shares received")):
        if isinstance(raw.get(key), int) and raw[key]:
            parts.append(f"{raw[key]} {label}")
    return ", ".join(parts)


def normalize_status(raw: Any) -> dict[str, Any]:
    """Accept whatever a worker stored and return ``{"state", "detail", "updated"}``.

    Understands :meth:`AppState.set_status` dicts, plain strings, ``{"ok": bool}`` and
    the workers' own status dicts (``enabled``/``online``/``connected``/``last_error``...).
    """
    if raw is None:
        return {"state": "unknown", "detail": "", "updated": 0}
    if isinstance(raw, str):
        state = raw if raw in STATES else _STATE_ALIASES.get(raw, "ok")
        return {"state": state, "detail": "" if raw in STATES or raw in _STATE_ALIASES else raw, "updated": 0}
    if isinstance(raw, dict):
        given = str(raw.get("state") or "").lower()
        state = given if given in STATES else _STATE_ALIASES.get(given) or _infer_state(raw)
        if state == "ok" and raw.get("enabled") is False:
            state = "disabled"
        detail = raw.get("detail") or raw.get("message") or (raw.get("error") if isinstance(raw.get("error"), str)
                                                              else "") or _summary(raw, state)
        if not detail and given and given not in STATES:
            detail = given.replace("_", " ")
        updated = max((_ts(raw.get(k)) for k in _UPDATED_KEYS), default=0)
        return {"state": state, "detail": str(detail), "updated": updated}
    return {"state": "unknown", "detail": str(raw), "updated": 0}
