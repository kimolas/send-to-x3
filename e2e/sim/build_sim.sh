#!/usr/bin/env bash
# Build the CrossPoint firmware as a native, headless X3 simulator.
#
#   e2e/sim/build_sim.sh [options] <crosspoint-ref>
#
#   <crosspoint-ref>     a tag/branch/commit of crosspoint-reader, e.g. 2.0.0rc or 1.6.5
#   --sim-ref <rev>      crosspoint-simulator revision (default: pinned per firmware ref below)
#   --env <pio-env>      PlatformIO env to build (default: simulator_x3)
#   --force              rebuild even if an up-to-date binary exists
#   --offline            do not fetch from GitHub (use the clones already in the work dir)
#
# Everything lives in a work dir OUTSIDE the repo:
#   SENDTOX3_E2E_WORK    default $HOME/.cache/sendtox3-e2e (must not contain spaces:
#                        PlatformIO's symlink:// lib_deps cannot express them)
#   $WORK/git/           blobless clone of crosspoint-reader + clone of crosspoint-simulator
#   $WORK/fw/<ref>/      firmware worktree (detached at <ref>, submodules initialised)
#   $WORK/simulator/<rev>/  simulator worktree
#   $WORK/venv/          python venv with PlatformIO
#   $WORK/pio-core/      PLATFORMIO_CORE_DIR (native platform + tool cache)
#   $WORK/bin/<ref>/program + build-info.json   the result
#   $WORK/logs/build-<ref>.log
#
# The last line printed is `SIM_BIN=<path to program>`.
#
# The firmware checkout is never committed to. The build adds an untracked
# platformio.local.ini (picked up by the firmware's own `extra_configs`), and the
# simulator's library build hook applies its documented idempotent source patches
# (BookMetadataCache size_t->uint32_t, GfxRenderer::setOrientation notification).
#
# Requirements: git, curl, a C++20 clang/gcc, SDL2 (`sdl2-config`; macOS: brew install
# sdl2 or sdl2-compat), python3 >= 3.9. Linux additionally needs libssl-dev.
set -euo pipefail

FIRMWARE_URL="${CROSSPOINT_REPO_URL:-https://github.com/crosspoint-reader/crosspoint-reader.git}"
SIMULATOR_URL="${CROSSPOINT_SIM_REPO_URL:-https://github.com/crosspoint-reader/crosspoint-simulator.git}"
PIO_VERSION="${SENDTOX3_PIO_VERSION:-6.2.0}"
WORK="${SENDTOX3_E2E_WORK:-$HOME/.cache/sendtox3-e2e}"
PY="${PYTHON:-python3}"

FW_REF=""
SIM_REF=""
PIO_ENV="simulator_x3"
FORCE=0
OFFLINE=0

die() { echo "build_sim: $*" >&2; exit 1; }
log() { echo "build_sim: $*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --sim-ref) SIM_REF="${2:?--sim-ref needs a value}"; shift 2 ;;
    --env) PIO_ENV="${2:?--env needs a value}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --offline) OFFLINE=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) [ -z "$FW_REF" ] || die "only one <crosspoint-ref> allowed"; FW_REF="$1"; shift ;;
  esac
done
[ -n "$FW_REF" ] || die "usage: build_sim.sh [--sim-ref REV] [--env ENV] [--force] <crosspoint-ref>"

# Simulator revisions verified to build and boot each firmware release on the X3.
# The simulator has no tags; it tracks firmware develop, so pick the newest commit
# whose HAL shims still match the release (see e2e/README.md for how these were chosen).
if [ -z "$SIM_REF" ]; then
  case "$FW_REF" in
    2.0.0rc) SIM_REF="1777fe2f1da1405cf5ecf191b831734e8862a92f" ;;
    3f387a9*) SIM_REF="1777fe2f1da1405cf5ecf191b831734e8862a92f" ;;
    1.6.5)   SIM_REF="9d963b6" ;;
    *)       SIM_REF="main" ;;
  esac
fi

case "$WORK" in *" "*) die "SENDTOX3_E2E_WORK must not contain spaces: $WORK" ;; esac
case "$FW_REF$SIM_REF" in *[!A-Za-z0-9._/-]*) die "refs may only contain [A-Za-z0-9._/-]" ;; esac
SAFE_REF="$(printf '%s' "$FW_REF" | tr '/' '_')"

for tool in git curl perl; do command -v "$tool" >/dev/null || die "$tool not found"; done
command -v sdl2-config >/dev/null || die "sdl2-config not found (macOS: brew install sdl2; Debian: apt install libsdl2-dev)"
command -v "$PY" >/dev/null || die "$PY not found (set PYTHON=)"

mkdir -p "$WORK/git" "$WORK/fw" "$WORK/simulator" "$WORK/bin" "$WORK/logs"
LOG="$WORK/logs/build-$SAFE_REF.log"

