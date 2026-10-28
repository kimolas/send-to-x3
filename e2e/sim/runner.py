"""Run the headless CrossPoint simulator once, with scripted input and screenshots.

The simulator reads its whole input schedule at startup (``CROSSPOINT_SIM_INPUT_SCRIPT``,
``<ms>:<KEY>[:<hold ms>]`` separated by ``;``, times from process start) and saves BMP
screenshots at fixed times (``CROSSPOINT_SIM_SCREENSHOTS``, ``<ms>:<path>``). Input is
therefore open loop: :class:`InputScript` builds the schedule with generous gaps, and
the scenarios verify the outcome afterwards from the serial log (activity
transitions, firmware log lines), the SD card and the backend's request log, never
from screen text.

While the simulator runs, :func:`run_sim` streams its serial output, fires
:class:`Watch` callbacks on matching lines (e.g. push files once the web server is
up) and can stop the run early once a condition holds.

Environment the simulator understands (crosspoint-simulator README and src/):
``CROSSPOINT_SIM_SD``, ``CROSSPOINT_SIM_HTTP_PORT`` (web server; WebSocket = port+1),
``CROSSPOINT_SIM_INPUT_SCRIPT[_AFTER_WAKE]``, ``CROSSPOINT_SIM_SCREENSHOTS[_AFTER_WAKE]``,
``CROSSPOINT_SIM_WIFI_CONNECT=fail|no-ssid|timeout``, ``CROSSPOINT_SIM_WIFI_NETWORKS``,
``CROSSPOINT_SIM_FREE_HEAP``, ``CROSSPOINT_SIM_MAX_ALLOC_HEAP``. Never set
``CROSSPOINT_SIM_HTTP_MOCK_ROOT``: it answers any URL whose basename matches a local file.

Software restarts: upstream, ``ESP.restart()`` is a no-op, so the firmware's silent
restart continues in the same process. Simulators built with
``e2e/sim/patches/simulator/0001-real-software-restart.patch`` (the ``fork`` build)
relaunch instead when ``CROSSPOINT_SIM_RESTART=exec`` (``run_sim(restart="exec")``):
they keep the RTC_NOINIT variables, print ``[SIM] software restart: relaunching``,
``execvp`` themselves (same pid and serial pipe) and run
``CROSSPOINT_SIM_INPUT_SCRIPT_AFTER_RESTART`` / ``..._SCREENSHOTS_AFTER_RESTART``
(``after_restart``), timed from the relaunch, never the old schedule.
:meth:`SimRun.boots` splits the serial log at each relaunch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Pattern, Union

from . import images

KEYS = {"BACK", "ENTER", "LEFT", "RIGHT", "UP", "DOWN", "POWER", "SLEEP", "HOME"}
REPO = Path(__file__).resolve().parents[2]
DEFAULT_WORK = Path(os.environ.get("SENDTOX3_E2E_WORK", Path.home() / ".cache" / "sendtox3-e2e"))

_ACTIVITY_RE = re.compile(r"\[ACT\] Entering activity: (\S+)")
_RELAUNCH_RE = re.compile(r"^\[SIM\] software restart: relaunching.*$", re.M)
_SILENT_RESTART_RE = re.compile(r"Silent restart \(target=([\w-]+)\)")
_LOG_RE = re.compile(r"^\[(\d+)\] \[(DBG|INF|ERR|WRN)\] \[([^\]]+)\] (.*)$")


def work_dir() -> Path:
    return Path(os.environ.get("SENDTOX3_E2E_WORK", DEFAULT_WORK))


def find_program(ref: str) -> Path:
    """The simulator binary built by ``e2e/sim/build_sim.sh <ref>``."""
    p = work_dir() / "bin" / ref.replace("/", "_") / "program"
    if not p.is_file() or not os.access(p, os.X_OK):
        raise FileNotFoundError(f"no simulator for {ref} at {p}; run: e2e/sim/build_sim.sh {ref}")
    return p


# ---------------------------------------------------------------------------------------
# Build provenance
# ---------------------------------------------------------------------------------------


class StaleBuild(RuntimeError):
    """The simulator for a ref was built from other inputs than this checkout has now."""


def build_info(ref: str) -> dict:
    """The ``build-info.json`` that ``build_sim.sh <ref>`` wrote next to the binary
    (commits, patch blob ids, simulator worktree, build time); {} when there is none."""
    try:
        return json.loads((work_dir() / "bin" / ref.replace("/", "_") / "build-info.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _blob(p: Path) -> str:
    """``git hash-object`` of a file: the id build_sim.sh records for each patch."""
    data = p.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def expected_build(ref: str) -> dict:
    """What ``build_sim.sh <ref>`` builds from in this checkout: develop's pin and the
    fork's base (``firmware/UPSTREAM``), and the default patch sets (the fork's
    ``firmware/patches/`` and ``e2e/sim/patches/simulator/``; none for other refs)."""
    want: dict = {"firmware_patches": [], "simulator_patches": []}
    if ref == "develop":
        m = re.search(r'^DEVELOP_SHA="([0-9a-f]{40})"$', (REPO / "e2e" / "sim" / "build_sim.sh").read_text("utf-8"),
                      re.M)
        want["firmware_commit"] = m.group(1) if m else ""
    elif ref == "fork":
        upstream = (REPO / "firmware" / "UPSTREAM").read_text("utf-8")
        want["firmware_commit"] = next((ln.split("=", 1)[1].strip() for ln in upstream.splitlines()
                                        if ln.startswith("COMMIT=")), "")
        want["firmware_patches"] = [_blob(p) for p in sorted((REPO / "firmware" / "patches").glob("*.patch"))]
        want["simulator_patches"] = [_blob(p) for p in sorted((REPO / "e2e" / "sim" / "patches" / "simulator")
                                                              .glob("*.patch"))]
    return want


def stale_build(ref: str) -> list[str]:
    """Why the binary for ``ref`` is not what ``build_sim.sh <ref>`` would build from this
    checkout now; empty when it is. (The script's own stamp also covers the simulator
    pin and the script itself; the patches and base commit are what change often.)"""
    info = build_info(ref)
    if not info:
        return ["no build-info.json"]
    def ids(blobs: list[str]) -> str:
        return ",".join(b[:8] for b in blobs) or "none"

    want = expected_build(ref)
    why = []
    if "firmware_commit" in want and info.get("firmware_commit") != want["firmware_commit"]:
        why.append(f"built from firmware {str(info.get('firmware_commit'))[:12]}, "
                   f"expected {want['firmware_commit'][:12]}")
    for key, where in (("firmware_patches", "firmware/patches"), ("simulator_patches", "e2e/sim/patches/simulator")):
        have = [str(p.get("blob")) for p in info.get(key) or []]
        if have != want[key]:
            why.append(f"{key.replace('_', ' ')} differ from {where} (built {ids(have)}, now {ids(want[key])})")
    return why


def check_build(ref: str) -> None:
    """Raise :class:`StaleBuild` when the binary for ``ref`` is out of date."""
    why = stale_build(ref)
    if why:
        raise StaleBuild(f"stale {ref} simulator: {'; '.join(why)}. "
                         f"Run e2e/sim/build_sim.sh {ref}, or pass --build to run_e2e.py")


def free_port(pair: bool = False) -> int:
    """A free loopback TCP port (and, with ``pair``, the next one free too)."""
    for _ in range(200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if not pair:
            return port
        if port < 65535 and _port_free(port) and _port_free(port + 1):
            return port
    raise RuntimeError("no free loopback port pair")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# ---------------------------------------------------------------------------------------
# Input schedule
# ---------------------------------------------------------------------------------------


@dataclass
class ShotSpec:
    name: str
    at_ms: int


class InputScript:
    """Builds ``CROSSPOINT_SIM_INPUT_SCRIPT`` with a moving cursor.

    Every :meth:`press` schedules key-down at the cursor, key-up ``hold_ms`` later, then
    advances the cursor by ``gap_ms`` (default) or ``wait_ms``. Presses closer than the
    firmware's frame time could merge (the simulator latches one edge per button per
    frame), so the default gap is several frames even when the firmware is busy.
    """

    def __init__(self, start_ms: int = 2500, gap_ms: Optional[int] = None) -> None:
        if gap_ms is None:
            gap_ms = int(450 * float(os.environ.get("SENDTOX3_E2E_SLOWDOWN", "1") or 1))
        if start_ms < 0 or gap_ms < 120:
            raise ValueError("start_ms must be >= 0 and gap_ms >= 120")
        self.t = start_ms
        self.gap_ms = gap_ms
        self.events: list[tuple[int, str]] = []
        self.shots: list[ShotSpec] = []
        self.marks: list[tuple[int, str]] = []
        self.quit_at: Optional[int] = None

    def press(self, key: str, *, hold_ms: Optional[int] = None, wait_ms: Optional[int] = None) -> "InputScript":
        key = key.upper()
        if key not in KEYS:
            raise ValueError(f"unknown key {key!r}; known: {sorted(KEYS)}")
        self._check_open()
        spec = f"{self.t}:{key}" + (f":{int(hold_ms)}" if hold_ms is not None else "")
        self.events.append((self.t, spec))
        held = hold_ms if hold_ms is not None else 80
        self.t += max(held + 120, self.gap_ms if wait_ms is None else int(wait_ms))
        return self

    def presses(self, key: str, count: int, *, wait_ms: Optional[int] = None) -> "InputScript":
        for _ in range(count):
            self.press(key)
        if wait_ms is not None:
            self.wait(wait_ms)
        return self

    def hold(self, key: str, ms: int, *, wait_ms: Optional[int] = None) -> "InputScript":
        return self.press(key, hold_ms=ms, wait_ms=wait_ms)

    def wait(self, ms: int) -> "InputScript":
        self._check_open()
        self.t += int(ms)
        return self

    def shot(self, name: str, *, wait_ms: int = 0) -> "InputScript":
        """Screenshot at the cursor (optionally after ``wait_ms``); names must be unique."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError(f"screenshot name must be [A-Za-z0-9_.-]+: {name!r}")
        if any(s.name == name for s in self.shots):
            raise ValueError(f"duplicate screenshot name {name!r}")
        self.wait(wait_ms)
        self.shots.append(ShotSpec(name, self.t))
        self.t += 50
        return self

    def mark(self, label: str) -> "InputScript":
        self.marks.append((self.t, label))
        return self

    def quit(self, *, wait_ms: int = 500) -> "InputScript":
        self.wait(wait_ms)
        self.events.append((self.t, f"{self.t}:QUIT"))
        self.quit_at = self.t
        return self

    @property
    def end_ms(self) -> int:
        return self.quit_at if self.quit_at is not None else self.t

    def input_spec(self) -> str:
        return ";".join(spec for _, spec in sorted(self.events, key=lambda e: e[0]))

    def screenshot_spec(self, shot_dir: Path) -> str:
        for s in self.shots:
            p = str(shot_dir / f"{s.name}.bmp")
            if ";" in p:
                raise ValueError(f"screenshot path cannot contain ';': {p}")
        return ";".join(f"{s.at_ms}:{shot_dir / (s.name + '.bmp')}" for s in self.shots)

    def _check_open(self) -> None:
        if self.quit_at is not None:
            raise RuntimeError("script already ends with QUIT")


