"""Validate the Send to X3 app against the real Home Assistant Supervisor code.

This imports the Supervisor's own validators from a source checkout (no running
Supervisor needed) and checks, the way the Supervisor does when it loads a store:

* ``repository.yaml``       with ``supervisor.store.validate.SCHEMA_REPOSITORY_CONFIG``
* app discovery             the ``**/config.*`` scan of ``StoreData._find_app_configs``
                            (exactly one app, ``send_to_x3``)
* ``send_to_x3/config.yaml`` with ``supervisor.apps.validate.SCHEMA_APP_CONFIG``; any
                            advisory it logs (deprecations, misconfiguration) fails
* ``translations/*.yaml``   with ``SCHEMA_APP_TRANSLATIONS``, plus coverage of every
                            schema option and every port
* option sets               merged onto the defaults like ``App.options`` and validated
                            with ``AppOptions`` (what ``write_options`` writes to
                            /data/options.json); valid sets must pass, broken ones
                            must be rejected; every accepted set is then loaded with
                            ``sendtox3.config.load_options`` to prove the Python side
                            reads exactly what the Supervisor writes
* the configuration form    rendered with ``UiOptions``
* the security rating       with ``supervisor.apps.utils.rating_security``

Usage (``e2e/ha/validate_config.sh`` clones the Supervisor and builds the venv)::

    python3 -I e2e/ha/validate_config.py --supervisor /path/to/supervisor-checkout

Exit status 0 when everything passes. Needs Python >= 3.14 and the Supervisor's
requirements.txt installed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[2]
APP_DIR = REPO / "send_to_x3"
SUPERVISOR_TAG = "2026.10.4"
EXPECTED_RATING = 7  # 5 base + 2 ingress (no custom AppArmor profile yet)


# --------------------------------------------------------------------- reporting
@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        line = what if not detail else f"{what}: {detail}"
        (self.passed if ok else self.failed).append(line)
        print(("PASS " if ok else "FAIL ") + line, flush=True)
        return ok


class _Capture(logging.Handler):
    """Collects WARNING+ records the Supervisor emits while validating."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())

    def take(self) -> list[str]:
        out, self.records = self.records, []
        return out


# ------------------------------------------------------------------ option sets
def valid_option_sets(defaults: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """User option sets (as the frontend would save them) that must be accepted."""
    return {
        "defaults (nothing saved yet)": {},
        "every option saved unchanged": deepcopy(defaults),
        "optional text fields cleared": {
            k: v for k, v in defaults.items() if not (isinstance(v, str) and v == "")
        },
        "email ingest, LAN push, fixed tokens": {
            "log_level": "debug",
            "server_host": "192.168.1.50:8765",
            "ingest_token": "x" * 32,
            "device_token": "y" * 32,
            "admin_token": "z" * 32,
            "imap_enabled": True,
            "imap_user": "reader@example.com",
            "imap_password": "app password with spaces",
            "imap_folder": "x3",
            "imap_processed_folder": "x3/done",
            "imap_plus_tag": "x3-7f2k",
            "allowed_senders": ["Me@Example.com", "other@example.org"],
            "require_dmarc": False,
            "ha_share_enabled": False,
            "device_host": "192.168.1.77",
            "push_poll_seconds": 5,
            "push_folder": "Reading",
            "auto_delivery": False,
            "delivery_format": "txt",
            "inbox_folder": "/Inbox/",
            "retention_days": 3650,
            "pdf_enabled": False,
            "fetch_timeout_seconds": 120,
            "allow_private_urls": True,
        },
        "numbers given as strings": {"imap_port": "993", "retention_days": "7"},
    }


def invalid_option_sets() -> dict[str, dict[str, Any]]:
    """User option sets the Supervisor must refuse (so the UI shows an error)."""
    return {
        "unknown delivery_format": {"delivery_format": "pdf"},
        "unknown log_level": {"log_level": "verbose"},
        "retention_days 0 (would delete everything)": {"retention_days": 0},
        "push_poll_seconds below 5": {"push_poll_seconds": 1},
        "fetch_timeout_seconds above 120": {"fetch_timeout_seconds": 600},
        "imap_port 0": {"imap_port": 0},
        "imap_enabled not a bool": {"imap_enabled": "maybe"},
        "allowed_senders not a list": {"allowed_senders": "me@example.com"},
        "required imap_host null": {"imap_host": None},
    }


# ------------------------------------------------------------------------ checks
def _import_supervisor(src: Path) -> SimpleNamespace:
    sys.path.insert(0, str(src))
    from supervisor.apps.options import AppOptions, UiOptions
    from supervisor.apps.utils import rating_security
    from supervisor.apps.validate import SCHEMA_APP_CONFIG, SCHEMA_APP_TRANSLATIONS
    from supervisor.const import FILE_SUFFIX_CONFIGURATION, SECURITY_DEFAULT, SECURITY_DISABLE, SECURITY_PROFILE
    from supervisor.store.validate import SCHEMA_REPOSITORY_CONFIG
    from supervisor.utils.common import find_one_filetype, read_json_or_yaml_file

    try:
        from supervisor.apps.app import _OPTIONS_MERGER as merger
    except ImportError:  # pragma: no cover - older layouts
        from deepmerge import Merger

        merger = Merger([(dict, ["merge"])], ["override"], ["override"])

    return SimpleNamespace(**locals())


def _version_of(src: Path) -> str:
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(src), "describe", "--tags", "--always"], capture_output=True, text=True, check=False
    )
    return out.stdout.strip() or "unknown version"


