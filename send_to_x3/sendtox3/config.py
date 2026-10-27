"""Runtime options.

Inside Home Assistant the Supervisor writes validated options to /data/options.json
before every start. Outside HA (dev, tests, plain docker) the same file is read from
``$SENDTOX3_DATA_DIR/options.json`` and any option can be overridden with an
environment variable ``SENDTOX3_<OPTION_NAME_UPPER>``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

DEFAULT_PORT = 8765


@dataclass
class Options:
    log_level: str = "info"

    # Network identity the X3 uses to reach this app ("192.168.1.50" or "192.168.1.50:8765").
    # Empty = auto-detect (Supervisor network info, else first non-loopback IPv4).
    server_host: str = ""
    port: int = DEFAULT_PORT  # container port; the host port may be remapped in HA

    # Tokens: empty = generated on first start and persisted in the data dir.
    ingest_token: str = ""
    device_token: str = ""
    admin_token: str = ""

    # Email ingest (IMAP, outbound polling only)
    imap_enabled: bool = False
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_processed_folder: str = ""  # optional move target after processing
    imap_plus_tag: str = ""  # accept only mail addressed to <user>+<tag>@...
    allowed_senders: list[str] = field(default_factory=list)  # empty = any sender
    require_dmarc: bool = True

    # Home Assistant Companion app share sheet (mobile_app.share events)
    ha_share_enabled: bool = True

    # LAN push to a CrossPoint device in File Transfer mode (works on 1.6.5 and 2.x)
    device_host: str = ""  # X3 IP/hostname; empty disables
    push_poll_seconds: int = 20
    push_folder: str = "/Inbox"

    # CrossPoint 2.x plugin
    auto_delivery: bool = True  # sleep.enter zero-tap delivery
    delivery_format: str = "epub"  # "epub" | "txt" for catalog downloads and LAN push
    inbox_folder: str = "/Inbox"

    retention_days: int = 30
    pdf_enabled: bool = True
    fetch_timeout_seconds: int = 20
    allow_private_urls: bool = False  # SSRF guard for URL fetching

    @property
    def data_dir(self) -> Path:
        return Path(os.environ.get("SENDTOX3_DATA_DIR", "/data"))


def _coerce(value: Any, typ: Any) -> Any:
    if typ is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if typ is int:
        return int(value)
    if typ is str:
        return "" if value is None else str(value)
    if getattr(typ, "__origin__", None) is list:
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        return [v.strip() for v in str(value).split(",") if v.strip()]
    return value


def load_options(path: str | os.PathLike | None = None) -> Options:
    data_dir = Path(os.environ.get("SENDTOX3_DATA_DIR", "/data"))
    p = Path(path) if path else data_dir / "options.json"
    raw: dict[str, Any] = {}
    if p.exists():
        raw = json.loads(p.read_text(encoding="utf-8") or "{}")
    hints = get_type_hints(Options)
    kwargs: dict[str, Any] = {}
    for f in fields(Options):
        env = os.environ.get("SENDTOX3_" + f.name.upper())
        if env is not None:
            kwargs[f.name] = _coerce(env, hints[f.name])
        elif f.name in raw and raw[f.name] is not None:
            kwargs[f.name] = _coerce(raw[f.name], hints[f.name])
    opts = Options(**kwargs)
    opts.delivery_format = opts.delivery_format if opts.delivery_format in ("epub", "txt") else "epub"
    opts.inbox_folder = "/" + opts.inbox_folder.strip("/") if opts.inbox_folder.strip("/") else "/Inbox"
    opts.push_folder = "/" + opts.push_folder.strip("/") if opts.push_folder.strip("/") else "/Inbox"
    opts.allowed_senders = [s.lower() for s in opts.allowed_senders]
    return opts
