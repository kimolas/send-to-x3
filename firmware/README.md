# Send to X3 firmware: developer notes

The Send to X3 firmware is upstream CrossPoint at a pinned commit plus a small patch
series. Users install the result with the X3's own SD Card Firmware Update; their
guide is [docs/FIRMWARE.md](../docs/FIRMWARE.md). This page is for building, testing
and moving it to a newer CrossPoint.

## Layout

```
firmware/
  UPSTREAM             URL and COMMIT of upstream CrossPoint the patches apply to
  patches/*.patch      the patch series, applied in order with `git am`
  build.sh             host entry point: clone/update, apply, test, build (in Docker)
  container-setup.sh   turns python:3.13-slim into the builder (docker run + docker commit
                       -> sendtox3-firmware-builder:1: pioarduino core 6.1.19, cmake, ninja)
  container-build.sh   runs inside the builder image: host tests, then `pio run -e sendtox3`
  dist/                build output (gitignored)
```

`UPSTREAM` pins develop `8f4d035` (2026-10-28). Everything the app relies on is
in one patch today:

`0001-feat-open-plugin-catalog-books-in-the-reader.patch`

- **`download.open`** (boolean, default false) in a JSON catalog's `device.json`. With
  it, Confirm on a single-file row whose destination the reader opens (`.epub`,
  `.txt`, `.md`, `.xtc`) opens the book: a copy already at the destination opens as
  is (no request; an `.epub` must start with a ZIP signature, otherwise it is fetched
  again), otherwise the book downloads as usual and then opens. Opening saves
  `APP_STATE.openEpubPath` and calls `silentRestartToReader()`, the clean-heap reboot
  every Wi-Fi screen does on exit. After a download it first shows the Loading popup
  and drains the plugin event outboxes while Wi-Fi is up (so the app's
  `POST /device/v1/fetched` happens before the reboot), unless the heap is below
  `HttpDownloader::MIN_TLS_*` (the events then wait for the next Wi-Fi join); a copy
  already on the card queued no event, so it skips the drain. If `state.json` cannot
  be saved, a download keeps the Download complete screen and a copy already on the
  card fails with `STR_FILE_OPEN_FAILED` ("Cannot open file"). Bundle rows and other
  file types keep the Download complete screen. The footer and details label read
  Open (`STR_OPEN`) on the rows that open, Fetch on the others; no new strings.
- `PluginCatalogOpen.h`: the decision as a header-only function, with the host test
  `test/plugin_catalog_open/`.
- The capability **`catalog.open_after_download`** in `FirmwareIdentity.h`, so it
  appears in `/api/status` `pluginCapabilities` and `{plugin_capabilities}`. The app
  reads it during the Wi-Fi plugin install (`plugin_bundle.opens_books`).
- The `sendtox3` build env in `platformio.ini`: the X3/X4 release flags with
  `CROSSPOINT_VERSION = ${crosspoint.version}-sx3+${SENDTOX3_FW_HASH}`. The suffix keeps
  the built-in updater from offering official 2.0.0 as an update (it compares the
  first three numbers, and treats an `-rc` build as older than the release); see the
  table in docs/FIRMWARE.md. The board tag stays `x4`, so official images and this one
  accept each other in the SD updater.
- `docs/sd-plugins.md`: the key, the capability and the behaviour.

Stock firmware never reads `download.open`, so the app always writes it
(`plugin_bundle.build_device_json`).

## Building

```sh
firmware/build.sh                  # clone/update upstream, apply the patches, host tests, build
firmware/build.sh --no-tests       # skip the host tests
firmware/build.sh --tests-only     # only the host tests
firmware/build.sh --src DIR        # build an existing CrossPoint checkout as it is
```

- Needs Docker and git. The source tree lives in `$SENDTOX3_FW_WORK/crosspoint`
  (default `~/.cache/sendtox3-firmware`; the path must not contain spaces); each run
  drops an interrupted `git am`, resets the tree to `COMMIT` (`checkout --force`,
  `clean -fdq`) and applies the patches, so do not work in it. If the patches do not
  apply, build.sh says so and leaves no `git am` state behind.
- The builder image is `sendtox3-firmware-builder:1`, made once from
  `python:3.13-slim` by `container-setup.sh`. After changing `container-setup.sh`,
  run `docker image rm sendtox3-firmware-builder:1` (or bump the tag in build.sh) so
  the next build makes it again. PlatformIO's packages and the build output live in
  the Docker volumes `sendtox3-fw-pio` and `sendtox3-fw-build` (about 5 GB;
  `docker volume rm` them to start over; `docker image rm
  sendtox3-firmware-builder:1 python:3.13-slim` frees the images).