def check_repository(sv: SimpleNamespace, rep: Report) -> None:
    import voluptuous as vol

    try:
        path = sv.find_one_filetype(REPO, "repository", sv.FILE_SUFFIX_CONFIGURATION)
        data = sv.SCHEMA_REPOSITORY_CONFIG(sv.read_json_or_yaml_file(path))
        rep.check(True, "repository.yaml is valid", f"name={data['name']!r}")
    except (vol.Invalid, Exception) as err:  # noqa: BLE001 - report any loader error
        rep.check(False, "repository.yaml is valid", str(err))


def check_discovery(sv: SimpleNamespace, rep: Report) -> None:
    """Replicates StoreData._find_app_configs: every config.{json,yaml,yml} is an app."""
    ignored_trees = {".git", "__pycache__", "node_modules"}
    found = []
    for path in REPO.glob("**/config.*"):
        rel = path.relative_to(REPO)
        if any(p.startswith(".") or p == "rootfs" for p in rel.parts):
            continue
        if path.suffix not in sv.FILE_SUFFIX_CONFIGURATION:
            continue
        if set(rel.parts) & ignored_trees or rel.parts[:2] == ("e2e", "work"):
            continue  # not in git (see .gitignore), so the Supervisor never sees it
        found.append(rel.as_posix())
    rep.check(found == ["send_to_x3/config.yaml"], "the repository contains exactly one app", ", ".join(found))


def check_app_config(sv: SimpleNamespace, cap: _Capture, rep: Report) -> dict[str, Any] | None:
    import voluptuous as vol

    raw = sv.read_json_or_yaml_file(APP_DIR / "config.yaml")
    try:
        cfg = sv.SCHEMA_APP_CONFIG(deepcopy(raw))
    except vol.Invalid as err:
        rep.check(False, "config.yaml passes SCHEMA_APP_CONFIG", str(err))
        return None
    warnings = cap.take()
    rep.check(not warnings, "config.yaml passes SCHEMA_APP_CONFIG without advisories", "; ".join(warnings))
    dropped = sorted(set(raw) - set(cfg))
    rep.check(not dropped, "config.yaml has no keys the Supervisor ignores", ", ".join(dropped))
    rep.check(cfg["slug"] == APP_DIR.name, "slug matches the folder name", cfg["slug"])
    rep.check(cfg["init"] is False, "init: false (s6-overlay base image)")
    rep.check(
        cfg["ingress"] and cfg["ingress_port"] == 8765 and "8765/tcp" in cfg.get("ports", {}),
        "ingress and the LAN port share container port 8765",
    )
    rep.check("webui" not in cfg, "no webui (ingress provides Open Web UI)")
    rep.check(cfg.get("watchdog", "").endswith("[PORT:8765]/healthz"), "watchdog probes /healthz on 8765")
    rep.check(
        sorted(cfg["arch"]) == ["aarch64", "amd64"], "arch is aarch64 + amd64", ",".join(cfg["arch"])
    )
    rep.check("image" not in cfg, "image: is commented out (local build by default)")
    rep.check(
        cfg["backup"] == "cold" and not cfg.get("backup_pre") and not cfg.get("backup_post"),
        "cold backup (the WAL-mode SQLite database is copied while the app is stopped)",
        str(cfg["backup"]),
    )
    rep.check("port" not in cfg["schema"], "no port option (ingress and the watchdog are fixed at 8765)")
    return cfg