# --- macOS toolchain quirk ---------------------------------------------------------
# Some Command Line Tools installs carry a stale /Library/Developer/CommandLineTools/
# usr/include/c++/v1 that contains only __cxx_version. It shadows the SDK's libc++
# headers and every C++ compile fails with "'cassert' file not found". Point the
# compiler at the SDK's headers instead (reinstalling the CLT also fixes it).
if [ "$(uname -s)" = "Darwin" ]; then
  CLT_CXX=/Library/Developer/CommandLineTools/usr/include/c++/v1
  if [ -d "$CLT_CXX" ] && [ ! -e "$CLT_CXX/cassert" ] && [ -z "${CPLUS_INCLUDE_PATH:-}" ]; then
    SDK_PATH="$(xcrun --show-sdk-path 2>/dev/null || true)"
    [ -n "$SDK_PATH" ] && [ -e "$SDK_PATH/usr/include/c++/v1/cassert" ] \
      || die "stale $CLT_CXX and no SDK libc++ headers; reinstall the Command Line Tools"
    export CPLUS_INCLUDE_PATH="$SDK_PATH/usr/include/c++/v1"
    log "macOS: stale CLT libc++ headers, using CPLUS_INCLUDE_PATH=$CPLUS_INCLUDE_PATH"
  fi
fi

# --- PlatformIO in a private venv --------------------------------------------------
if [ ! -x "$WORK/venv/bin/pio" ] || ! "$WORK/venv/bin/pio" --version 2>/dev/null | grep -q "version $PIO_VERSION\$"; then
  log "creating venv with platformio==$PIO_VERSION"
  "$PY" -m venv "$WORK/venv"
  "$WORK/venv/bin/python" -m pip install -q --upgrade pip
  "$WORK/venv/bin/python" -m pip install -q "platformio==$PIO_VERSION"
fi
export PATH="$WORK/venv/bin:$PATH"
export PLATFORMIO_CORE_DIR="$WORK/pio-core"

# --- sources -----------------------------------------------------------------------
FW_GIT="$WORK/git/crosspoint-reader.git"
SIM_GIT="$WORK/git/crosspoint-simulator"
if [ ! -d "$FW_GIT" ]; then
  [ "$OFFLINE" = 0 ] || die "--offline but $FW_GIT does not exist"
  log "cloning $FIRMWARE_URL (blobless)"
  git clone -q --filter=blob:none --no-checkout "$FIRMWARE_URL" "$FW_GIT"
fi
if [ ! -d "$SIM_GIT" ]; then
  [ "$OFFLINE" = 0 ] || die "--offline but $SIM_GIT does not exist"
  log "cloning $SIMULATOR_URL"
  git clone -q --no-checkout "$SIMULATOR_URL" "$SIM_GIT"
fi
resolve() {  # <git dir> <ref> -> full commit sha; fetches unless the ref is a sha or a known tag
  local dir="$1" ref="$2" sha
  if [[ "$ref" =~ ^[0-9a-f]{7,40}$ ]] && sha="$(git -C "$dir" rev-parse -q --verify "$ref^{commit}")"; then
    echo "$sha"; return 0
  fi
  if sha="$(git -C "$dir" rev-parse -q --verify "refs/tags/$ref^{commit}")"; then
    echo "$sha"; return 0
  fi
  if [ "$OFFLINE" = 0 ]; then
    git -C "$dir" fetch -q --tags --force origin >&2 || die "fetch failed in $dir"
  fi
  git -C "$dir" rev-parse -q --verify "refs/tags/$ref^{commit}" \
    || git -C "$dir" rev-parse -q --verify "refs/remotes/origin/$ref^{commit}" \
    || git -C "$dir" rev-parse -q --verify "$ref^{commit}" \
    || die "unknown ref $ref in $dir"
}
FW_SHA="$(resolve "$FW_GIT" "$FW_REF")"
SIM_SHA="$(resolve "$SIM_GIT" "$SIM_REF")"
log "firmware $FW_REF = $FW_SHA, simulator $SIM_REF = $SIM_SHA"

FW_DIR="$WORK/fw/$SAFE_REF"
SIM_DIR="$WORK/simulator/${SIM_SHA:0:12}"
checkout_worktree() {  # <git dir> <worktree> <sha>
  local dir="$1" wt="$2" sha="$3"
  if [ -e "$wt/.git" ]; then
    [ "$(git -C "$wt" rev-parse HEAD)" = "$sha" ] || git -C "$wt" checkout -q --detach "$sha"
  else
    git -C "$dir" worktree prune
    git -C "$dir" worktree add -q -f --detach "$wt" "$sha"
  fi
}
checkout_worktree "$FW_GIT" "$FW_DIR" "$FW_SHA"
checkout_worktree "$SIM_GIT" "$SIM_DIR" "$SIM_SHA"
if [ "$OFFLINE" = 0 ] || [ ! -e "$FW_DIR/freeink-sdk/README.md" ]; then
  (cd "$FW_DIR" && git submodule sync -q --recursive && git submodule update -q --init --recursive)
