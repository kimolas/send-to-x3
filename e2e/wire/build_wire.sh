#!/usr/bin/env bash
# Build the wire harness: CrossPoint 2.x's real plugin HTTP code over real sockets.
#
#   e2e/wire/build_wire.sh [<crosspoint-ref>]       (default 2.0.0rc)
#
# Needs the firmware worktree and simulator that e2e/sim/build_sim.sh <ref> prepared in
# $SENDTOX3_E2E_WORK (default $HOME/.cache/sendtox3-e2e): it reuses the checkout, the
# ArduinoJson that PlatformIO fetched for it, and the simulator's HalStorage/Arduino
# shims. Output: $WORK/bin/<ref>/wire_harness; the last line printed is WIRE_BIN=<path>.
# Rebuilds only when an input changes. 1.6.5 has no plugin system: nothing to build.
set -euo pipefail

REF="${1:-2.0.0rc}"
WORK="${SENDTOX3_E2E_WORK:-$HOME/.cache/sendtox3-e2e}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SAFE_REF="$(printf '%s' "$REF" | tr '/' '_')"
FW="$WORK/fw/$SAFE_REF"
INFO="$WORK/bin/$SAFE_REF/build-info.json"
OUT="$WORK/bin/$SAFE_REF/wire_harness"

die() { echo "build_wire: $*" >&2; exit 1; }
[ -f "$INFO" ] || die "no simulator build for $REF; run e2e/sim/build_sim.sh $REF first"
[ -f "$FW/src/util/PluginEvents.cpp" ] || die "$REF has no plugin system (src/util/PluginEvents.cpp missing)"
SIM_SHA="$(sed -n 's/.*"simulator_commit": "\([0-9a-f]*\)".*/\1/p' "$INFO")"
VERSION="$(sed -n 's/.*"firmware_version": "\([^"]*\)".*/\1/p' "$INFO")"
SIM="$WORK/simulator/${SIM_SHA:0:12}/src"
SDK="$FW/freeink-sdk/libs"
AJ="$FW/.pio/libdeps/simulator_x3/ArduinoJson/src"
[ -d "$SIM" ] || die "simulator sources missing at $SIM"
[ -d "$AJ" ] || die "ArduinoJson missing at $AJ (build the simulator first)"

CXX="${CXX:-c++}"
FLAGS=(-std=gnu++2a -O1 -g -Wall -Wno-unused-parameter -Wno-unused-function
       -D_LIBCPP_DISABLE_AVAILABILITY -DSIMULATOR "-DCROSSPOINT_VERSION=\"$VERSION\""
       -DARDUINOJSON_ENABLE_ARDUINO_STRING=1 -DDESTRUCTOR_CLOSES_FILE=1)
if [ "$(uname -s)" = "Darwin" ]; then
  # Same Command Line Tools quirk as build_sim.sh: a stale CLT libc++ include dir.
  SDKROOT="$(xcrun --show-sdk-path)"
  CLT_CXX=/Library/Developer/CommandLineTools/usr/include/c++/v1
  if [ -d "$CLT_CXX" ] && [ ! -e "$CLT_CXX/cassert" ]; then
    FLAGS+=(-nostdinc++ -isystem "$SDKROOT/usr/include/c++/v1")
  fi
fi
# Include order matters: harness stubs, then the REAL SDK SecureNet headers (shadowing
# the simulator's curl-backed SecureHttpClient.h/ResumableFetch.h), then the
# simulator's Arduino/HalStorage shims, then the firmware.
INCLUDES=(-I"$HERE/stubs" -I"$SDK/network/SecureNet/include" -I"$SDK/network/CatalogList/include"
          -I"$SDK/network/JsonSax/include" -I"$SIM" -I"$AJ" -I"$FW/src" -I"$FW/lib/Utf8")
SOURCES=("$HERE/wire_harness.cpp" "$FW/src/util/PluginHttp.cpp" "$FW/src/util/PluginEvents.cpp"
         "$FW/src/util/PluginLocations.cpp" "$FW/src/util/StringUtils.cpp" "$FW/lib/Utf8/Utf8.cpp"
         "$FW/src/network/HttpDownloader.cpp" "$SDK/network/SecureNet/src/SecureClient.cpp"
         "$SDK/network/CatalogList/src/JsonListParser.cpp" "$SDK/network/JsonSax/src/StreamingJsonParser.cpp"
         "$SIM/HalStorage.cpp")

STAMP="$( (printf '%s\n' "${FLAGS[@]}" "${INCLUDES[@]}"; cat "${SOURCES[@]}" "$HERE"/stubs/*.h "$HERE"/stubs/components/*.h) \
          | git hash-object --stdin)"
if [ -x "$OUT" ] && [ -f "$OUT.stamp" ] && [ "$(cat "$OUT.stamp")" = "$STAMP" ]; then
  echo "WIRE_BIN=$OUT"
  exit 0
fi
mkdir -p "$(dirname "$OUT")"
"$CXX" "${FLAGS[@]}" "${INCLUDES[@]}" "${SOURCES[@]}" -o "$OUT.tmp"
mv -f "$OUT.tmp" "$OUT"
echo "$STAMP" > "$OUT.stamp"
echo "build_wire: built $OUT ($REF, firmware version $VERSION)" >&2
echo "WIRE_BIN=$OUT"
