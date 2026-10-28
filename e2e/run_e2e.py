#!/usr/bin/env python3
"""Run the firmware end-to-end scenarios (see e2e/README.md).

    python e2e/run_e2e.py                          # every scenario, every built firmware, mock backend
    python e2e/run_e2e.py -s S1,S2 -f 2.0.0rc      # a subset
    python e2e/run_e2e.py -f releases,fork         # the releases plus the Send to X3 firmware
    python e2e/run_e2e.py --backend local          # the real app (python -m sendtox3)
    python e2e/run_e2e.py --list

Simulators come from ``e2e/sim/build_sim.sh <ref>``; ``--build`` runs it (and
``e2e/wire/build_wire.sh`` for W1) first, which rebuilds only what changed. A scenario
on a simulator built from other patches or another base commit than this checkout has
(``firmware/patches``, ``firmware/UPSTREAM``, ``e2e/sim/patches``) is an error, not a
pass. Results: a table on stdout and ``<out>/report.json`` (with each simulator's
build-info.json); every run keeps its SD card, serial log, screenshots (PNG) and the
recorded HTTP exchanges under ``<out>/<scenario>-<firmware>/``. Exit status 0 only when
nothing failed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from e2e.scenarios import REGISTRY, Context, Result, execute  # noqa: E402
from e2e.sim import runner  # noqa: E402
from e2e.sim.firmware import FIRMWARES, RELEASES, firmware  # noqa: E402

DEFAULT_PYTHON = os.environ.get("SENDTOX3_E2E_PYTHON", sys.executable)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="CrossPoint firmware e2e scenarios")
    ap.add_argument("-s", "--scenario", default="all", help="comma-separated ids (default: all)")
    ap.add_argument("-f", "--firmware", default="releases",
                    help=f"comma-separated refs ({', '.join(FIRMWARES)}) and/or 'releases' ({', '.join(RELEASES)}), "
                         "or 'all'")
    ap.add_argument("-b", "--backend", choices=("mock", "local", "docker"), default="mock")
    ap.add_argument("--bundle", choices=("auto", "app", "spec"), default="auto",
                    help="plugin bundle generator: the app's plugin_bundle, the §10 template, or auto")
    ap.add_argument("--python", default=DEFAULT_PYTHON, help="interpreter for the backend process")
    ap.add_argument("--docker-image", default="send_to_x3:e2e", help="image for --backend docker")
    ap.add_argument("--out", type=Path, default=None, help="results dir (default: $SENDTOX3_E2E_WORK/runs/<time>)")
    ap.add_argument("--build", action="store_true",
                    help="run build_sim.sh (and build_wire.sh for W1) first; each rebuilds only what changed")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--json", action="store_true", help="print the report JSON instead of the table")
    return ap.parse_args(argv)


def select(spec: str, known: list[str], what: str) -> list[str]:
    if spec == "all":
        return known
    picked: list[str] = []
    for x in (x.strip() for x in spec.split(",")):
        names = list(RELEASES) if x == "releases" and what == "firmware" else [x] if x else []
        picked += [y for y in names if y not in picked]
    unknown = [x for x in picked if x not in known]
    if unknown:
        raise SystemExit(f"unknown {what}: {', '.join(unknown)} (known: {', '.join(known)})")
    return picked


def ensure_built(scenarios: list[str], refs: list[str]) -> None:
    """Build what the selected scenarios run on. Always runs the scripts: their stamps
    make an up-to-date build a no-op, and a present binary may predate a patch edit."""
    for ref in refs:
        runs_on = [sid for sid in scenarios if ref in REGISTRY[sid].firmwares]
        if not runs_on:
            continue
        subprocess.run(["bash", str(REPO / "e2e" / "sim" / "build_sim.sh"), ref], check=True)
        if "W1" in runs_on:
            subprocess.run(["bash", str(REPO / "e2e" / "wire" / "build_wire.sh"), ref], check=True)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    if a.list:
        for sc in REGISTRY.values():
            print(f"{sc.id:4} {sc.title}  [{', '.join(sc.firmwares)}]")
        return 0
    scenarios = select(a.scenario, list(REGISTRY), "scenario")
    refs = select(a.firmware, list(FIRMWARES), "firmware")
    if a.build:
        ensure_built(scenarios, refs)
    out = (a.out or runner.work_dir() / "runs" / time.strftime("%Y%m%d-%H%M%S")).resolve()
    out.mkdir(parents=True, exist_ok=True)

    results: list[Result] = []
    for sid in scenarios:
        sc = REGISTRY[sid]
        for ref in refs:
            if ref not in sc.firmwares:
                continue
            res = Result(scenario=sid, firmware=ref, backend=a.backend, build=runner.build_info(ref))
            ctx = Context(fw=firmware(ref), backend_kind=a.backend, out=out / f"{sid}-{ref}", result=res,
                          python=a.python, bundle_source=a.bundle, docker_image=a.docker_image)
            ctx.out.mkdir(parents=True, exist_ok=True)
            print(f"[e2e] {sid} on {ref} ({a.backend}) ...", file=sys.stderr, flush=True)
            results.append(execute(sc, ctx))
            r = results[-1]
            print(f"[e2e] {sid} on {ref}: {r.status.upper()} in {r.seconds:.1f}s", file=sys.stderr, flush=True)

    report = {"out": str(out), "backend": a.backend, "results": [r.to_json() for r in results]}
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if a.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_table(results, out)
    return 0 if all(r.status in ("pass", "skip") for r in results) and results else 1


def print_table(results: list[Result], out: Path) -> None:
    for r in results:
        passed = sum(c.ok for c in r.checks)
        print(f"{r.scenario:4} {r.firmware:8} {r.status.upper():5} {r.seconds:6.1f}s  {passed}/{len(r.checks)} checks")
        for c in r.checks:
            if not c.ok:
                print(f"       FAIL {c.name}: {c.detail[:400]}")
        if r.error:
            print("       " + r.error.strip().replace("\n", "\n       ")[:2000])
    print(f"report: {out / 'report.json'}")


if __name__ == "__main__":
    sys.exit(main())