fi

FW_VERSION="$(awk '/^\[crosspoint\]/{f=1;next} /^\[/{f=0} f&&/^version *=/{sub(/^version *= */,"");print;exit}' "$FW_DIR/platformio.ini")"
[ -n "$FW_VERSION" ] || FW_VERSION="$FW_REF"
grep -q '^extra_configs *= *platformio.local.ini' "$FW_DIR/platformio.ini" \
  || die "$FW_REF's platformio.ini has no 'extra_configs = platformio.local.ini'; pass the env via a project conf manually"

# --- platformio.local.ini from the simulator's own sample at that revision ---------
case "$(uname -s)" in
  Darwin) SAMPLE="$SIM_DIR/sample-platformio-macos.ini" ;;
  *)      SAMPLE="$SIM_DIR/sample-platformio-linux-wsl.ini" ;;
esac
[ -f "$SAMPLE" ] || SAMPLE="$SIM_DIR/sample-platformio.ini"
[ -f "$SAMPLE" ] || die "no simulator sample ini in $SIM_DIR"
LOCAL_INI="$FW_DIR/platformio.local.ini"
"$WORK/venv/bin/python" - "$SAMPLE" "$LOCAL_INI" "$SIM_DIR" "$FW_VERSION" "$PIO_ENV" <<'PYEOF'
import re, sys
sample, out, sim_dir, version, env = sys.argv[1:]
text = open(sample, encoding="utf-8").read()
# Use the pinned local simulator checkout instead of GitHub HEAD.
text = re.sub(r"^(\s*)simulator\s*=\s*\S+", lambda m: f"{m.group(1)}simulator=symlink://{sim_dir}", text, flags=re.M)
text = text.replace(".pio/libdeps/$PIOENV/simulator/", sim_dir + "/")
# Report the real release version (User-Agent, /api/status) instead of "dev-simulator".
text = re.sub(r'-DCROSSPOINT_VERSION=\\"[^"\\]*\\"', f'-DCROSSPOINT_VERSION=\\\\"{version}\\\\"', text)
# The project owns the (unused) run_simulator target so the lib hook does not add a second one.
text = re.sub(r"^\[env:simulator\]\n", "[env:simulator]\ncustom_run_simulator_target_owner = project\n", text, count=1, flags=re.M)
if f"[env:{env}]" not in text:
    if env != "simulator_x3":
        sys.exit(f"sample ini has no [env:{env}]")
    text += "\n[env:simulator_x3]\nextends = env:simulator\nbuild_flags =\n  ${env:simulator.build_flags}\n  -DSIMULATOR_DEVICE_X3\n  -DFREEINK_DEVICE_X3=1\n"
header = (f"; Generated by send-to-x3 e2e/sim/build_sim.sh from {sample}\n"
          f"; firmware version {version}; do not commit.\n")
open(out, "w", encoding="utf-8").write(header + text)
PYEOF

# --- build (skipped when the inputs are unchanged) ---------------------------------
OUT_DIR="$WORK/bin/$SAFE_REF"
INFO="$OUT_DIR/build-info.json"
SCRIPT_SUM="$(cat "$0" | git hash-object --stdin)"
STAMP="$FW_SHA $SIM_SHA $PIO_ENV $SCRIPT_SUM $(uname -sm)"
if [ "$FORCE" = 0 ] && [ -x "$OUT_DIR/program" ] && [ -f "$OUT_DIR/.stamp" ] && [ "$(cat "$OUT_DIR/.stamp")" = "$STAMP" ]; then
  log "up to date ($INFO)"
  echo "SIM_BIN=$OUT_DIR/program"
  exit 0
fi

log "building $PIO_ENV for $FW_REF (log: $LOG)"
START=$(date +%s)
if ! (cd "$FW_DIR" && pio run -e "$PIO_ENV") >"$LOG" 2>&1; then
  grep -E "error:|Error|undefined" "$LOG" | head -30 >&2 || true
  die "build failed; see $LOG"
fi
END=$(date +%s)
mkdir -p "$OUT_DIR"
cp "$FW_DIR/.pio/build/$PIO_ENV/program" "$OUT_DIR/program.tmp"
mv -f "$OUT_DIR/program.tmp" "$OUT_DIR/program"
cat >"$INFO" <<EOF
{
  "firmware_ref": "$FW_REF",
  "firmware_commit": "$FW_SHA",
  "firmware_version": "$FW_VERSION",
  "simulator_ref": "$SIM_REF",
  "simulator_commit": "$SIM_SHA",
  "pio_env": "$PIO_ENV",
  "platformio": "$PIO_VERSION",
  "host": "$(uname -sm)",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "build_seconds": $((END - START))
}
EOF
echo "$STAMP" >"$OUT_DIR/.stamp"
log "built in $((END - START)) s"
echo "SIM_BIN=$OUT_DIR/program"
