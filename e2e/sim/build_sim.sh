#!/usr/bin/env bash
# Build the CrossPoint firmware as a native, headless X3 simulator.
#
#   e2e/sim/build_sim.sh [options] <crosspoint-ref | develop | fork>
#
#   <crosspoint-ref>     a tag/branch/commit of crosspoint-reader, e.g. 2.0.0rc or 1.6.5
#   develop              develop pinned at 8f4d035 (2026-10-28), the base of the Send to X3 firmware
#   fork                 the Send to X3 firmware: firmware/UPSTREAM's commit + firmware/patches/*.patch,
#                        on a simulator with e2e/sim/patches/simulator/*.patch (CROSSPOINT_SIM_RESTART=exec)
#   --sim-ref <rev>      crosspoint-simulator revision (default: pinned per firmware ref below)
#   --fw-patch <file>    git-apply <file> to the firmware checkout (repeatable; replaces fork's default set)
#   --sim-patch <file>   git-apply <file> to the simulator checkout (repeatable; replaces fork's default set)
#   --name <name>        output name (default: the ref; <ref>+<patch hash> with --fw-patch)
#   --env <pio-env>      PlatformIO env to build (default: simulator_x3)
#   --force              rebuild even if an up-to-date binary exists
#   --offline            do not fetch from GitHub (use the clones already in the work dir)
#
# Everything lives in a work dir OUTSIDE the repo:
#   SENDTOX3_E2E_WORK    default $HOME/.cache/sendtox3-e2e (must not contain spaces:
#                        PlatformIO's symlink:// lib_deps cannot express them)
#   $WORK/git/           blobless clone of crosspoint-reader + clone of crosspoint-simulator
#   $WORK/fw/<name>/     firmware worktree (detached at the ref, patches applied, submodules initialised)
#   $WORK/simulator/<sha12>[-<patch hash>]/  simulator worktree (patched ones get their own)
#   $WORK/venv/          python venv with PlatformIO
#   $WORK/pio-core/      PLATFORMIO_CORE_DIR (native platform + tool cache)
#   $WORK/bin/<name>/program + build-info.json   the result
#   $WORK/logs/build-<name>.log
#
# The last line printed is `SIM_BIN=<path to program>`.
#
# The firmware checkout is never committed to. Patches are applied to the working tree
# (the worktree is reset to the commit first whenever the patch set changes). The build
# adds an untracked platformio.local.ini (picked up by the firmware's own `extra_configs`),
# and the simulator's library build hook applies its documented idempotent source patches
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
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

FW_REF=""
SIM_REF=""
NAME=""
PIO_ENV="simulator_x3"
FORCE=0
OFFLINE=0
FW_PATCHES=()
SIM_PATCHES=()

die() { echo "build_sim: $*" >&2; exit 1; }
log() { echo "build_sim: $*" >&2; }
abspath() { [ -f "$1" ] || die "no such patch file: $1"; echo "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --sim-ref) SIM_REF="${2:?--sim-ref needs a value}"; shift 2 ;;
    --fw-patch) FW_PATCHES+=("$(abspath "${2:?--fw-patch needs a file}")"); shift 2 ;;
    --sim-patch) SIM_PATCHES+=("$(abspath "${2:?--sim-patch needs a file}")"); shift 2 ;;
    --name) NAME="${2:?--name needs a value}"; shift 2 ;;
    --env) PIO_ENV="${2:?--env needs a value}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --offline) OFFLINE=1; shift ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) [ -z "$FW_REF" ] || die "only one <crosspoint-ref> allowed"; FW_REF="$1"; shift ;;
  esac
done
[ -n "$FW_REF" ] || die "usage: build_sim.sh [--sim-ref REV] [--fw-patch FILE]... [--sim-patch FILE]... [--name NAME] [--env ENV] [--force] <crosspoint-ref>"

