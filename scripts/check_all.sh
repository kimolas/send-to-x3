#!/usr/bin/env bash
# Runs every verification level in order (see docs/TESTING.md):
#   1. unit + integration tests                (always)
#   2. docker tests: image build/smoke, epubcheck, GreenMail IMAP, Android import  (--docker)
#   3. firmware host harnesses: CrossPoint's EPUB/TXT/OPDS/filename code         (--firmware)
#   4. firmware simulator e2e (S1-S5, W1) against the real app, local + docker    (--e2e)
#   5. Home Assistant Supervisor schema validation                               (--supervisor)
# --all = everything. Work files go to $SENDTOX3_E2E_WORK (default ~/.cache/sendtox3-e2e),
# which must not contain spaces and should not be inside a synced folder.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
PY=${PYTHON:-python3}
WORK=${SENDTOX3_E2E_WORK:-$HOME/.cache/sendtox3-e2e}
export SENDTOX3_E2E_WORK=$WORK
export FWH_WORK=${FWH_WORK:-$WORK/fwharness}

DOCKER=0 FIRMWARE=0 E2E=0 SUPERVISOR=0
for arg in "$@"; do
  case "$arg" in
    --docker) DOCKER=1 ;;
    --firmware) FIRMWARE=1 ;;
    --e2e) E2E=1 ;;
    --supervisor) SUPERVISOR=1 ;;
    --all) DOCKER=1 FIRMWARE=1 E2E=1 SUPERVISOR=1 ;;
    -h|--help) sed -n 2,10p "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

stage() { printf '\n==== %s ====\n' "$*"; }

stage "1. unit + integration tests"
"$PY" -m pytest -q -p no:cacheprovider

if [ "$FIRMWARE" = 1 ] || [ "$E2E" = 1 ]; then
  stage "building CrossPoint simulators / firmware checkouts (2.0.0rc, 1.6.5, develop 3f387a9)"
  e2e/sim/build_sim.sh 2.0.0rc
  e2e/sim/build_sim.sh 1.6.5
  e2e/sim/build_sim.sh 3f387a9
  e2e/wire/build_wire.sh 2.0.0rc
fi

if [ "$DOCKER" = 1 ]; then
  stage "2. docker tests"
  # The Android import test is marked docker + network (it fetches HTTP Shortcuts'
  # source on the first run), so network tests are enabled here too; -m docker keeps
  # the network-only tests out of this stage.
  SENDTOX3_DOCKER_TESTS=1 SENDTOX3_NETWORK_TESTS=1 "$PY" -m pytest -q -p no:cacheprovider -m docker
fi

if [ "$FIRMWARE" = 1 ]; then
  stage "3. firmware host harnesses"
  CP_REPO="$WORK/fw/1.6.5:$WORK/fw/2.0.0rc:$WORK/fw/3f387a9"
  OPDS_BIN=$(CP_REPO="$WORK/fw/1.6.5" e2e/fwharness/opds/build.sh | tail -1)
  SENDTOX3_FIRMWARE_TESTS=1 CP_REPO="$CP_REPO" SENDTOX3_OPDS_HARNESS="$OPDS_BIN" \
    "$PY" -m pytest -q -p no:cacheprovider tests/test_render_validation.py tests/test_opds.py \
      tests/integration/test_flow_share_to_x3.py
fi

if [ "$E2E" = 1 ]; then
  stage "4a. simulator e2e, real app (python -m sendtox3)"
  "$PY" e2e/run_e2e.py -f all -b local
  stage "4b. simulator e2e, real app image (docker)"
  docker build -q -t send_to_x3:e2e send_to_x3 >/dev/null
  "$PY" e2e/run_e2e.py -f releases -b docker
fi

if [ "$SUPERVISOR" = 1 ]; then
  stage "5. Home Assistant Supervisor schema validation"
  WORK_DIR="$WORK/ha" PYTHON="$PY" e2e/ha/validate_config.sh
fi

stage "all requested checks passed"