# ---------------------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------------------


@dataclass
class Watch:
    """Run ``action(line)`` (in a worker thread) when a serial line matches ``pattern``."""

    pattern: Union[str, Pattern[str]]
    action: Callable[[str], None]
    once: bool = True
    fired: int = 0
    errors: list[str] = field(default_factory=list)

    def matches(self, line: str) -> bool:
        pat = self.pattern
        return bool(pat.search(line)) if hasattr(pat, "search") else pat in line  # type: ignore[union-attr]


@dataclass
class Shot:
    name: str
    at_ms: int
    bmp: Path
    png: Optional[Path] = None
    ink: Optional[float] = None

    @property
    def taken(self) -> bool:
        return self.bmp.is_file()

    def gray(self) -> images.Gray:
        return images.read_bmp(self.bmp)


@dataclass
class SimRun:
    name: str
    program: Path
    out_dir: Path
    sd_root: Path
    returncode: Optional[int]
    seconds: float
    timed_out: bool
    stopped_early: bool
    serial_path: Path
    shots: dict[str, Shot]
    env: dict[str, str]
    watch_errors: list[str] = field(default_factory=list)
    # Wall-clock time (time.time(), as WireTap exchanges) of each relaunch line, to order
    # the process's restarts against the HTTP requests it made.
    relaunched_at: list[float] = field(default_factory=list)

    @property
    def log(self) -> str:
        return self.serial_path.read_text(encoding="utf-8", errors="replace") if self.serial_path.exists() else ""

    def lines(self, tag: Optional[str] = None, level: Optional[str] = None) -> list[str]:
        out = []
        for line in self.log.splitlines():
            m = _LOG_RE.match(line)
            if tag is not None or level is not None:
                if not m or (tag is not None and m.group(3) != tag) or (level is not None and m.group(2) != level):
                    continue
            out.append(line)
        return out

    def boots(self) -> list[str]:
        """The serial log split at each software-restart relaunch (``restart="exec"``):
        one entry per process image, the first being the initial boot."""
        return _RELAUNCH_RE.split(self.log)

    def _text(self, boot: Optional[int]) -> str:
        if boot is None:
            return self.log
        boots = self.boots()
        return boots[boot] if -len(boots) <= boot < len(boots) else ""

    def activities(self, boot: Optional[int] = None) -> list[str]:
        """Activities entered, in order (``Activity::onEnter`` logs every one); with
        ``boot``, only those of that boot (an index into :meth:`boots`)."""
        return _ACTIVITY_RE.findall(self._text(boot))

    def entered(self, name: str, boot: Optional[int] = None) -> bool:
        return name in self.activities(boot)

    def silent_restarts(self, boot: Optional[int] = None) -> list[str]:
        """Targets of the firmware's silent restarts, in order (``main.cpp``
        ``silentRestartTo``: ``[MAIN] Silent restart (target=reader)``)."""
        return _SILENT_RESTART_RE.findall(self._text(boot))

    def find(self, pattern: str) -> list[re.Match[str]]:
        return list(re.finditer(pattern, self.log, re.M))

    def errors(self, ignore: Iterable[str] = ()) -> list[str]:
        ign = list(ignore)
        return [ln for ln in self.lines(level="ERR") if not any(i in ln for i in ign)]

    def shot_png(self, name: str) -> Optional[Path]:
        s = self.shots.get(name)
        return s.png if s and s.png else None

    def summary(self) -> dict:
        return {
            "name": self.name,
            "returncode": self.returncode,
            "seconds": round(self.seconds, 2),
            "timed_out": self.timed_out,
            "stopped_early": self.stopped_early,
            "serial_log": str(self.serial_path),
            "activities": self.activities(),
            "boots": len(self.boots()),
            "screenshots": {k: str(v.png or v.bmp) for k, v in self.shots.items() if v.taken},
            "watch_errors": self.watch_errors,
        }


