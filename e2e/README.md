# Firmware end-to-end tests

These harnesses run **real, unmodified CrossPoint firmware code** on a Mac or Linux
box, with no X3 attached, against a Send to X3 backend. The one exception is `fork`, the
Send to X3 firmware: upstream develop plus `firmware/patches/`, exactly what users flash.

| Harness | What runs | Used for |
|---|---|---|
| `sim/` | The whole firmware compiled for the X3 with [crosspoint-simulator](https://github.com/crosspoint-reader/crosspoint-simulator): native, headless SDL, scripted buttons, BMP screenshots, a folder as the SD card | Plugin catalog, sleep delivery, File Transfer (LAN push), OPDS, reading the files, the Send to X3 firmware opening a download in the reader |
| `wire/` | The 2.x plugin HTTP code (`PluginHttp`, `PluginEvents`, `HttpDownloader`, the SDK's `SecureHttpClient`) over real sockets | The exact bytes the device sends, and response framing against the device's own client |
| `fwharness/` | The EPUB/TXT parsers and layout (separate owner; see its README) | Rendering checks of generated files |

`run_e2e.py` runs the scenarios below against a pluggable backend and writes a report.

## Quick start

```sh
export SENDTOX3_E2E_WORK=$HOME/.cache/sendtox3-e2e    # default; must not contain spaces
e2e/sim/build_sim.sh 2.0.0rc                           # ~1.5 min the first time
e2e/sim/build_sim.sh 1.6.5
e2e/sim/build_sim.sh develop                           # develop 8f4d035
e2e/sim/build_sim.sh fork                              # the Send to X3 firmware (firmware/)
e2e/wire/build_wire.sh 2.0.0rc                         # a few seconds; needs build_sim <ref> first
python3 e2e/run_e2e.py                                 # S1-S5 + W1 on 2.0.0rc and 1.6.5, mock backend
python3 e2e/run_e2e.py -s S6 -f fork                   # Confirm opens the book
python3 e2e/run_e2e.py -s S2 -f 2.0.0rc,3f387a9        # a subset (3f387a9: see "Firmware findings")
python3 e2e/run_e2e.py -f releases,fork -b local       # the releases and the fork, the real app
python3 e2e/run_e2e.py --list
```

Each run keeps everything under `$SENDTOX3_E2E_WORK/runs/<timestamp>/<scenario>-<firmware>/`:
the SD card folder (`sd/`), per simulator run the `serial.log` and `shots/*.png`, the
recorded HTTP exchanges (`backend_exchanges.json`, `device_exchanges.json`), the
backend's log and data dir, and `report.json` at the top with every check and, per
result, the simulator's `build-info.json` (what was tested).

`--build` runs `build_sim.sh` for every firmware a selected scenario runs on (and
`build_wire.sh` where W1 runs) first; both rebuild only when an input changed. Without
it, a simulator built from other patches or another base commit than the checkout has
(`firmware/patches`, `firmware/UPSTREAM`, `sim/patches/simulator`; develop's pin) makes
its scenarios fail with "stale ... simulator" instead of quietly testing the old build. `SENDTOX3_E2E_SLOWDOWN=2` doubles every
wait in the input scripts (for a loaded CI runner). The backend process uses the
interpreter running `run_e2e.py` (or `--python`); the app needs its requirements.

## Building the simulator (`sim/build_sim.sh <crosspoint-ref | develop | fork>`)

Everything goes to the work dir, never into this repo:

```
$WORK/git/crosspoint-reader.git   blobless clone        $WORK/fw/<name>/          firmware worktree + submodules
$WORK/git/crosspoint-simulator    clone                 $WORK/simulator/<sha>[-<patches>]/  simulator worktree
$WORK/venv/                       platformio==6.2.0     $WORK/pio-core/           PLATFORMIO_CORE_DIR
$WORK/bin/<name>/program          the simulator         $WORK/bin/<name>/build-info.json
$WORK/logs/build-<name>.log
```

- The simulator has no tags; it tracks firmware develop. Pinned pairs, verified to build
  and run every scenario: **2.0.0rc** (`91ed3ef`, 2026-10-19) and **3f387a9** with
  simulator `1777fe2` (main, 2026-10-25); **1.6.5** (`93e98bb`, 2026-09-27) with
  simulator `9d963b6` (2026-09-29, the last commit before "Update simulator for SD
  plugins"); **develop** (pinned at `8f4d035`, 2026-10-28) and **fork** with simulator
  `5b38077` ("match the SDK SecureHttpClient in the shim"; develop calls
  `SecureHttpClient::connect` since `9ba67ea`, which `1777fe2` lacks). 1.6.5 is an
  ancestor of 2.0.0rc on develop, so a simulator commit from just after its date
  matches its HAL. Other refs default to simulator `main`; override with `--sim-ref`.
- **`fork`** is the Send to X3 firmware: the commit in `firmware/UPSTREAM` plus
  `firmware/patches/*.patch`, on simulator `5b38077` plus
  `sim/patches/simulator/*.patch` (below). In general `--fw-patch FILE` and
  `--sim-patch FILE` (both repeatable) `git apply` patches to the checkouts; a patched
  firmware build is named `<ref>+<hash>` unless `--name` says otherwise, and a patched
  simulator gets its own worktree, `simulator/<sha12>-<hash>`. When the patch set
  changes, the worktree is reset to the commit first. The patch blob ids are part of
  the rebuild stamp and listed in `build-info.json`, with the simulator worktree
  (`simulator_dir`, which `build_wire.sh` compiles against). When `firmware/UPSTREAM`
  moves, re-check the simulator pin (the script warns).
- A new worktree's submodules (`freeink-sdk`, and `lucide` inside it) are cloned from
  another worktree's module repos when one has the pinned commit, so it needs no
  download and works `--offline`; anything missing comes from GitHub.
- `platformio.local.ini` (picked up by the firmware's own `extra_configs`) is generated
  from the simulator's `sample-platformio-macos.ini` / `-linux-wsl.ini` at that commit,
  with the simulator dependency pointed at the pinned local worktree and
  `CROSSPOINT_VERSION` set to the release's real version (`2.0.0`, `1.6.5`) so the
  User-Agent and `/api/status` are what a device reports. The firmware's
  `platformio.ini` is not edited. The simulator's library hook applies its documented
  idempotent patches to the worktree (`BookMetadataCache` size_t->uint32_t,
  `GfxRenderer::setOrientation` notification).
- **macOS toolchain quirk:** some Command Line Tools installs have a stale
  `/Library/Developer/CommandLineTools/usr/include/c++/v1` holding only
  `__cxx_version`; it hides the SDK's libc++ and every C++ file fails with
  `'cassert' file not found`. The script detects it and exports
  `CPLUS_INCLUDE_PATH=$(xcrun --show-sdk-path)/usr/include/c++/v1` (reinstalling the
  CLT also fixes it).
- **SDL2:** `sdl2-config` must be on PATH (`brew install sdl2`; this Mac has
  `sdl2-compat` 2.32, which works). Linux: `libsdl2-dev libssl-dev`.
- Rebuilds are skipped when firmware commit, simulator commit, patches, env and script
  are unchanged (`--force` to rebuild). `--offline` uses the existing clones.

### The simulator restart patch (`sim/patches/simulator/0001-real-software-restart.patch`)

Upstream, `ESP.restart()` does nothing in the simulator and `RTC_NOINIT_ATTR` variables
are plain globals, so the firmware's silent restart (the reboot every Wi-Fi screen does
on exit, and the one the fork uses to open the reader) continues in the same process.
The patch, applied to the fork's simulator only, adds an opt-in real restart: with
`CROSSPOINT_SIM_RESTART=exec` the simulator saves the RTC_NOINIT section (its own linker
section) to `$TMPDIR`, prints `[SIM] software restart: relaunching (rtc N bytes)`,
`execvp`s itself (same pid and serial pipe), restores the section before `setup()`
(`[SIM] software restart: boot N, restored X/Y rtc bytes`) and runs
`CROSSPOINT_SIM_INPUT_SCRIPT_AFTER_RESTART` / `..._SCREENSHOTS_AFTER_RESTART` instead of
the old schedule (timed from the relaunch, like the `_AFTER_WAKE` pair). The listening
sockets are close-on-exec so a relaunch can bind them again. Without the variable
nothing changes, so every other scenario runs on the patched simulator as before.
Verified on macOS (the `section$start$__DATA$__rtc_noinit` symbols); the Linux
`__start_rtc_noinit` path is untested. Worth offering upstream.

## Simulated SD card (`sim/sd.py`)

`build_sd(root, firmware, ...)` writes the firmware's own on-card formats (documented
with file:line in the module): `settings.json` with plugins on and the Plugin Hub row
hidden, `wifi.json` with a saved credential for the simulator's fake network (so Wi-Fi
auto-joins), optional `opds.json` servers and `opdsDownloadFolder`, an extracted plugin
bundle zip (refusing `..`, absolute names, symlinks), an empty `/Inbox`, and any files
to pre-place. `SdCard` reads back inbox files, the plugin outbox (`events.jsonl`),
snapshots and diffs. `browser_listing` reproduces the File Browser's order
(`FsHelpers::sortFileList`), which is how navigation finds a file.

## Running the simulator (`sim/runner.py`, `sim/nav.py`)

Input is open loop: `CROSSPOINT_SIM_INPUT_SCRIPT` is read once per process. `InputScript`
builds it with a moving cursor (`press`, `hold`, `wait`, `shot`, `quit`); `Nav` turns
intents into key sequences derived from each release's menu code (`sim/firmware.py`):
Home rows, the Plugins picker, catalog fetch, OPDS (2.x opens a publication page
first), File Transfer > Join a Network, Browse Files > folders > file, page forward,
and sleep (**hold POWER**; the simulator's own `SLEEP` key is never consumed by this
firmware). `run_sim` sets `CROSSPOINT_SIM_*`, streams the serial log, fires `Watch`
callbacks on log lines, stops early when a condition holds (never before the
scheduled screenshots exist), and converts BMP screenshots to PNG without
dependencies (`sim/images.py`).

`run_sim(restart="exec", after_restart=InputScript(...))` turns on the real restart
above (simulators built with the patch; others ignore it) and gives the relaunched
process its own schedule. `SimRun.boots()` splits the serial log at each relaunch;
`activities(boot=)`, `entered(name, boot=)` and `silent_restarts(boot=)` (the
`Silent restart (target=X)` targets) look at one boot, and `relaunched_at` holds the
wall-clock time of each relaunch, to order it against the wire tap's exchanges. On the
fork, `Nav.catalog_fetch` waits for the book to open and `Nav.dismiss` raises: there is
no "Download complete" screen. Once a book has been opened, Home lists it above its
menu: pass `Nav(recent_books=HOME_RECENT_ROWS)` (one row in the default Lyra theme).

Verification never reads screen text: the scenarios assert on the activities the
firmware logs (`[ACT] Entering activity: X`), firmware log lines, files on the card,
the HTTP exchanges recorded by a byte-exact TCP relay (`scenarios/wiretap.py`) between
the simulator and the backend, and simple image metrics (ink ratio, page-to-page
difference). Screenshots are for humans.

## Wire harness (`wire/`)

`build_wire.sh <ref>` compiles `wire_harness.cpp` with the firmware's real
`PluginHttp.cpp`, `PluginEvents.cpp`, `PluginLocations.cpp`, `StringUtils.cpp`,
`HttpDownloader.cpp`, the SDK's SecureNet (plain HTTP), `JsonListParser` and
`StreamingJsonParser`, plus the simulator's `HalStorage` (a folder as SD) and small
stubs (POSIX-socket `WiFiClient`). The catalog/download path mirrors
`PluginCatalogActivity` at 2.0.0rc (line refs in the source); for checkouts with develop's
paging styles (`c3b16da`: develop, fork) `build_wire.sh` sets `WIRE_PAGING_STYLES`, and
`{limit}`/`{offset}` are filled in the way develop does. `sleep`/`drain` call the
real `pluginevents::emit`/`drain`. It does not mirror `download.open`.

```sh
python3 e2e/wire/wire.py --base-url http://127.0.0.1:8765 --token <device token> --fresh catalog
python3 e2e/wire/wire.py --base-url ... --token ... download <item id>
python3 e2e/wire/wire.py --base-url ... --token ... sleep        # then: drain
```

It prints JSON: parsed rows, download result and HTTP status, every request the device
wrote (start line, headers, body size) and the files on the card. The SD folder
(`--sd`, default `$WORK/wire/sd`) persists so the outbox carries over between calls.
`--bundle app.zip` installs a real bundle instead of the §10 template.

## Scenarios (`scenarios/`, `run_e2e.py`)

| Id | Firmware | What it proves |
|---|---|---|
| S1 | 2.0.0rc, 3f387a9, develop, fork | Home > Plugins > Send to X3 lists the catalog (`page=1&limit=9&offset=0` on develop, `offset={offset}` before it; bearer token) with **Get 2 new** first; Confirm lands `/Inbox/<row title>.epub` (one 2-chapter EPUB, fetched with `?confirm=1`) and queues `book.downloaded`; until the report the first row reads **Nothing new right now** (the bundle awaits it and is not offered again); after the restart, opening the plugin POSTs the saved path to `/device/v1/fetched` before the list loads, which counts the items; two single fetches land `/Inbox/<title>.epub` byte-identical to what was served; an unsafe title (`Wait... what? A/B: …`) is made firmware-safe by the backend; no Confirm opens the reader. On the fork the card's device.json has no `download.open` (a 0.2.0 bundle, any other catalog), so this is the patch's reworked stock path |
| S2 | 2.0.0rc, 3f387a9, develop, fork | sleep.enter delivery: 200 -> `/Inbox/<ts>-<ev>.epub` (2-chapter digest); 404 -> nothing written, line kept; the next drain retries the same ev/ts and lands the new item |
| S3 | 2.0.0rc, 1.6.5, develop, fork | File Transfer > Join a Network starts the firmware's web server; the backend's LAN push finds it, makes sure `/Inbox` exists, uploads one file per item and writes `<title> (2).<ext>` next to an existing different file instead of failing; run with `delivery_format` epub (the app default) and txt |
| S4 | 2.0.0rc, 1.6.5, develop, fork | OPDS browse with HTTP Basic; the first entry, **Get 2 new** (no author), downloads as `/Inbox/<title>.epub`, one EPUB with both items, and no single entry is downloaded |
| S5 | 2.0.0rc, 1.6.5, develop, fork | The served EPUB and TXT open in the real reader; pages 1 and 2 render (screenshots) with no firmware errors |
| S6 | fork | Confirm opens the book (`download.open`), with the real restart: **Get 2 new** downloads with `?confirm=1`, lands byte for byte, `POST /device/v1/fetched` (the saved path) goes out before the relaunch, and the second boot starts in `EpubReader` with that file (no Boot, no Home; RTC flags intact); state.json and recent.json name it, page 1 is pixel-identical to the same file opened through Browse Files, DOWN turns the page; then the first row reads **Nothing new right now**. A single row downloads, reports and opens the same way, and DOWN saves page 2; the same row again sends no request at all and still opens ("Already on the card") where you left off: the reader loads the saved position, `progress.bin` is unchanged and the page matches the first run's page 2, not page 1; **Nothing new right now** (a 404) shows the error screen with no restart, no file and state.json unchanged |
| W1 | 2.0.0rc, 3f387a9, develop, fork | Wire harness: exact request bytes (the browse line per firmware: `?page=1&limit=9&offset={offset}`, on develop `offset=0`; headers, User-Agents), download, the **Get 1 new** row, sleep delivery (with `auto_delivery` on), 404 keeps the line, drain retries it; with the default plugin, a download queues `book.downloaded` and the drain POSTs the saved path to `/device/v1/fetched`, which only then marks the item |

Backends (`scenarios/backends.py`, `--backend`):

- `mock`: `e2e/mock_backend.py`, a standard-library stand-in implementing the §7
  device contract (ingest, catalog paging with the **Get N new** row: from `offset`
  when it is a number, else `(page-1)*limit` for `limit=8`, else `(page-1)*(limit-1)`;
  item and bundle downloads with `?confirm=1` and `POST /device/v1/fetched`, idempotent
  `/deliver`, OPDS with Basic, LAN push worker). `--empty-deliver-status 204` reproduces the
  empty-file regression on purpose.
- `local`: `python -m sendtox3` with a temp `SENDTOX3_DATA_DIR`, an `options.json`
  written by the harness (port, tokens, `server_host` = the tap, `device_host` for LAN
  push, `push_poll_seconds: 1`), bound to 127.0.0.1 via `SENDTOX3_BIND`.
- `docker`: the app image (`--docker-image`, default `send_to_x3:e2e`), published on
  127.0.0.1 only; LAN push reaches the simulator via `host.docker.internal`.

All backends are seeded the same way, through `POST /api/v1/ingest`. The plugin
bundle comes from the app's own `sendtox3.plugin_bundle` when importable (`--bundle
app|spec|auto`); `sim/sd.py` `spec_device_json` is the same document (browse URL
`?page={page}&limit={limit}&offset={offset}`, `download.open: true`), and a unit test
keeps the two equal.

## Firmware findings the harness surfaced

- **develop `c3b16da` changed catalog paging.** With a `?page={page}&limit={limit}`
  template it sends `limit` = page size (8) and expects pages of exactly that size, so
  the old server math repeated a row on every page after the first. The device.json now
  adds `&offset={offset}`: develop then sends `limit=9&offset=(page-1)*8` (one lookahead
  row, as before), and 2.0.0rc/3f387a9 send the literal `{offset}`, which the server
  ignores. Through the simulator the literal arrives as `offset=offset`: the shim fetches
  with the curl CLI, which globs `{...}` (S1 allows for it; W1 checks the real bytes).
- **2.0.0rc cannot deliver at sleep right after a boot.** `deliverSleepPluginEvents`
  (`src/main.cpp:405`) looks up the saved Wi-Fi credential without loading
  `wifi.json`; only `WifiSelectionActivity` loads it (`:97`). Since every wake is a
  reboot, a plain wake -> read -> sleep cycle queues `sleep.enter` and sends nothing.
  The queued line is delivered at the next Wi-Fi join (opening Plugins > Send to X3,
  OPDS, File Transfer, KOReader sync), or at a sleep taken while Wi-Fi is up (e.g. from
  the catalog). Fixed upstream after the tag by `facaa87` ("Load WiFi settings from
  file before sleep mode", 2026-10-23); S2 runs on develop `3f387a9` too to show the
  true zero-tap path.
- **2.x OPDS sends Basic only after a 401.** The SDK's `OpdsClient` requests without
  credentials, re-probes the 401 for an OPDS auth document, then latches Basic
  (`freeink-sdk/libs/network/Opds/src/OpdsClient.cpp:64-110,164-250`). A
  password-protected feed must answer 401 (a non-JSON or Basic-offering body) when the
  header is missing. 1.6.5 sends Basic up front.
- 2.x renders TXT through the EPUB engine and logs `[SCT] Failed to load CSS from
  cache` on every TXT open (harmless; `lib/Epub/Epub/Section.cpp:433`).

## Where the simulator differs from a device

- HTTP goes through host `curl` (adds `Accept: */*`, no keep-alive, verifies TLS,
  globs `{...}` in URLs); the wire harness covers the device's own client. Only
  GET/POST/PUT exist.
- The fork's simulator reports version 2.0.0 (platformio.ini); the device build
  reports `2.0.0-sx3+<hash>`.
- `ESP.restart()` is a no-op, so the firmware's "silent restart" after leaving a
  network screen continues in-process; scenarios never rely on state across it (each
  boot is a new process), except S6, which runs the fork's simulator with the restart
  patch and `restart="exec"`. Deep-sleep wake relaunches the process (`execvp`).
- The heap is a fake 1 MiB (the X3 has about 380 KB), so nothing here shows whether a
  restart is needed for memory; the restart exists on the device to defragment it.
- Wi-Fi joins instantly (`CROSSPOINT_SIM_WIFI_CONNECT=fail|no-ssid|timeout` to vary),
  battery is always 100%, the heap size is set by `CROSSPOINT_SIM_FREE_HEAP`, UDP
  discovery is a stub, the web server binds 127.0.0.1 only.
- Never set `CROSSPOINT_SIM_HTTP_MOCK_ROOT` (it answers any URL whose basename
  matches a local file); `run_sim` refuses it.

## Unit tests

`tests/test_e2e_harness.py` covers the SD builder, the firmware profiles (develop and
fork, the browse query each sends), navigation key sequences, natural sort, BMP/PNG,
the wire tap's HTTP parsing, the mock backend's contract (including the paging guard),
the spec device.json against the agreed contract and the app's own, the backend process
wrapper, wire-output parsing, the runner (with fake programs, one of which relaunches
itself like the patched simulator: boot splitting, the after-restart schedule), the
simulator patch against the runner's protocol, `build_sim.sh`'s pins, the build
provenance check (a stale fork simulator is an error), what `--build` runs and the
simulator worktree `build_wire.sh` picks. No simulator
or network needed. With `SENDTOX3_E2E_SIM=1` and built simulators it also runs S1, S3
(1.6.5), W1 and S6 (fork) through `run_e2e.py`.
