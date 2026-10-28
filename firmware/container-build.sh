#!/usr/bin/env bash
# Runs inside the builder image (see container-setup.sh and build.sh). /src is the
# CrossPoint tree, /build a volume for build output, /out the host folder that
# receives the firmware. Usage: sendtox3-firmware-build [--no-tests] [--no-build]
set -euo pipefail

tests=1
build=1
for arg in "$@"; do
  case "$arg" in
    --no-tests) tests=0 ;;
    --no-build) build=0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd /src

if (( tests )); then
  # The plugin host tests (upstream runs every suite in CI; these cover the
  # catalog code the patches touch).
  cmake -S test -B /build/test -G Ninja -DCMAKE_BUILD_TYPE=Release >/build/test-configure.log
  cmake --build /build/test --target PluginCatalogOpenTest PluginCatalogPagingTest \
    PluginManifestClassificationTest PluginPickerLayoutTest >/build/test-build.log
  ctest --test-dir /build/test -R '^Plugin(CatalogOpen|CatalogPaging|ManifestClassification|PickerLayout)' \
    --output-on-failure
fi

if (( build )); then
  # As upstream CI: install the platform, then pin the core inside the
  # platform's own penv (an unpinned one fights the platform over tool-scons).
  pio pkg install -e sendtox3
  uv pip install --python ~/.platformio/penv/bin/python "pioarduino==6.1.19"
  pio run -e sendtox3 2>&1 | tee /build/pio.log
  bin=/build/pio/sendtox3/firmware.bin
  version=$(grep -ao '2\.[0-9]*\.[0-9]*-sx3+[0-9a-f]*' "$bin" | head -n1)
  cp "$bin" "/out/crosspoint-${version}-x3-x4.bin"
  grep -E '^(RAM|Flash):' /build/pio.log >"/out/crosspoint-${version}-x3-x4.size.txt" || true
  echo "built /out/crosspoint-${version}-x3-x4.bin"
fi