- The first build downloads the toolchain and rebuilds the Arduino core with the
  project's `custom_sdkconfig`: 20 to 40 minutes. Later builds take a few minutes.
- Output: `firmware/dist/crosspoint-2.0.0-sx3+<hash>-x3-x4.bin` and a `.size.txt`
  with the RAM/flash summary. `<hash>` is `git rev-parse --short=7 HEAD` of the
  patched tree. build.sh fixes the committer and the dates, so the same `UPSTREAM`
  and patches always give the same hash, and a version names exactly what was built.
  (`--src` builds whatever commit that checkout has.)
- As upstream CI, the platform is installed first and the core inside the
  platform's own penv is pinned to `pioarduino==6.1.19` (an unpinned one fights the
  platform over `tool-scons`).

## Host tests

`container-build.sh` configures upstream's `test/` CMake project and runs the suites
that cover the catalog code the patch touches:

```
PluginCatalogOpenTest PluginCatalogPagingTest PluginManifestClassificationTest PluginPickerLayoutTest
```

`firmware/build.sh --tests-only` runs just those. Upstream CI runs every suite; to do
the same, run `cmake -S test -B build/test && cmake --build build/test && ctest
--test-dir build/test` in a checkout (see upstream `test/README`).

The app side has no firmware toolchain dependency: `tests/test_plugin_bundle.py`
checks the `device.json` the app writes, and `tests/test_web_device.py` models how
each firmware pages the catalog. End-to-end coverage in the simulator is described in
[e2e/README.md](../e2e/README.md).

## Moving to a newer CrossPoint

Work in your own clone, not in the build tree:

```sh
git clone --recursive https://github.com/crosspoint-reader/crosspoint-reader.git ~/src/cp
cd ~/src/cp
git checkout -b send-to-x3 <COMMIT from firmware/UPSTREAM>
git am "$HOME/My Drive/Claude/Projects/xteink/firmware/patches/"*.patch
git fetch origin
BASE=origin/develop                                  # or a chosen commit
git rebase "$BASE"
git submodule update --init --recursive              # the rebase may move freeink-sdk
```

Resolve conflicts, then test and build that tree as it is:

```sh
"$HOME/My Drive/Claude/Projects/xteink/firmware/build.sh" --src ~/src/cp
```

When it works, regenerate the series and the pin:

```sh
rm "$HOME/My Drive/Claude/Projects/xteink/firmware/patches/"*.patch
git format-patch -o "$HOME/My Drive/Claude/Projects/xteink/firmware/patches" "$BASE"
```

and set `COMMIT=` (and the date in the comment) in `UPSTREAM` to the full hash of
`$BASE` (`git rev-parse "$BASE"`). Then run `firmware/build.sh` once without `--src` to check that the
series applies cleanly.

What to look at after a rebase:

- **Catalog paging.** The app's catalog contract depends on what the firmware sends
  for `{page}`, `{limit}` and `{offset}` (`PluginCatalogPaging.h`,
  `PluginCatalogActivity::substituted`): today `limit = page_size + 1` and
  `offset = (page - 1) * page_size` for the app's template. If that changes, update
  `delivery.catalog_page` and the firmware models in `tests/test_web_device.py`.
- **The download path.** The patch splits `downloadBook` into `bookDestination` and
  the download; upstream changes to the destination rules (sanitizing, `dest_dir`
  config, `protectedpaths::isPluginPath`) land in `bookDestination`.
- **Restart helpers.** `silentRestartToReader()` and the reader route at boot
  (`main.cpp`, target READER opens `APP_STATE.openEpubPath`).
- **Event drain.** `pluginevents::drain` still sends the outbox synchronously while
  Wi-Fi is up.
- **The capability string** in `FirmwareIdentity.h` (the app matches it exactly) and
  the `sendtox3` env next to upstream's release envs in `platformio.ini`.
- **Version.** `crosspoint.version` in `platformio.ini`. Once upstream releases that
  version, the built-in updater offers the next official release over this build
  (docs/FIRMWARE.md).

## Licence

CrossPoint is MIT-licensed; the patches are offered under the same licence. Self-built
firmware lacks the LCP release key that official builds inject in CI, so LCP-protected
books do not open on it.