# Named builds. develop is pinned (the branch moves); the fork follows firmware/UPSTREAM,
# so re-check the simulator pin below whenever that commit changes.
DEVELOP_SHA="8f4d035cbc56ae226f0881cfce85f65243988e1b"
BASE_REF="$FW_REF"
case "$FW_REF" in
  develop)
    BASE_REF="$DEVELOP_SHA" ;;
  fork)
    BASE_REF="$(sed -n 's/^COMMIT=//p' "$REPO/firmware/UPSTREAM" 2>/dev/null || true)"
    [ -n "$BASE_REF" ] || die "fork: no COMMIT= in $REPO/firmware/UPSTREAM"
    [ "$BASE_REF" = "$DEVELOP_SHA" ] || log "warning: firmware/UPSTREAM is $BASE_REF, not $DEVELOP_SHA; re-check the simulator pin"
    if [ ${#FW_PATCHES[@]} -eq 0 ]; then
      for p in "$REPO"/firmware/patches/*.patch; do [ -f "$p" ] && FW_PATCHES+=("$p"); done
      [ ${#FW_PATCHES[@]} -gt 0 ] || die "fork: no patches in $REPO/firmware/patches"
    fi
    if [ ${#SIM_PATCHES[@]} -eq 0 ]; then
      for p in "$HERE"/patches/simulator/*.patch; do [ -f "$p" ] && SIM_PATCHES+=("$p"); done
    fi ;;
esac

# Simulator revisions verified to build and boot each firmware on the X3. The simulator
# has no tags; it tracks firmware develop, so pick the newest commit whose HAL shims
# still match the firmware (see e2e/README.md for how these were chosen). develop from
# 9ba67ea on calls SecureHttpClient::connect, which the shim has from 5b38077 on.
if [ -z "$SIM_REF" ]; then
  case "$FW_REF" in
    2.0.0rc) SIM_REF="1777fe2f1da1405cf5ecf191b831734e8862a92f" ;;
    3f387a9*) SIM_REF="1777fe2f1da1405cf5ecf191b831734e8862a92f" ;;
    1.6.5)   SIM_REF="9d963b6" ;;
    develop|fork|8f4d035*) SIM_REF="5b380779ddea5f1549f649eab0480e69e30a77b6" ;;
    *)       SIM_REF="main" ;;
  esac
fi

case "$WORK" in *" "*) die "SENDTOX3_E2E_WORK must not contain spaces: $WORK" ;; esac
case "$BASE_REF$SIM_REF" in *[!A-Za-z0-9._/-]*) die "refs may only contain [A-Za-z0-9._/-]" ;; esac

# Patch sets are identified by their contents (git blob ids), in the order given.
patch_sums() { local p; for p in "$@"; do printf '%s ' "$(git hash-object "$p")"; done; }
FW_PATCH_SUMS="$(patch_sums ${FW_PATCHES[@]+"${FW_PATCHES[@]}"})"
SIM_PATCH_SUMS="$(patch_sums ${SIM_PATCHES[@]+"${SIM_PATCHES[@]}"})"
set_tag() { printf '%s' "$1" | git hash-object --stdin | cut -c1-8; }
if [ -z "$NAME" ]; then
  NAME="$FW_REF"
  [ -z "$FW_PATCH_SUMS" ] || [ "$FW_REF" = fork ] || NAME="$FW_REF+$(set_tag "$FW_PATCH_SUMS")"
fi
SAFE_NAME="$(printf '%s' "$NAME" | tr '/' '_')"
case "$SAFE_NAME" in ""|.*|*[!A-Za-z0-9._+-]*) die "--name may only contain [A-Za-z0-9._+-] and must not start with a dot" ;; esac

for tool in git curl perl; do command -v "$tool" >/dev/null || die "$tool not found"; done
command -v sdl2-config >/dev/null || die "sdl2-config not found (macOS: brew install sdl2; Debian: apt install libsdl2-dev)"
command -v "$PY" >/dev/null || die "$PY not found (set PYTHON=)"

mkdir -p "$WORK/git" "$WORK/fw" "$WORK/simulator" "$WORK/bin" "$WORK/logs"
LOG="$WORK/logs/build-$SAFE_NAME.log"

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
FW_SHA="$(resolve "$FW_GIT" "$BASE_REF")"
SIM_SHA="$(resolve "$SIM_GIT" "$SIM_REF")"
log "firmware $FW_REF = $FW_SHA${FW_PATCH_SUMS:+ + ${#FW_PATCHES[@]} patch(es)}, simulator $SIM_REF = $SIM_SHA${SIM_PATCH_SUMS:+ + ${#SIM_PATCHES[@]} patch(es)}"

FW_DIR="$WORK/fw/$SAFE_NAME"
SIM_DIR="$WORK/simulator/${SIM_SHA:0:12}"
[ -z "$SIM_PATCH_SUMS" ] || SIM_DIR="$SIM_DIR-$(set_tag "$SIM_PATCH_SUMS")"
prepare_worktree() {  # <git dir> <worktree> <sha> [patch files...]: detached at <sha> with exactly these patches
  local dir="$1" wt="$2" sha="$3" mark have="" want p
  shift 3
  want="$sha $(patch_sums ${@+"$@"})"
  if [ ! -e "$wt/.git" ]; then
    git -C "$dir" worktree prune
    git -C "$dir" worktree add -q -f --detach "$wt" "$sha"
  fi
  mark="$(git -C "$wt" rev-parse --absolute-git-dir)/sendtox3-patches"
  [ -f "$mark" ] && have="$(cat "$mark")"
  if [ "$(git -C "$wt" rev-parse HEAD)" = "$sha" ] && { [ "$have" = "$want" ] || { [ -z "$have" ] && [ $# -eq 0 ]; }; }; then
    return 0
  fi
  if [ -n "$have" ] || [ $# -gt 0 ]; then
    # Start from the pristine commit: reverts earlier patches and removes the files they added
    # (ignored files, i.e. .pio/ and platformio.local.ini, stay).
    git -C "$wt" checkout -q -f --detach "$sha"
    git -C "$wt" reset -q --hard
    git -C "$wt" clean -q -fd
  else
    git -C "$wt" checkout -q --detach "$sha"
  fi
  for p in "$@"; do
    git -C "$wt" apply --whitespace=nowarn "$p" || die "$(basename "$p") does not apply to ${sha:0:12} in $wt"
    log "applied $(basename "$p") to $(basename "$wt")"
  done
  if [ $# -gt 0 ]; then printf '%s' "$want" >"$mark"; else rm -f "$mark"; fi
}
prepare_worktree "$FW_GIT" "$FW_DIR" "$FW_SHA" ${FW_PATCHES[@]+"${FW_PATCHES[@]}"}
prepare_worktree "$SIM_GIT" "$SIM_DIR" "$SIM_SHA" ${SIM_PATCHES[@]+"${SIM_PATCHES[@]}"}

# Submodules (freeink-sdk, and lucide inside it) are cloned from another worktree's module
# repos when one has the pinned commit, so a new worktree needs no download and works
# --offline; anything missing comes from GitHub.
FW_GITDIR="$(git -C "$FW_GIT" rev-parse --absolute-git-dir)"
LOCAL_URLS=()
find_local_modules() {  # <git dir> <commit> <.gitmodules: file or ""=blob at commit> <modules/ prefix>
  local gd="$1" commit="$2" file="$3" sub="$4" key url name path sha cand
  local cfg=(--blob "$commit:.gitmodules")
  [ -z "$file" ] || cfg=(--file "$file")
  while read -r key url; do
    name="${key#submodule.}"; name="${name%.url}"
    path="$(git --git-dir="$gd" config "${cfg[@]}" "submodule.$name.path" 2>/dev/null)" || continue
    sha="$(git --git-dir="$gd" ls-tree "$commit" -- "$path" 2>/dev/null | awk '$2 == "commit" {print $3}')"
    [ -n "$sha" ] || continue
    for cand in "$FW_GITDIR"/worktrees/*/modules/"$sub$name"; do
      [ -d "$cand" ] && git --git-dir="$cand" cat-file -e "$sha^{commit}" 2>/dev/null || continue
      LOCAL_URLS+=(-c "url.$cand.insteadOf=$url")
      find_local_modules "$cand" "$sha" "" "$sub$name/modules/"
      break
    done
  done < <(git --git-dir="$gd" config "${cfg[@]}" --get-regexp '^submodule\..*\.url$' 2>/dev/null || true)
}
if [ -f "$FW_DIR/.gitmodules" ]; then
  find_local_modules "$FW_GITDIR" "$FW_SHA" "$FW_DIR/.gitmodules" ""
fi
if [ "$OFFLINE" = 0 ] || [ ! -e "$FW_DIR/freeink-sdk/README.md" ]; then
  [ ${#LOCAL_URLS[@]} -eq 0 ] || log "submodules: $(( ${#LOCAL_URLS[@]} / 2 )) available from local module repos"
  (cd "$FW_DIR" && git submodule sync -q --recursive \
    && git -c protocol.file.allow=always ${LOCAL_URLS[@]+"${LOCAL_URLS[@]}"} submodule update -q --init --recursive) \
    || die "submodule update failed in $FW_DIR"
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
OUT_DIR="$WORK/bin/$SAFE_NAME"
INFO="$OUT_DIR/build-info.json"
SCRIPT_SUM="$(git hash-object "$0")"
STAMP="$FW_SHA $SIM_SHA $PIO_ENV $SCRIPT_SUM $(uname -sm) fw-patches:${FW_PATCH_SUMS% } sim-patches:${SIM_PATCH_SUMS% }"
if [ "$FORCE" = 0 ] && [ -x "$OUT_DIR/program" ] && [ -f "$OUT_DIR/.stamp" ] && [ "$(cat "$OUT_DIR/.stamp")" = "$STAMP" ]; then
  log "up to date ($INFO)"
  echo "SIM_BIN=$OUT_DIR/program"
  exit 0
fi

log "building $PIO_ENV for $NAME (log: $LOG)"
START=$(date +%s)
if ! (cd "$FW_DIR" && pio run -e "$PIO_ENV") >"$LOG" 2>&1; then
  grep -E "error:|Error|undefined" "$LOG" | head -30 >&2 || true
  die "build failed; see $LOG"
fi
END=$(date +%s)
mkdir -p "$OUT_DIR"
cp "$FW_DIR/.pio/build/$PIO_ENV/program" "$OUT_DIR/program.tmp"
mv -f "$OUT_DIR/program.tmp" "$OUT_DIR/program"
"$WORK/venv/bin/python" - "$INFO" "$NAME" "$FW_REF" "$FW_SHA" "$FW_VERSION" "$SIM_REF" "$SIM_SHA" "$SIM_DIR" \
    "$PIO_ENV" "$PIO_VERSION" "$(uname -sm)" "$((END - START))" "${#FW_PATCHES[@]}" \
    ${FW_PATCHES[@]+"${FW_PATCHES[@]}"} ${SIM_PATCHES[@]+"${SIM_PATCHES[@]}"} <<'PYEOF'
import hashlib, json, sys, time
from pathlib import Path
out, name, ref, sha, version, sim_ref, sim_sha, sim_dir, env, pio, host, secs, n_fw, *patches = sys.argv[1:]
def blob(p):  # the same id `git hash-object` gives, which the stamp uses
    data = Path(p).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
def listed(ps):
    return [{"file": Path(p).name, "path": p, "blob": blob(p)} for p in ps]
info = {"name": name, "firmware_ref": ref, "firmware_commit": sha, "firmware_version": version,
        "firmware_patches": listed(patches[:int(n_fw)]), "simulator_ref": sim_ref, "simulator_commit": sim_sha,
        "simulator_patches": listed(patches[int(n_fw):]), "simulator_dir": sim_dir,
        "pio_env": env, "platformio": pio, "host": host,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "build_seconds": int(secs)}
Path(out).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
PYEOF
echo "$STAMP" >"$OUT_DIR/.stamp"
log "built in $((END - START)) s"
echo "SIM_BIN=$OUT_DIR/program"