def check_translations(sv: SimpleNamespace, cfg: dict[str, Any], rep: Report) -> None:
    import voluptuous as vol

    tdir = APP_DIR / "translations"
    files = sorted(p for p in tdir.glob("*") if p.suffix in sv.FILE_SUFFIX_CONFIGURATION)
    rep.check(any(p.stem == "en" for p in files), "translations/en.yaml exists")
    for path in files:
        try:
            tr = sv.SCHEMA_APP_TRANSLATIONS(sv.read_json_or_yaml_file(path))
        except (vol.Invalid, Exception) as err:  # noqa: BLE001
            rep.check(False, f"{path.name} passes SCHEMA_APP_TRANSLATIONS", str(err))
            continue
        rep.check(True, f"{path.name} passes SCHEMA_APP_TRANSLATIONS")
        conf = tr.get("configuration", {})
        missing = sorted(set(cfg["schema"]) - set(conf))
        extra = sorted(set(conf) - set(cfg["schema"]))
        rep.check(not missing, f"{path.name} names every option", ", ".join(missing))
        rep.check(not extra, f"{path.name} has no stale options", ", ".join(extra))
        empty = sorted(k for k, v in conf.items() if not (v.get("description") or "").strip())
        rep.check(not empty, f"{path.name} describes every option", ", ".join(empty))
        ports = set(cfg.get("ports", {}))
        rep.check(set(tr.get("network", {})) == ports, f"{path.name} describes every port", ", ".join(sorted(ports)))


def _load_with_sendtox3(options: dict[str, Any]) -> Any:
    sys.path.insert(0, str(APP_DIR))
    from sendtox3.config import load_options

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "options.json"
        p.write_text(json.dumps(options), encoding="utf-8")
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("SENDTOX3_")}
        try:
            return load_options(p)
        finally:
            os.environ.update(saved)


def _as_loaded(key: str, value: Any) -> Any:
    """The value sendtox3.config.load_options should produce for a written option."""
    if key in ("inbox_folder", "push_folder"):
        return "/" + value.strip("/") if value.strip("/") else "/Inbox"
    if key == "allowed_senders":
        return [s.lower() for s in value if s.strip()]
    return value


def check_options(sv: SimpleNamespace, cfg: dict[str, Any], cap: _Capture, rep: Report) -> None:
    import voluptuous as vol

    coresys = SimpleNamespace(homeassistant=SimpleNamespace(secrets={}), hardware=None)
    defaults = cfg["options"]

    def validate(user: dict[str, Any]) -> dict[str, Any]:
        merged = sv.merger.merge(deepcopy(defaults), deepcopy(user))
        return sv.AppOptions(coresys, cfg["schema"], cfg["name"], cfg["slug"]).validate(merged)

    for label, user in valid_option_sets(defaults).items():
        try:
            written = validate(user)
        except vol.Invalid as err:
            rep.check(False, f"options accepted: {label}", str(err))
            continue
        warnings = cap.take()
        rep.check(not warnings, f"options accepted: {label}", "; ".join(warnings))
        rep.check(set(written) == set(cfg["schema"]), f"options.json has every option: {label}")
        try:
            opts = _load_with_sendtox3(written)
        except Exception as err:  # noqa: BLE001
            rep.check(False, f"sendtox3 loads the written options: {label}", repr(err))
            continue
        mismatched = [k for k, v in written.items() if getattr(opts, k) != _as_loaded(k, v)]
        rep.check(not mismatched, f"sendtox3 reads back every value: {label}", ", ".join(mismatched))

    for label, user in invalid_option_sets().items():
        try:
            validate(user)
        except vol.Invalid as err:
            rep.check(True, f"options rejected: {label}", str(err).splitlines()[0])
        else:
            rep.check(False, f"options rejected: {label}", "the Supervisor accepted it")
        cap.take()

    # An older version had a `port` option. A value saved then must be dropped on
    # upgrade (the Supervisor ignores options that left the schema), so the app
    # listens on 8765.
    label = "a port saved by an older version is dropped"
    try:
        written = validate({"port": 9000})
    except vol.Invalid as err:
        rep.check(False, label, str(err))
    else:
        rep.check("port" not in written and _load_with_sendtox3(written).port == 8765, label)
    cap.take()


def check_ui(sv: SimpleNamespace, cfg: dict[str, Any], rep: Report) -> None:
    ui = sv.UiOptions(SimpleNamespace())(cfg["schema"])
    names = [n["name"] for n in ui]
    rep.check(names == list(cfg["schema"]), "the configuration form renders every option", f"{len(names)} fields")
    secrets = sorted(n["name"] for n in ui if n.get("format") == "password")
    rep.check(
        secrets == ["admin_token", "device_token", "imap_password", "ingest_token"],
        "secrets render as password fields",
        ", ".join(secrets),
    )
    lists = [n["name"] for n in ui if n.get("multiple")]
    rep.check(lists == ["allowed_senders"], "allowed_senders renders as a list", ", ".join(lists))


def check_rating(sv: SimpleNamespace, cfg: dict[str, Any], rep: Report) -> None:
    apparmor = sv.SECURITY_DEFAULT
    if not cfg.get("apparmor", True):
        apparmor = sv.SECURITY_DISABLE
    elif (APP_DIR / "apparmor.txt").exists():
        apparmor = sv.SECURITY_PROFILE
    app = SimpleNamespace(
        apparmor=apparmor,
        with_ingress=cfg["ingress"],
        host_network=cfg["host_network"],
        data=cfg,
        access_auth_api=cfg["auth_api"],
        signed=False,
        privileged=cfg.get("privileged", []),
        with_kernel_modules=cfg["kernel_modules"],
        hassio_role=cfg["hassio_role"],
        host_pid=cfg["host_pid"],
        host_uts=cfg["host_uts"],
        access_docker_api=cfg["docker_api"],
        with_full_access=cfg["full_access"],
    )
    expected = EXPECTED_RATING + (1 if apparmor == sv.SECURITY_PROFILE else 0)
    rating = sv.rating_security(app)
    rep.check(rating == expected, "security rating", f"{rating} (expected {expected})")


def check_build_files(rep: Report) -> None:
    for name in ("Dockerfile", "run.sh", "requirements.txt", "README.md", "DOCS.md", "CHANGELOG.md",
                 "icon.png", "logo.png"):
        rep.check((APP_DIR / name).is_file(), f"send_to_x3/{name} exists")
    rep.check(not any(APP_DIR.glob("build.*")), "no deprecated build.yaml")
    rep.check(not any(APP_DIR.glob("Dockerfile.*")), "one Dockerfile for both architectures")


# -------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate send_to_x3 with the Supervisor's own schemas.")
    ap.add_argument(
        "--supervisor",
        type=Path,
        default=Path(os.environ.get("SUPERVISOR_SRC", REPO / "e2e" / "work" / "ha" / "supervisor")),
        help=f"home-assistant/supervisor checkout (tag {SUPERVISOR_TAG}); env SUPERVISOR_SRC",
    )
    args = ap.parse_args(argv)
    if not (args.supervisor / "supervisor" / "apps" / "validate.py").is_file():
        print(f"no Supervisor checkout at {args.supervisor}; run e2e/ha/validate_config.sh", file=sys.stderr)
        return 2

    cap = _Capture()
    logging.getLogger("supervisor").addHandler(cap)
    logging.getLogger("supervisor").setLevel(logging.DEBUG)
    logging.getLogger("supervisor").propagate = False

    sv = _import_supervisor(args.supervisor)
    print(f"Supervisor source: {args.supervisor} ({_version_of(args.supervisor)})")
    rep = Report()
    check_repository(sv, rep)
    check_discovery(sv, rep)
    check_build_files(rep)
    cfg = check_app_config(sv, cap, rep)
    if cfg is not None:
        check_translations(sv, cfg, rep)
        check_options(sv, cfg, cap, rep)
        check_ui(sv, cfg, rep)
        check_rating(sv, cfg, rep)

    print(f"\n{len(rep.passed)} passed, {len(rep.failed)} failed")
    for line in rep.failed:
        print(f"  FAILED: {line}")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
