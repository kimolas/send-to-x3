#!/usr/bin/env python3
"""Run the CrossPoint 2.x plugin HTTP code (wire harness) against a server.

    python e2e/wire/wire.py --base-url http://127.0.0.1:8765 --token <device token> catalog
    python e2e/wire/wire.py --base-url ... --token ... download <item id>
    python e2e/wire/wire.py --base-url ... --token ... sleep [--book /Inbox/x.epub --percent 40]
    python e2e/wire/wire.py --base-url ... --token ... drain

The SD card folder (``--sd``, default ``$SENDTOX3_E2E_WORK/wire/sd``) keeps the plugin
outbox between calls, so ``sleep`` then ``drain`` behaves like two device drains. It is
(re)built with the plugin bundle when missing or with ``--fresh``; ``--bundle`` uses a
bundle zip (e.g. the app's) instead of the §10 template. Prints one JSON document: the
harness events, every request the device wrote (method, target, headers, body size),
and the files it left on the card.

Build the binary first: ``e2e/sim/build_sim.sh 2.0.0rc && e2e/wire/build_wire.sh 2.0.0rc``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from e2e.scenarios.wiretap import HttpMessage, parse_requests  # noqa: E402
from e2e.sim import runner, sd as sdmod  # noqa: E402
from e2e.sim.firmware import firmware  # noqa: E402


@dataclass
class WireResult:
    returncode: int
    events: list[dict]
    stderr: str
    sent: bytes = b""
    connects: int = 0
    files: dict[str, sdmod.FileInfo] = field(default_factory=dict)

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == kind]

    @property
    def rows(self) -> list[dict]:
        return self.of("row")

    @property
    def browse(self) -> Optional[dict]:
        b = self.of("browse")
        return b[0] if b else None

    @property
    def download(self) -> Optional[dict]:
        d = self.of("download")
        return d[0] if d else None

    def requests(self) -> list[HttpMessage]:
        return parse_requests(self.sent)

    def to_json(self) -> dict:
        return {
            "returncode": self.returncode,
            "events": [e for e in self.events if e.get("type") != "wire"],
            "connects": self.connects,
            "requests": [{"start_line": r.start_line, "headers": r.headers, "body_bytes": len(r.body)}
                         for r in self.requests()],
            "files": {k: {"size": v.size, "sha256": v.sha256} for k, v in self.files.items()},
            "log": self.stderr.splitlines()[-40:],
        }


def find_binary(ref: str = "2.0.0rc") -> Path:
    p = runner.work_dir() / "bin" / ref.replace("/", "_") / "wire_harness"
    if not p.is_file():
        raise FileNotFoundError(f"no wire harness for {ref} at {p}; run e2e/wire/build_wire.sh {ref}")
    return p


def prepare_sd(root: Path, *, ref: str = "2.0.0rc", base_url: str = "", token: str = "",
               bundle: Optional[bytes] = None) -> sdmod.SdCard:
    """A fresh card with the plugin (``bundle``, else the §10 template) and ``/Inbox``."""
    if bundle is None:
        if not base_url or not token:
            raise ValueError("need --base-url and --token (or a bundle)")
        bundle = sdmod.spec_bundle_zip(base_url, token)
    return sdmod.build_sd(root, firmware(ref), bundle=bundle)


def plugin_dir(root: Path) -> str:
    for base in ("/.crosspoint/plugins", "/plugins", "/.plugins"):
        host = sdmod.sd_path(root, base)
        if host.is_dir():
            for d in sorted(host.iterdir()):
                if (d / "device.json").is_file():
                    return f"{base}/{d.name}"
    raise FileNotFoundError(f"no plugin with a device.json on {root}")


def run(binary: Path, sd_root: Path, args: Sequence[str], *, timeout: float = 180) -> WireResult:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CROSSPOINT_")}
    env["CROSSPOINT_SIM_SD"] = str(sd_root)
    p = subprocess.run([str(binary), *args], env=env, capture_output=True, timeout=timeout, cwd=sd_root)
    events: list[dict] = []
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("@"):
            try:
                events.append(json.loads(line[1:]))
            except json.JSONDecodeError:
                events.append({"type": "unparsed", "line": line})
    wire = next((e for e in events if e.get("type") == "wire"), {})
    return WireResult(returncode=p.returncode, events=events, stderr=p.stderr.decode("utf-8", "replace"),
                      sent=base64.b64decode(wire.get("sent_b64", "")), connects=int(wire.get("connects", 0)),
                      files=sdmod.snapshot(sd_root))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="CrossPoint 2.x plugin HTTP over real sockets")
    ap.add_argument("--ref", default="2.0.0rc")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--bundle", type=Path, help="plugin bundle zip to install instead of the §10 template")
    ap.add_argument("--sd", type=Path, default=None)
    ap.add_argument("--fresh", action="store_true", help="rebuild the SD card folder first")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("catalog")
    c.add_argument("--page", type=int, default=1)
    d = sub.add_parser("download")
    d.add_argument("id")
    d.add_argument("--page", type=int, default=1)
    s = sub.add_parser("sleep")
    s.add_argument("--book", default="")
    s.add_argument("--percent", default="")
    sub.add_parser("drain")
    a = ap.parse_args(argv)

    root = (a.sd or runner.work_dir() / "wire" / "sd").resolve()
    if a.fresh or not root.exists():
        prepare_sd(root, ref=a.ref, base_url=a.base_url, token=a.token,
                   bundle=a.bundle.read_bytes() if a.bundle else None)
    pdir = plugin_dir(root)
    if a.cmd == "catalog":
        args = ["catalog", pdir, str(a.page)]
    elif a.cmd == "download":
        args = ["download", pdir, a.id, str(a.page)]
    elif a.cmd == "sleep":
        args = ["sleep"] + ([a.book, a.percent] if a.book else [])
    else:
        args = ["drain"]
    res = run(find_binary(a.ref), root, args)
    out = res.to_json()
    out["sd"] = str(root)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return res.returncode


if __name__ == "__main__":
    sys.exit(main())
