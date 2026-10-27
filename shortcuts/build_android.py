#!/usr/bin/env python3
"""Generate the HTTP Shortcuts (Android) import file for "Send to X3".

HTTP Shortcuts (ch.rmy.android.http_shortcuts, MIT, github.com/Waboodoo/HTTP-Shortcuts)
imports a plain JSON file in its export format. This writes one category with one
shortcut that appears in Android's share sheet::

    python3 shortcuts/build_android.py          # -> shortcuts/android/http-shortcuts-send-to-x3.json

The file itself is built by ``send_to_x3/sendtox3/phone.py`` (format notes there), the
same code the app's Setup page uses for its download prefilled with the server's
address and token. That module uses only the standard library, and so does this script.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "android" / "http-shortcuts-send-to-x3.json"

_PACKAGE_ROOT = str(HERE.parent / "send_to_x3")
if _PACKAGE_ROOT not in sys.path:
    sys.path.insert(0, _PACKAGE_ROOT)

from sendtox3.phone import (  # noqa: E402  (needs the path above)
    CATEGORY_ID,
    CODE_ON_FAILURE,
    CODE_ON_PREPARE,
    CODE_ON_SUCCESS,
    COMPATIBILITY_VERSION,
    FORMAT_VERSION,
    KEY_EMAIL,
    KEY_TEXT,
    KEY_TITLE,
    KEY_TOKEN,
    KEY_URL,
    SHORTCUT_ID,
    SHORTCUT_NAME,
    SOURCE,
    TIMEOUT_MS,
    VARIABLE_IDS,
    AndroidConfig,
    build_http_shortcuts,
    http_shortcuts_bytes,
    placeholder,
)

__all__ = [
    "CATEGORY_ID", "CODE_ON_FAILURE", "CODE_ON_PREPARE", "CODE_ON_SUCCESS", "COMPATIBILITY_VERSION", "DEFAULT_OUT",
    "FORMAT_VERSION", "KEY_EMAIL", "KEY_TEXT", "KEY_TITLE", "KEY_TOKEN", "KEY_URL", "SHORTCUT_ID", "SHORTCUT_NAME",
    "SOURCE", "TIMEOUT_MS", "VARIABLE_IDS", "AndroidConfig", "build_http_shortcuts", "http_shortcuts_bytes", "main",
    "placeholder", "write_http_shortcuts",
]


def write_http_shortcuts(path: Path = DEFAULT_OUT, cfg: Optional[AndroidConfig] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = http_shortcuts_bytes(cfg)
    if not (path.is_file() and path.read_bytes() == data):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Generate the HTTP Shortcuts import file for Send to X3.")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output file (default: {DEFAULT_OUT})")
    p.add_argument("--ingest-url", default="", help="e.g. http://192.168.1.50:8765/api/v1/ingest")
    p.add_argument("--token", default="", help="ingest token (avoid in files you share)")
    p.add_argument("--email", default="", help="ingest email address for the away-from-home fallback")
    args = p.parse_args(argv)
    try:
        cfg = AndroidConfig(ingest_url=args.ingest_url.strip(), token=args.token.strip(), email=args.email.strip())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {write_http_shortcuts(args.out, cfg)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