def run_sim(
    program: Union[Path, str],
    sd_root: Union[Path, str],
    script: InputScript,
    out_dir: Union[Path, str],
    *,
    name: str = "run",
    http_port: Optional[int] = None,
    env: Optional[Mapping[str, str]] = None,
    watches: Iterable[Watch] = (),
    stop_when: Optional[Callable[[], bool]] = None,
    stop_grace_ms: int = 800,
    timeout_s: Optional[float] = None,
    after_wake: Optional[InputScript] = None,
    restart: Optional[str] = None,
    after_restart: Optional[InputScript] = None,
    png: bool = True,
) -> SimRun:
    """Run the simulator once and collect its serial log and screenshots.

    ``restart="exec"`` makes the firmware's software restarts relaunch the process
    (simulators with the restart patch only; others ignore it), and ``after_restart``
    is the input and screenshot schedule of the relaunched process. A run without a
    restart never uses it, so give the main script its own QUIT.

    ``timeout_s`` defaults to the end of every script given plus 20 s. ``stop_when``
    ends the run ``stop_grace_ms`` after it first returns true once every scheduled
    screenshot (after-wake and after-restart ones included) has been saved. On timeout
    or early stop the process (and anything it spawned) gets SIGTERM, then SIGKILL
    after 3 s.
    """
    program = Path(program).resolve()
    sd_root = Path(sd_root).resolve()
    out_dir = Path(out_dir).resolve()
    shot_dir = out_dir / "shots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    for old in shot_dir.glob("*.bmp"):
        old.unlink()
    serial_path = out_dir / "serial.log"

    if restart not in (None, "exec"):
        raise ValueError(f"restart must be None or 'exec', not {restart!r}")
    if after_restart is not None and restart is None:
        raise ValueError("after_restart needs restart='exec' (otherwise ESP.restart() is a no-op)")
    schedules = [script] + [s for s in (after_wake, after_restart) if s is not None]
    all_shots = [sh for s in schedules for sh in s.shots]
    names = [sh.name for sh in all_shots]
    if len(set(names)) != len(names):
        raise ValueError(f"screenshot names must be unique across the schedules: {names}")

    run_env = {k: v for k, v in os.environ.items() if not k.startswith("CROSSPOINT_")}
    run_env.update({
        "SDL_VIDEODRIVER": run_env.get("SDL_VIDEODRIVER", "dummy"),
        "SDL_AUDIODRIVER": "dummy",
        "CROSSPOINT_SIM_SD": str(sd_root),
        "CROSSPOINT_SIM_HTTP_PORT": str(http_port or free_port(pair=True)),
        "CROSSPOINT_SIM_INPUT_SCRIPT": script.input_spec(),
        "CROSSPOINT_SIM_SCREENSHOTS": script.screenshot_spec(shot_dir),
    })
    if after_wake is not None:
        run_env["CROSSPOINT_SIM_INPUT_SCRIPT_AFTER_WAKE"] = after_wake.input_spec()
        run_env["CROSSPOINT_SIM_SCREENSHOTS_AFTER_WAKE"] = after_wake.screenshot_spec(shot_dir)
    if restart is not None:
        run_env["CROSSPOINT_SIM_RESTART"] = restart
    if after_restart is not None:
        run_env["CROSSPOINT_SIM_INPUT_SCRIPT_AFTER_RESTART"] = after_restart.input_spec()
        run_env["CROSSPOINT_SIM_SCREENSHOTS_AFTER_RESTART"] = after_restart.screenshot_spec(shot_dir)
    if env:
        run_env.update(env)
    if run_env.get("CROSSPOINT_SIM_HTTP_MOCK_ROOT"):
        raise ValueError("CROSSPOINT_SIM_HTTP_MOCK_ROOT would bypass the backend under test")

    limit = timeout_s if timeout_s is not None else sum(s.end_ms for s in schedules) / 1000 + 20
    watch_list = list(watches)
    watch_threads: list[threading.Thread] = []
    watch_errors: list[str] = []
    relaunched_at: list[float] = []

    def fire(w: Watch, line: str) -> None:
        try:
            w.action(line)
        except Exception as e:  # noqa: BLE001 - reported in the run summary
            msg = f"watch {w.pattern!r} failed: {type(e).__name__}: {e}"
            w.errors.append(msg)
            watch_errors.append(msg)

    start = time.monotonic()
    with serial_path.open("w", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            [str(program)], cwd=out_dir, env=run_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )

        def pump() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if _RELAUNCH_RE.match(line):
                    relaunched_at.append(time.time())
                log_f.write(line + "\n")
                log_f.flush()
                for w in watch_list:
                    if (w.once and w.fired) or not w.matches(line):
                        continue
                    w.fired += 1
                    t = threading.Thread(target=fire, args=(w, line), daemon=True)
                    t.start()
                    watch_threads.append(t)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()

        timed_out = stopped_early = False
        condition_since: Optional[float] = None
        while proc.poll() is None:
            now = time.monotonic()
            if now - start > limit:
                timed_out = True
                break
            if stop_when is not None:
                try:
                    hit = stop_when()
                except Exception as e:  # noqa: BLE001
                    watch_errors.append(f"stop_when failed: {type(e).__name__}: {e}")
                    hit = False
                # Never cut a run short before its scheduled screenshots exist.
                if hit and condition_since is None and all(
                        (shot_dir / f"{sh.name}.bmp").exists() for sh in all_shots):
                    condition_since = now
                if condition_since is not None and (now - condition_since) * 1000 >= stop_grace_ms:
                    stopped_early = True
                    break
            time.sleep(0.05)
        if proc.poll() is None:
            _terminate(proc)
        reader.join(timeout=5)
    for t in watch_threads:
        t.join(timeout=30)
    seconds = time.monotonic() - start

    shots: dict[str, Shot] = {}
    for spec in all_shots:
        shot = Shot(spec.name, spec.at_ms, shot_dir / f"{spec.name}.bmp")
        if shot.taken:
            try:
                g = shot.gray()
                shot.ink = round(g.ink_ratio(), 4)
                if png:
                    shot.png = images.write_png(g, shot.bmp.with_suffix(".png"))
            except ValueError as e:
                watch_errors.append(f"screenshot {spec.name}: {e}")
        shots[spec.name] = shot

    return SimRun(name=name, program=program, out_dir=out_dir, sd_root=sd_root, returncode=proc.returncode,
                  seconds=seconds, timed_out=timed_out, stopped_early=stopped_early, serial_path=serial_path,
                  shots=shots, env={k: v for k, v in run_env.items() if k.startswith("CROSSPOINT_")},
                  watch_errors=watch_errors, relaunched_at=relaunched_at)


def _terminate(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue
