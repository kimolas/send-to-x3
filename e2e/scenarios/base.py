"""Scenario plumbing: context, checks, registry, shared helpers."""

from __future__ import annotations

import io
import json
import time
import traceback
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from e2e.sim import runner
from e2e.sim.firmware import Firmware
from e2e.sim.runner import SimRun

from .backends import Backend, BackendConfig, make_backend
from .wiretap import WireTap


class SkipScenario(Exception):
    """Raised by a scenario that cannot run in this environment (reason in the message)."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    scenario: str
    firmware: str
    backend: str
    status: str = "pass"  # pass | fail | skip | error
    seconds: float = 0.0
    checks: list[Check] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def to_json(self) -> dict:
        return {
            "scenario": self.scenario, "firmware": self.firmware, "backend": self.backend, "status": self.status,
            "seconds": round(self.seconds, 2), "error": self.error, "notes": self.notes,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "runs": self.runs, "artifacts": self.artifacts,
        }


@dataclass
class Context:
    """Everything a scenario needs; one per (scenario, firmware) run."""

    fw: Firmware
    backend_kind: str
    out: Path
    result: Result
    python: str
    bundle_source: str = "auto"
    program_override: Optional[Path] = None
    docker_image: str = "send_to_x3:e2e"
    _cleanups: list[Callable[[], None]] = field(default_factory=list)

    # -- checks ---------------------------------------------------------------------------

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        self.result.checks.append(Check(name, bool(ok), detail if isinstance(detail, str) else json.dumps(detail,
                                                                                                         default=str)))
        return bool(ok)

    def note(self, msg: str) -> None:
        self.result.notes.append(msg)

    def artifact(self, name: str, path: Path | str) -> None:
        self.result.artifacts[name] = str(path)

    # -- resources ------------------------------------------------------------------------

    @property
    def program(self) -> Path:
        if self.program_override:
            return self.program_override
        try:
            return runner.find_program(self.fw.ref)
        except FileNotFoundError as e:
            raise SkipScenario(str(e)) from None

    def defer(self, fn: Callable[[], None]) -> None:
        self._cleanups.append(fn)

    def cleanup(self) -> None:
        while self._cleanups:
            fn = self._cleanups.pop()
            try:
                fn()
            except Exception:  # noqa: BLE001
                self.note("cleanup failed: " + traceback.format_exc(limit=1))

    def start_backend(self, name: str = "backend", *, device_host: str = "", **cfg_kw: Any) -> tuple[Backend, WireTap]:
        """Start a fresh backend behind a wire tap; both stop at scenario end."""
        port = runner.free_port()
        tap = WireTap(port, name=f"{name}-tap").start()
        self.defer(tap.stop)
        data = self.out / name
        cfg_kw.setdefault("docker_image", self.docker_image)
        cfg = BackendConfig(port=port, data_dir=data, public_url=tap.url, device_host=device_host,
                            log_path=data / "requests.jsonl", python=self.python, **cfg_kw)
        backend = make_backend(self.backend_kind, cfg)
        backend.start()
        self.defer(backend.stop)
        self.artifact(f"{name}_log", backend.stdout_path)
        return backend, tap

    def record_run(self, run: SimRun) -> SimRun:
        self.result.runs.append(run.summary())
        for shot, s in run.shots.items():
            if s.png:
                self.artifact(f"{run.name}/{shot}", s.png)
        return run

    def dump_tap(self, tap: WireTap, name: str) -> Path:
        p = self.out / f"{name}.json"
        p.write_text(json.dumps([e.summary() for e in tap.exchanges()], indent=2, default=str), encoding="utf-8")
        self.artifact(name, p)
        return p


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    fn: Callable[[Context], None]
    firmwares: tuple[str, ...]


REGISTRY: dict[str, Scenario] = {}


def scenario(id: str, title: str, *, firmwares: tuple[str, ...]) -> Callable[[Callable[[Context], None]], Callable]:
    def deco(fn: Callable[[Context], None]) -> Callable[[Context], None]:
        if id in REGISTRY:
            raise ValueError(f"duplicate scenario {id}")
        REGISTRY[id] = Scenario(id, title, fn, firmwares)
        return fn

    return deco


def execute(sc: Scenario, ctx: Context) -> Result:
    start = time.monotonic()
    try:
        sc.fn(ctx)
        ctx.result.status = "pass" if all(c.ok for c in ctx.result.checks) and ctx.result.checks else "fail"
        if not ctx.result.checks:
            ctx.result.error = "scenario recorded no checks"
    except SkipScenario as e:
        ctx.result.status, ctx.result.error = "skip", str(e)
    except Exception as e:  # noqa: BLE001 - one scenario's crash must not stop the suite
        ctx.result.status = "error"
        ctx.result.error = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=6)}"
    finally:
        ctx.cleanup()
        ctx.result.seconds = time.monotonic() - start
    return ctx.result


# ---------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------


def epub_info(data: bytes) -> dict:
    """Light structural facts about an EPUB (zip, mimetype first and stored, title)."""
    info: dict[str, Any] = {"bytes": len(data), "zip": False}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            first = z.infolist()[0] if names else None
            info.update(zip=True, entries=len(names), mimetype_first=bool(first and first.filename == "mimetype"),
                        mimetype_stored=bool(first and first.compress_type == zipfile.ZIP_STORED),
                        mimetype=z.read("mimetype").decode("ascii", "replace") if "mimetype" in names else None)
            opf = next((n for n in names if n.endswith(".opf")), None)
            if opf:
                import re

                text = z.read(opf).decode("utf-8", "replace")
                m = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", text, re.S)
                info["title"] = m.group(1) if m else None
                info["chapters"] = text.count("<itemref ")
    except zipfile.BadZipFile:
        pass
    return info


def valid_epub(data: bytes) -> bool:
    i = epub_info(data)
    return bool(i.get("zip") and i.get("mimetype_first") and i.get("mimetype_stored")
                and i.get("mimetype") == "application/epub+zip")


LONG_TEXT = "\n\n".join(
    f"{n}. " + p for n, p in enumerate([
        "An e-paper screen holds its image without power. Tiny capsules of pigment are pulled up or pushed down by "
        "an electric field, and once they settle they stay put until the next refresh, which is why a reader can "
        "show the same page for weeks on a single charge.",
        "That property shapes everything about reading on such a device. Pages turn with a short flash rather than "
        "an animation, text is crisp in direct sunlight, and there is no backlight shining into the reader’s eyes "
        "late at night — only reflected light, as with paper.",
        "Sending an article to the reader should feel just as calm. The phone hands the link to a small server at "
        "home; the server fetches the page, keeps only the words, and waits. When the reader next goes to sleep on "
        "the home network it quietly collects whatever is new.",
        "Typography matters more than one might expect. Paragraphs need room to breathe, headings should stand out "
        "without shouting, and long words deserve hyphenation so that justified lines do not open rivers of white "
        "space across the page. Café, naïve, façade and “smart quotes” must survive the trip intact.",
        "None of this needs images. A clean column of text is what the screen does best, and stripping pictures, "
        "advertisements and navigation keeps files small enough to arrive in seconds over a modest Wi-Fi link.",
    ] * 4, start=1)
)
"""About 1,000 words of original (CC0) prose with Latin-1 punctuation, for multi-page tests."""
