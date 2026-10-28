# Testing Send to X3

This page lists every way Send to X3 is checked, from fast unit tests to a full
Home Assistant install, and how to run each one. It ends with what the automated
checks cannot cover, and a checklist for trying everything on your real Raspberry Pi,
X3, phones and mailbox.

You do not need to read the code to follow it. Every command is meant to be copied
into Terminal as it is; replace anything in `<angle brackets>` with your own value.

## The levels at a glance

| Level | What it proves | Needs | Time | Command |
|---|---|---|---|---|
| 1. Unit and integration tests | Every module on its own, and whole flows through the real app with fake web sites, fake mail servers, a fake Home Assistant and a fake X3 | Python 3.14 | about 1 min | `scripts/check_all.sh` |
| 2. Docker tests | The app image builds and starts the way Home Assistant runs it; every generated EPUB passes W3C epubcheck; email works against a real IMAP server; the Android import file loads in HTTP Shortcuts' own code | Docker Desktop | about 1.5 min once images are cached | `scripts/check_all.sh --docker` |
| 3. Firmware host harnesses | CrossPoint's own EPUB, TXT, OPDS and file-name code, compiled on the Mac, accepts what the app produces (1.6.5, 2.0.0rc and develop `3f387a9`) | Xcode Command Line Tools, SDL2 (the build step is shared with level 4) | about 1.5 min plus the first build | `scripts/check_all.sh --firmware` |
| 3b. The Send to X3 firmware | CrossPoint's own host tests for the plugin catalog code the Send to X3 patch touches (opening books, paging, manifests), then the X3 firmware build itself | Docker Desktop | a few minutes; 20 to 40 min for the first build | `firmware/build.sh --tests-only`, `firmware/build.sh` |
| 4. Simulator end-to-end | The whole CrossPoint firmware, running in a simulator, talks to the real app: plugin catalog, sleep delivery, LAN push, OPDS, opening the files | SDL2, Docker Desktop | about 4 min per backend plus the first build | `scripts/check_all.sh --e2e` |
| 5. Home Assistant | Home Assistant's Supervisor accepts the app's configuration; a real Supervisor builds, installs and starts it | Network; Docker Desktop for the install test | about 1 min (a few minutes the first time); 5 to 15 min for the install test | `scripts/check_all.sh --supervisor`, `e2e/ha/devcontainer_install.sh` |
| 6. Manual acceptance | Your real Pi, X3, iPhone, Android phone and Gmail | The devices | about 2 hours | [checklist below](#manual-acceptance-checklist-on-your-real-devices) |

`scripts/check_all.sh --all` runs levels 1 to 5 except level 3b and the Home Assistant
install test, which are started on their own.

## Latest measured results

These are the most recent runs on the development Mac (Apple Silicon, 2026-10-28),
all on version 0.3.0 and the Send to X3 firmware patch (after one review round), with
`scripts/check_all.sh --all` plus `e2e/ha/devcontainer_install.sh`.

| Level | Result |
|---|---|
| 1. Unit and integration tests | 2480 passed, 36 skipped (about 77 s) |
| 2. Docker tests (image build and smoke, epubcheck, GreenMail IMAP, Android import) | 13 passed, 3 skipped (OCR tests that need tesseract on the host; OCR runs in the image) |
| 3. Firmware host harnesses | 46 passed, against CrossPoint 1.6.5, 2.0.0rc and develop `3f387a9` |
| 4. Simulator end-to-end, real app (`python -m sendtox3`) | 25 of 25 scenario runs passed, 563 of 563 checks (2.0.0rc, 1.6.5, `3f387a9`, develop `8f4d035` and the fork) |
| 4. Simulator end-to-end, real app image (docker) | 16 of 16 scenario runs passed, 353 of 353 checks (2.0.0rc, 1.6.5 and the fork) |
| 5. Supervisor validation | 58 of 58 checks passed, 27 form fields |
| 5. Install in a real Supervisor | Passed (devcontainer `5-apps`, stable channel): build, install, start, `/healthz`, 401 without a token, paste form and 20 MB upload through ingress, Companion app share |
| Send to X3 firmware (`firmware/build.sh`) | Built `2.0.0-sx3+142f6b6` (5,497,008 bytes, 83.7% of the app slot); 25 of 25 plugin host tests passed; `esptool image_info`: ESP32-C3, checksum and SHA-256 valid |

Re-run levels 2 to 5 with `scripts/check_all.sh --all` and
`e2e/ha/devcontainer_install.sh` before a release.

Skipped tests are expected in level 1: they are the tests that need Docker, the
internet, a firmware checkout, a built simulator or your iCloud identity. Levels 2 to 4
run most of them; the rest are the optional extras listed under level 1.

---

## One-time setup on a Mac

Do this once. It takes about 20 minutes, mostly downloads.

1. **Apple's command line tools** (git, clang). In Terminal:

   ```sh
   xcode-select --install
   ```

   A dialog opens; choose **Install**. If it says the tools are already installed,
   that is fine.

2. **Homebrew packages**: Python 3.14 for the app and tests, SDL2 for the X3
   simulator, and Tesseract for the optional OCR tests.

   ```sh
   brew install python@3.14 sdl2 tesseract
   ```

3. **Docker Desktop**: install it from docker.com, open it once and wait until it
   says it is running. Levels 2, 4 (docker backend) and 5 (install test) need it.

4. **A Python environment for this project.** Keep it outside the Google Drive
   folder (syncing thousands of small files is slow and can corrupt it):

   ```sh
   cd "$HOME/My Drive/Claude/Projects/xteink"
   python3.14 -m venv ~/.venvs/sendtox3
   ~/.venvs/sendtox3/bin/pip install -r requirements-dev.txt
   ```

   The last line should end with "Successfully installed ...".

5. **Every time you open a new Terminal window** to run tests:

   ```sh
   cd "$HOME/My Drive/Claude/Projects/xteink"
   source ~/.venvs/sendtox3/bin/activate
   ```

   The prompt now starts with `(sendtox3)`. All commands below assume this.

Two rules that avoid most problems:

- The project folder's path contains a space (`My Drive`). Always keep the quotes
  around it, as above.
- Builds and test runs write to a work folder, `~/.cache/sendtox3-e2e` by default
  (change it with `export SENDTOX3_E2E_WORK=<folder>`). That folder must not contain
  spaces and must not be inside Google Drive or iCloud.

## Run everything: `scripts/check_all.sh`

[`scripts/check_all.sh`](../scripts/check_all.sh) runs the levels in order and stops
at the first failure.

```sh
scripts/check_all.sh                 # level 1 only
scripts/check_all.sh --docker        # + level 2
scripts/check_all.sh --firmware      # + level 3 (builds the firmware checkouts first)
scripts/check_all.sh --e2e           # + level 4 (builds the simulators first)
scripts/check_all.sh --supervisor    # + level 5, Supervisor validation
scripts/check_all.sh --all           # all of the above
scripts/check_all.sh --help
```

Options can be combined (`--docker --supervisor`). Level 1 always runs first.

**What you should see:** each stage starts with a line like
`==== 1. unit + integration tests ====` and ends with a pytest summary such as
`2155 passed, 35 skipped`, or a scenario table. The last line of a successful run is:

```
==== all requested checks passed ====
```

If anything fails, the script stops there with the failing test's output and a
non-zero exit code; nothing after it runs.

`PYTHON=<interpreter>` picks the Python to use (default `python3`, which is the
project environment once it is activated).

---

## Level 1: unit and integration tests

```sh
python -m pytest -q
```

(`scripts/check_all.sh` with no options runs the same tests.) Expect a line like
`2155 passed, 35 skipped` at the end. No network, Docker or X3 is used: web pages
come from local fixture servers in the tests, and the X3, Home Assistant and the mail
server are fakes (`tests/fakes/`).

What is covered, by area:

| Area | Test files |
|---|---|
| Sharing (`POST /api/v1/ingest`): authentication, body formats, size limits, error replies, the 25-second "still processing" reply (also when the app shuts down during the wait), the 503 "Not saved" reply when the disk or database fails | `test_web_ingest.py`, `test_ingest.py`, `test_fix_E_main.py`, `test_fix2_E_main.py` |
| How a share is classified: a lone link, a link with a short comment (fetched, with the comment kept as a note), several links (one item each), a Safari selection with its page (the selection, with the page as source), plain text (a note); duplicates; titles (file-safe title and the headline in the book); failures that still keep the link | `test_ingest.py`, `test_extract_text.py`, `test_fix_A_ingest.py`, `test_fix2_A_ingest.py`, `integration/test_flow_share_to_x3.py` |
| Shares interrupted by a restart are redone at the next start (the spool) | `test_fix_A_store.py`, `test_fix_A_ingest.py`, `test_fix_E_main.py` |
| Article extraction, the safe fetcher (blocks local addresses, fetches over IPv4 only, limits size, time and redirects), site-specific readers (GitHub, Wikipedia, Substack, Bluesky, X) | `test_extract_html.py`, `test_extract_fetch.py`, `test_extract_sites.py`, `test_fix_B_*.py`, `test_fix2_B_html.py`, `test_fix2_B_fetch.py` |
| PDFs: generated PDFs, real documents in `tests/fixtures/pdfs`, OCR of scanned pages, the isolated worker (killed when the budget runs out, minimal environment, unprivileged account) | `test_pdf.py`, `test_pdf_fixtures.py`, `test_pdf_ocr.py`, `test_fix_D_pdf_worker.py`, `test_fix2_D_pdf_root.py` |
| EPUB and TXT output, firmware-safe titles, uploaded EPUBs (size and time limits) | `test_render_epub.py`, `test_render_txt.py`, `test_names.py`, `test_render_validation.py` (corpus part), `test_fix_D_epub_urls.py`, `test_fix2_D_epub.py` |
| Email: parsing, the acceptance filter (plus-tag, allowed senders, DMARC/DKIM), refusing all mail when neither `allowed_senders` nor `imap_plus_tag` is set or when `'*'` is used in a plain INBOX, the IMAP worker against a fake server (refused mail left unread, the first check of an untagged INBOX, the attempt counter) | `test_email.py`, `test_email_accept.py`, `test_imap_worker.py`, `test_fix_C_*.py`, `test_fix2_C_email.py`, `test_fix2_C_wildcard.py`, `test_fix2_C_imap.py` |
| Home Assistant Companion app shares (`mobile_app.share`) against a fake Home Assistant | `test_ha_share.py`, `test_fix_G_ha_share.py` |
| Plugin catalog paging as each firmware does it: 2.0.0rc and develop `3f387a9` (`limit=9` and the literal text `{offset}`), develop `8f4d035` and the Send to X3 firmware (`limit=9` and a numeric `offset`), and the same with a plugin installed by 0.2.0 (`limit=8`, no `offset`), for 0, 7, 8, 9, 15 and 20 items: every row exactly once, the first row on page 1 only | `test_web_device.py`, `test_delivery.py` |
| X3 delivery: the "Get N new" row (bundle contents, stable ids and bytes, file names that never repeat, replaced bundles, "Nothing new right now", TXT bundles, a download in the other format, a failing build that still lists the items, the X3's confirmation of saved files, a dropped download marking nothing, a read-all-then-close counting, the TCP-acknowledgement decision, items awaiting the report kept out of new bundles and sleep delivery and re-offered after the grace period under a new name, other plugins' reports ignored, the queue's "not confirmed yet"), plugin catalog paging with that row, downloads, sleep delivery (repeat requests get the same file, digests, size caps, and "nothing new" when `auto_delivery` is off, the default), OPDS feed with its first entry | `test_fetch_bundle.py`, `test_delivery.py`, `test_web_device.py`, `test_opds.py`, `test_fix_A_delivery.py` |
| LAN push and plugin install against a fake CrossPoint File Transfer server (1.6.5 and 2.0), including the install log line that says whether **Confirm** opens the book (`catalog.open_after_download` in `/api/status`) and `download.open` in every plugin | `test_lan_push.py`, `test_device_client.py`, `test_plugin_bundle.py`, `test_fix_E_plugin_bundle.py`, `test_fix2_E_plugin_bundle.py` |
| The admin web UI: Home Assistant ingress, token sign-in, CSRF protection, forms, the Setup page (the Android HTTP Shortcuts file filled in with the server address and token, **New device token**, address warnings), plugin bundle download and install, warnings | `test_web_admin.py`, `test_fix_E_web.py`, `test_fix2_E_web.py` |
| Start-up, worker restarts, daily clean-up, `--show-tokens`, retrying the server address detection | `test_web_main.py`, `test_fix_E_main.py` |
| Database and files (owner-only permissions, no clash between identical uploads) | `test_store.py`, `test_fix_A_store.py`, `test_fix2_A_store.py` |
| Packaging: `config.yaml` mirrors the app's options exactly, translations and `DOCS.md` cover every option, Dockerfile and `run.sh` rules, icons, pinned GitHub Actions; the app's own docs checked against the code | `test_packaging.py`, `test_fix2_H_docs.py` |
| Phone shortcuts: the iOS Shortcut generator (structure and logic, run by an interpreter of its actions), the Android import file, both against the real share API | `test_shortcuts.py`, `test_shortcuts_ingest.py`, `test_phone.py`, `test_fix_G_shortcuts.py`, `test_fix2_G_shortcuts.py` |
| The iOS Shortcut as Apple's own WorkflowKit reads it (runs automatically on a Mac) | `test_shortcuts_macos.py`, `test_fix2_G_shortcuts_macos.py` |
| The simulator test harness itself (SD card builder, button sequences, screenshots, the wire tap, the mock backend) | `test_e2e_harness.py` |

The `test_fix_*` and `test_fix2_*` files hold the regression tests of the two
review-fix rounds; each fails on the code before its fix.

### Optional extras inside level 1

These are skipped unless you turn them on:

| Extra | Command | Notes |
|---|---|---|
| Real web sites (a handful of public pages) | `SENDTOX3_NETWORK_TESTS=1 python -m pytest -q tests/test_extract_network.py` | Needs internet; sites change, so an occasional failure here is not necessarily a bug |
| Sign the iOS Shortcut with your iCloud identity | `SENDTOX3_SHORTCUTS_SIGN=1 python -m pytest -q tests/test_shortcuts_macos.py` | Mac signed in to iCloud; signs locally (`people-who-know-me`), nothing is uploaded |
| OCR of scanned PDFs | runs automatically when `tesseract` is installed | Skipped otherwise |
| Three simulator scenarios through pytest | `SENDTOX3_E2E_SIM=1 python -m pytest -q tests/test_e2e_harness.py` | Runs S1, S3 (1.6.5) and W1; build the simulators first (level 4) |

---

## Level 2: docker tests

Start Docker Desktop, then:

```sh
scripts/check_all.sh --docker
```

or only this level:

```sh
SENDTOX3_DOCKER_TESTS=1 python -m pytest -q -m docker
```

The first run downloads base images (a few hundred MB). Every container binds to
127.0.0.1 only.

| Test | What it proves |
|---|---|
| `tests/test_packaging_docker.py` | Builds the app image for this Mac's architecture (arm64 on Apple Silicon) exactly as Home Assistant would, then: the Python version and every runtime library import; `tesseract` is present for OCR; the image labels; `/data/options.json` is read; the server answers `GET /healthz`; a crash stops the container (so Home Assistant's watchdog can restart it); no Python bytecode is written at run time; the image stays within its size budget |
| `tests/test_render_validation.py -k epubcheck` | Every EPUB in a generated test corpus passes W3C epubcheck 5.4.0 with `--failonwarnings`: 0 fatal errors, 0 errors, 0 warnings. The epubcheck image ([`e2e/epubcheck/`](../e2e/epubcheck/README.md)) runs with no network |
| `tests/test_imap_greenmail.py` | The email worker against GreenMail, a real IMAP server, over IMAPS: new mail is picked up within seconds (IMAP IDLE, not the 60-second poll), processed exactly once, marked read and moved to the processed folder; refused mail (failed DMARC) is not processed and stays unread in place; after the mail server restarts, the worker reconnects and keeps delivering |
| `tests/test_shortcuts_android_import.py` | The Android file goes through HTTP Shortcuts' own import and placeholder code (compiled from its source at version 4.9.0): the app accepts it, it is a text share target, every placeholder resolves, the rendered requests are right, and a broken file is refused |

The Android import test also needs the internet: its first run downloads about 90 MB
of HTTP Shortcuts source and build tools. `scripts/check_all.sh --docker` turns on
network tests for this stage (`-m docker` still keeps the network-only tests out), so
it runs there. On its own:

```sh
SENDTOX3_DOCKER_TESTS=1 SENDTOX3_NETWORK_TESTS=1 python -m pytest -q tests/test_shortcuts_android_import.py
```

To check the other architecture under emulation (slow):

```sh
SENDTOX3_DOCKER_TESTS=1 SENDTOX3_DOCKER_PLATFORM=linux/amd64 python -m pytest -q tests/test_packaging_docker.py
```

---

## Level 3: firmware host harnesses

These compile the **real, unmodified** CrossPoint code that decides whether a file
works on the X3, and run it on the Mac against what the app produces. Details:
[`e2e/fwharness/README.md`](../e2e/fwharness/README.md).

```sh
scripts/check_all.sh --firmware
```

The first run clones CrossPoint and checks out three versions into
`~/.cache/sendtox3-e2e/fw/`: tag **1.6.5** (current stable), **2.0.0rc** (the
2026-10-19 prerelease with plugins) and develop commit **`3f387a9`** (2026-10-25,
which contains the sleep Wi-Fi fix described in level 4). It then builds the
harnesses for each and runs:

| Test | What it proves, on each firmware version |
|---|---|
| `test_firmware_epub_corpus` | Every EPUB in the test corpus opens: the title and author reach the Library, every chapter (at least one per item; long articles are split into several) is in the table of contents with the item's title, parses and lays out into pages with the real built-in fonts, and no long web address is printed in the text |
| `test_firmware_epub_layout_details` | Lists keep one item per line with their markers; the title, byline and "Source:" lines are where they should be; code blocks keep their indentation; the language reaches the reader (hyphenation); special characters survive |
| `test_firmware_txt_corpus` | Every TXT opens in the TXT reader (1.6.5) or the EPUB engine that 2.x uses for TXT, with the file name as its title, at least one page and no control characters |
| `test_firmware_txt_long_article_is_fast` | A long article indexes within the time budget (20 s on the Mac, `SENDTOX3_TXT_BUDGET`), thanks to the app splitting long paragraphs |
| `test_firmware_safe_titles_survive_real_sanitizers` | Titles the app produces come out of CrossPoint's file-name sanitizers unchanged (so the file on the card has the title you saw), and 2.x accepts them as plugin download names |
| `test_firmware_python_ports_match_cpp` | The app's Python copies of those sanitizers behave exactly like the C++ code |
| `test_opds.py::test_feed_parses_in_crosspoint_opds_parser` | The OPDS feed parses in CrossPoint 1.6.5's real OPDS parser |
| `integration/test_flow_share_to_x3.py` (OPDS step) | A feed produced by a full share flow parses in the same parser with the expected rows |

Not simulated here: the X3's screen edge insets, right-to-left text, fonts loaded from
the SD card, images, encrypted EPUBs, the device's memory limits and its speed (times
are Mac times; the X3 is much slower).

To run only this level once the checkouts exist:

```sh
W=$HOME/.cache/sendtox3-e2e
export FWH_WORK="$W/fwharness"
OPDS_BIN=$(CP_REPO="$W/fw/1.6.5" e2e/fwharness/opds/build.sh | tail -1)
SENDTOX3_FIRMWARE_TESTS=1 CP_REPO="$W/fw/1.6.5:$W/fw/2.0.0rc:$W/fw/3f387a9" SENDTOX3_OPDS_HARNESS="$OPDS_BIN" \
  python -m pytest -q tests/test_render_validation.py tests/test_opds.py tests/integration/test_flow_share_to_x3.py
```

---

## Level 3b: the Send to X3 firmware

The optional [Send to X3 firmware](FIRMWARE.md) is upstream CrossPoint at a pinned
commit plus one patch (`firmware/`; developer notes in
[`firmware/README.md`](../firmware/README.md)). Its checks run in Docker:

```sh
firmware/build.sh --tests-only     # the host tests only
firmware/build.sh                  # the host tests, then the firmware build
```

| Check | What it proves |
|---|---|
| The patch applies | `firmware/patches/` applies with `git am` on the commit in `firmware/UPSTREAM` |
| `PluginCatalogOpenTest` | Without `download.open` every row downloads; bundle rows and files the reader does not open keep the download screen; a book on the card opens without a download; a new book downloads, then opens; `catalog.open_after_download` is advertised |
| `PluginCatalogPagingTest`, `PluginManifestClassificationTest`, `PluginPickerLayoutTest` | Upstream's tests for the catalog code around the patch still pass |
| The build | `pio run -e sendtox3` builds `firmware/dist/crosspoint-2.0.0-sx3+<hash>-x3-x4.bin` and a `.size.txt` with the RAM and flash use |

**What you should see:** ctest's `100% tests passed`, and after a build the last lines
name the files in `firmware/dist/`. The first build downloads the toolchain into
Docker volumes (about 5 GB) and takes 20 to 40 minutes.

Not covered here: the firmware on a real X3 (see the checklist,
[section M](#m-the-send-to-x3-firmware-optional)).

---

## Level 4: the X3 simulator, end to end

This runs the **whole CrossPoint firmware** for the X3, compiled with
[crosspoint-simulator](https://github.com/crosspoint-reader/crosspoint-simulator), on
the Mac with no X3 attached. A folder plays the SD card, button presses are
scripted, and the simulator takes screenshots. The app under test is the real one.
Details: [`e2e/README.md`](../e2e/README.md).

```sh
scripts/check_all.sh --e2e
```

This builds five simulators the first time (1.6.5, 2.0.0rc, develop `3f387a9`, develop
and the Send to X3 firmware; about 1.5 minutes each) and the wire harness for the four
2.x builds, then runs:

1. every scenario on every firmware it supports against the app started as
   `python -m sendtox3` (`e2e/run_e2e.py -f all -b local`);
2. every scenario on the two releases (2.0.0rc and 1.6.5) and the Send to X3 firmware
   against the app's Docker image (`e2e/run_e2e.py -f releases,fork -b docker`).
   Docker Desktop must be running.

The number of runs and checks for each is in
[Latest measured results](#latest-measured-results).

**What you should see** for each backend is a table like:

```
S1   2.0.0rc  PASS    33.7s  27/27 checks
S2   2.0.0rc  PASS    67.7s  28/28 checks
...
report: /Users/<you>/.cache/sendtox3-e2e/runs/<date-time>/report.json
```

### The scenarios

| Id | Firmware | What it proves |
|---|---|---|
| **S1** catalog | 2.0.0rc, `3f387a9`, develop, fork (with `download.open` removed, so the fork's stock path is covered) | The browse request is what that build sends (`limit=9`; `offset={offset}` as literal text on 2.0.0rc and `3f387a9`, a number on develop). The installed plugin has no sleep delivery (the default), only the download report. On the X3, Home > Plugins > Send to X3 starts with **Get 2 new** (the request carries the device token); Confirm on it saves `/Inbox/Get 2 new · <date time>.epub`, one EPUB with both items, and queues `book.downloaded`. Until the report the first row reads **Nothing new right now** (awaiting the report, not offered again); after the restart, opening the plugin again POSTs the saved path before the list loads, which then counts the items. Selecting the entries below saves `/Inbox/<title>.epub`, byte for byte what the app served. A title full of characters the firmware refuses (`Wait... what? A/B: …`) is made safe by the app and still downloads |
| **S2** sleep delivery | 2.0.0rc, `3f387a9`, develop, fork | With the optional `auto_delivery` on, going to sleep fetches everything new as one EPUB, `/Inbox/<time>-<event>.epub` (a two-chapter digest for two items). With nothing new the app answers "nothing" (404), the X3 writes no file and keeps the request queued; the next attempt repeats the same request and receives the next new item. On 2.0.0rc it also proves that a sleep right after waking sends nothing (see the finding below); on `3f387a9` the plain wake, sleep cycle delivers with no taps |
| **S3** LAN push | 2.0.0rc, 1.6.5, develop, fork | With the X3 on Home > File Transfer > Join a Network, the app finds the X3's own web server, creates `/Inbox` if needed and uploads one file per item. When a different file with the same name is already on the card, it saves `<title> (2).<ext>` and leaves the old file alone. Run with EPUB (the default) and with TXT |
| **S4** OPDS | 2.0.0rc, 1.6.5, develop, fork | The X3's OPDS browser signs in with user `x3` and the device token, reads the feed and saves its first entry, **Get 2 new** (no author), as `/Inbox/Get 2 new · <date time>.epub`: one EPUB with both items, and no single entry downloaded |
| **S5** reading | 2.0.0rc, 1.6.5, develop, fork | The EPUB and TXT the app serves open in the real reader; pages 1 and 2 have text on them, page 2 differs from page 1, and the firmware logs no errors. Screenshots are saved for a human to look at |
| **W1** wire | 2.0.0rc, `3f387a9`, develop, fork | The X3's own HTTP code (not the simulator's stand-in) against the app: the exact bytes the X3 sends, catalog, download, the **Get 1 new** row (saved as `/Inbox/<row title>.epub`, then **Nothing new right now**), sleep delivery with `auto_delivery` on, a "nothing new" reply that keeps the request queued, and with the default plugin the confirmation (`?confirm=1`, then `POST /device/v1/fetched` with the saved path at the next drain; nothing is counted before it; a sleep wants no Wi-Fi) |
| **S6** open | fork | The Send to X3 firmware's **Open**: on **Get 2 new** the book downloads (`?confirm=1`), the X3 POSTs the saved path to `/device/v1/fetched` before it restarts, and the restart goes straight into the reader with that book (page 1 is the page **Browse Files** shows for it; a page turn works); afterwards the first row reads **Nothing new right now**. A single row downloads, reports and opens the same way (the test turns to page 2); the same row again opens with no request at all, on page 2 (same page image, the reader logs the saved position, `progress.bin` unchanged); **Nothing new right now** shows the error screen, with no restart and no file |

"develop" is CrossPoint develop `8f4d035` (2026-10-28) and "fork" the Send to X3
firmware built from it (`firmware/UPSTREAM` plus `firmware/patches/`) on a simulator
patched so that a restart relaunches the program like the X3's reboot; details in
[`e2e/README.md`](../e2e/README.md).

Checks never read text off the screen. They use the screens the firmware logs, other
firmware log lines, the files on the simulated SD card, the HTTP traffic recorded
byte for byte between the X3 and the app, and simple image measurements.

### Firmware findings from these runs

- **CrossPoint 2.0.0rc cannot deliver at sleep right after waking.** At sleep it looks
  up the saved Wi-Fi password without loading the Wi-Fi settings file, and every wake
  is a reboot. So a plain wake, read, sleep cycle only queues the request. It is sent
  the next time Wi-Fi is used: opening Home > Plugins > Send to X3, the OPDS browser,
  File Transfer or KOReader sync, or a sleep taken while Wi-Fi is still on. This is
  fixed upstream after the 2.0.0rc tag by develop commit `facaa87` (2026-10-23, "Load
  WiFi settings from file before sleep mode"). For truly zero-tap delivery, use a
  newer build or nightly that contains it. S2 runs on `3f387a9` to show that path.
- **2.x sends the OPDS password only after the server asks for it** (a 401 reply);
  1.6.5 sends it with every request. The app handles both.
- 2.x logs `[SCT] Failed to load CSS from cache` every time it opens a TXT file. It is
  harmless.

### Where to look after a run

Everything from a run stays under
`~/.cache/sendtox3-e2e/runs/<date-time>/<scenario>-<firmware>/`: the simulated SD card
(`sd/`), each simulator run's `serial.log` and `shots/*.png` (the screenshots), the
recorded HTTP exchanges, the app's log and data folder, and `report.json` one level up
with every check.

### Running parts of it

```sh
python e2e/run_e2e.py --list                       # the scenarios and their firmware
python e2e/run_e2e.py -s S2 -f 2.0.0rc -b local    # one scenario, one firmware, the real app
python e2e/run_e2e.py -f all -b local              # what check_all.sh runs first
docker build -t send_to_x3:e2e send_to_x3          # needed once before -b docker
python e2e/run_e2e.py -f releases,fork -b docker   # what check_all.sh runs second
python e2e/run_e2e.py                              # mock backend: a stand-in app, for working on the harness itself
```

`--build` runs `build_sim.sh` (and `build_wire.sh` where W1 runs) for every firmware
a selected scenario uses; each does nothing when its build is up to date. Without it, a
simulator built from older patches or another `firmware/UPSTREAM` makes the scenario an
error that says to rebuild. On a slow or busy Mac,
`SENDTOX3_E2E_SLOWDOWN=2` doubles every wait in the button scripts.

### Where the simulator differs from a real X3

- The simulator's HTTP goes through the Mac's `curl`. The wire harness (W1) covers the
  X3's own HTTP code instead.
- Wi-Fi joins instantly, the battery is always full, memory is a fake 1 MiB, and the
  X3's web server listens on the Mac only.
- On the stock simulators a restart of the firmware continues in the same process, and
  no scenario depends on it. The Send to X3 firmware's simulator relaunches the
  program like the X3's reboot, which S6 relies on. Waking from sleep starts a new
  process, like the real reboot.

---

## Level 5: Home Assistant

Details: [`e2e/ha/README.md`](../e2e/ha/README.md).

### Supervisor validation

```sh
scripts/check_all.sh --supervisor
```

or on its own:

```sh
WORK_DIR="$HOME/.cache/sendtox3-e2e/ha" e2e/ha/validate_config.sh
```

The first run downloads Home Assistant's Supervisor (pinned release 2026.10.4) and its
requirements. It then uses the Supervisor's own code to check `repository.yaml`, that
the app is discovered, `config.yaml`, the translations, and the configuration form.
It saves sample option sets the way the Home Assistant UI would (defaults, every
option, cleared text fields, an email and LAN push setup, numbers typed as text), and
reads each one back with the app's own option loader. Option sets that must be
refused (an unknown format, a retention of 0 days, out-of-range numbers, a text where
a list belongs) must be refused. It checks that a `port` option saved by an older
version is dropped (the app then listens on 8765), that the form shows 27 fields,
that secrets show as password fields and that the security rating is 7.

**What you should see:** one `PASS` line per check, then `58 passed, 0 failed`.

### Install in a real Supervisor (devcontainer)

This is the closest thing to installing on the Pi without touching the Pi. It runs
Home Assistant's official apps development container (Supervisor on the stable
channel, Home Assistant Core) in Docker Desktop and has that Supervisor build,
install and start the app from a copy of this repository.

```sh
WORK_DIR="$HOME/.cache/sendtox3-e2e/ha" e2e/ha/devcontainer_install.sh
```

Needs Docker Desktop with privileged containers allowed (the default), about 6 GB of
free disk and the internet. The first run takes 5 to 15 minutes, mostly downloading
Home Assistant. It checks, in order:

1. the app store lists `local_send_to_x3`, and the Supervisor builds and installs it;
2. it starts, and `/data/options.json` is what the Supervisor wrote;
3. `GET /healthz` answers on host port 8765 (the X3's path);
4. the admin page asks for a token when reached directly by another container;
5. real ingress (browser to Core to Supervisor to app): the web UI loads without a
   token, the paste form creates an item, and a 20 MB upload gets through;
6. a `mobile_app.share` event sent through Home Assistant Core becomes an item (the
   Companion app share path).

**What you should see** at the end:

```
==> PASS: the Supervisor built, installed and started local_send_to_x3; ingress and share work
```

To keep Home Assistant running afterwards and look around at http://127.0.0.1:7123,
put `KEEP=1` in front of the command. To remove it all later:

```sh
e2e/ha/devcontainer_install.sh --down
```

---

## What runs on GitHub (CI)

When the repository is on GitHub, two workflows run without anyone starting them:

- **Tests** (`.github/workflows/tests.yml`, every push to `main` and every pull
  request): level 1 on Ubuntu with Python 3.14; the Supervisor validation and a
  shell-script check; and the image tests from level 2
  (`tests/test_packaging_docker.py`) on both amd64 and aarch64 machines.
- **Build** (`.github/workflows/build.yml`): builds the aarch64 and amd64 images; on
  `main` it publishes them. See [DEVELOPMENT.md](DEVELOPMENT.md#release-process).

Levels 3, 4, the other docker tests and the install test only run on the Mac.

---

## What the automated checks do not cover

| Not covered | Why it matters | Covered instead by |
|---|---|---|
| A real X3 | The real device has less memory, a slower processor, real Wi-Fi (sleep delivery needs at least 20% battery and the network it last joined), real deep sleep, and screen edges the harness does not know | Checklist sections [C](#c-x3-with-crosspoint-2x-the-plugin) to [F](#f-reading-on-the-x3) |
| The Send to X3 firmware on a real X3 | Installing with SD Card Firmware Update, recovery mode and going back were worked out from the firmware source; opening books and the reboot into the reader ran only in the simulator (fake memory, a patched restart) | Section [M](#m-the-send-to-x3-firmware-optional) |
| A real iPhone running the Shortcut | Apple only lets its own apps run Shortcut actions; the tests check the file with Apple's WorkflowKit and run its logic in an interpreter, but never run it end to end | Section [G](#g-iphone) |
| A real Android phone with HTTP Shortcuts | The import code is tested, but not the app on a phone (share sheet, local network permission) | Section [H](#h-android) |
| Real Gmail | GreenMail stands in for it: Google's app passwords, labels, spam filtering and the DMARC results Google writes into each message are not tested | Section [J](#j-email-gmail) |
| Home Assistant OS on the Raspberry Pi | Building the image on the Pi, the Pi's network and ports, Samba, backups; the install test runs on a Mac | Section [A](#a-the-app-on-your-raspberry-pi) |
| The Companion app on a phone | The install test fires the same event the app sends, but not from a phone | Section [I](#i-home-assistant-companion-app) |
| Speed on the Pi | PDF and OCR times are measured on the Mac only; OCR is roughly 10 to 15 s per page on a Pi | Section [K](#k-pdfs-and-slow-shares) |
| Most real web sites | Only a few public sites are tried, and only when you turn that on | Your own everyday sharing |

---

## Manual acceptance checklist on your real devices

Work through the sections that apply to you, in order. Each step says what to do and
what you should see. Tick the box when it matches. If something does not match, note
the step number, take a photo of the X3 screen or a screenshot, and copy the app's log
(Home Assistant: **Settings > Apps > Send to X3 > Log**).

Throughout, `<Pi IP>` is the address of your Home Assistant machine, for example
`192.168.1.50`. The app's Setup page shows the address it detected.

Keep tokens private: do not paste them into chats, issues or screenshots you share.

### A. The app on your Raspberry Pi

Pick one way to install.

**Before the repository is published** (install it as a local app):

1. In Home Assistant open **Settings > Apps > Install app**, search for **Samba
   share**, install it, set a user name and password on its **Configuration** tab,
   and start it.
2. On the Mac, in Finder choose **Go > Connect to Server**, enter
   `smb://<Pi IP>` and sign in with that user name and password. Open the
   **local_apps** share.
3. Copy the `send_to_x3` folder from this repository into **local_apps** (the folder
   itself, so you end up with `local_apps/send_to_x3/config.yaml`).
4. Back in Home Assistant: **Settings > Apps > Install app**, open the menu at the top
   right (three dots) and choose **Check for updates**. **Send to X3** appears under
   **Local apps**.

Note: the Samba share and local-apps steps come from Home Assistant's documentation
for Home Assistant 2026.2 and later; they were not repeated on your Pi for this page.

**After the repository is published:** **Settings > Apps > Install app**, menu at the
top right > **Repositories**, paste the repository's Git URL, select **Add**, then
find **Send to X3** in the store.

Then:

- [ ] A1. Select **Send to X3**, then **Install**. Without a prebuilt image the Pi
  builds it, which takes several minutes. You should then see **Start**.
- [ ] A2. Open the **Configuration** tab. There is **no** "Container port" option.
  The **Network** section lists `8765/tcp` with the host port `8765`. (If another app
  already uses 8765 on your Pi, change the host port there and set **Server address
  for the X3** to `<Pi IP>:<new port>`.)
- [ ] A3. On the **Info** tab select **Start**, and turn on **Watchdog**. The **Log**
  tab shows `Send to X3 0.1.0 listening on 0.0.0.0:8765 (data in /data, 0 items)` and
  `server address for the X3 and phones: http://<Pi IP>:8765`.
- [ ] A4. On the Mac, open `http://<Pi IP>:8765/healthz` in a browser. You should see a
  short line starting with `{"ok":true`, for example `{"ok":true,"version":"0.1.0"}`.
- [ ] A5. In Home Assistant select **Open Web UI**. You should see the queue page with
  **Send something**, **Channels** and **Queue**, without being asked for a token.
- [ ] A6. Select **Setup**. The first line says the X3 and your phone reach the app at
  `http://<Pi IP>:8765` "(detected from Home Assistant)". If it shows an address
  starting with `172.30.` or a warning, set **Server address for the X3** to your
  `<Pi IP>` on the Configuration tab, save and restart the app.
- [ ] A7. Your Calibre apps still start and work as before.

### B. A first item from the web UI

- [ ] B1. On the queue page, under **Send something**, paste a link to a news article
  into **Text or a link** and select **Send to X3**. Within about 20 seconds the item
  appears in **Queue** with the article's title, status `ready` and `waiting` under
  **On the X3**.
- [ ] B2. Select the title. The preview shows the article text without images, menus
  or ads, and **EPUB** and **TXT** download links. Download the EPUB and open it on the
  Mac (Books): it has the title, the text and a "Source:" line.
- [ ] B3. Under **Or upload a file**, upload a short PDF and select **Upload**. A new
  item with the PDF's text appears.

### C. X3 with CrossPoint 2.x: the plugin

For an X3 on 2.0.0rc or a newer 2.x build.

- [ ] C1. On the X3: **Home > Settings > Connectivity > Plugins** and choose
  **Enabled** (plugins are off by default).
- [ ] C2. On the X3: **Home > File Transfer > Join a Network**. Keep that screen open
  and note the address it shows after "or http://".
- [ ] C3. In the web UI, **Setup**, section "X3 with CrossPoint 2.x", **Install over
  Wi-Fi**: enter the X3's address and select **Install plugin on my X3 now**. A log
  appears under the button and ends without an error. Then leave File Transfer on the
  X3.

  (Instead of C2 and C3 you can select **Download SD-card plugin bundle (zip)** and
  unzip it at the top of the X3's SD card, as the Setup page explains.)
- [ ] C4. On the X3: **Home > Plugins > Send to X3**. The first row reads like **Get 2
  new · Oct 26 14.05**, with the new titles below it. Under it are your items, newest
  first; each subtitle reads like `New · 1,234 words · example.com`.
- [ ] C5. Press **Confirm** on the first row. You should see **Download complete**. In
  the web UI the new items show **sent ..., not confirmed yet**. Go back to Home and
  open the plugin again (this joins Wi-Fi, and the X3 reports the saved file): the
  first row reads **Nothing new right now**, and the web UI shows `fetch`. Pressing
  Confirm on **Nothing new right now** shows **Download failed** and nothing changes.
- [ ] C5b. **A cancelled fetch is offered again.** Share a long PDF or several items,
  press Confirm on **Get N new** and press **Back** while it downloads. Go back to Home
  and open the plugin again: the same items are the first row again, as **Get N new ·
  ... (2)**, and the web UI shows them as **waiting**.
- [ ] C5c. **No second copy while the report is pending.** Fetch **Get 1 new**, press
  **Back** once (to the plugin list) and open **Send to X3** again: the first row reads
  **Nothing new right now** ("Your last fetch ... is confirmed the next time the X3
  joins Wi-Fi"). Go back to Home and open it again: the web UI now shows `fetch`.
- [ ] C6. On the X3: **Home > Library** has the new book (one new item: its own title;
  several: `Inbox: 2 items (...)` with one chapter each). **Browse Files > Inbox** shows
  the file `Get 2 new · Oct 26 14.05.epub`. Open it (see section F).
- [ ] C6b. Back in the plugin, select a single item below the first row. **Download
  complete**; the file in **Inbox** is named after the item's title and the web UI
  shows `catalog` for it.
- [ ] C6c. **Nothing arrives by itself.** Share a new item, then put the X3 to sleep
  and wake it. No **Loading...** popup at sleep, and nothing new in **Inbox**; the item
  still shows `waiting` until you use **Get 1 new**.

The rest of section C is only for the optional delivery at sleep. First turn it on:
Configuration tab, **Also deliver when the X3 sleeps** on, **Save**, restart the app,
and reinstall the plugin (C2 and C3).

- [ ] C7. **Sleep delivery on 2.0.0rc.** Share a new item (for example with B1). On
  the X3, open **Home > Plugins > Send to X3** (this joins Wi-Fi), then, from that
  screen, hold the power button to sleep. You should see the message **Send to X3: new
  reading delivered**: either while the catalog opens (a request queued at an earlier
  sleep is sent as soon as Wi-Fi is up) or just before the sleep screen. Wake the X3:
  **Browse Files > Inbox** has a new file named like `1761400000-1a2b3c4d-1.epub`,
  with one chapter per new item. In the web UI the items show `auto`, and the queue
  page lists the delivery under **Recent automatic deliveries**.
- [ ] C8. **The 2.0.0rc limitation.** Share another item. Wake the X3 and, without
  opening anything that uses Wi-Fi, put it back to sleep. On 2.0.0rc nothing is
  delivered; that is the known firmware bug, not a fault in the app. The item arrives
  the next time the X3 uses Wi-Fi (repeat C7). On a newer build or nightly that
  contains develop commit `facaa87` (2026-10-23 or later), this plain wake and sleep
  delivers it with no taps.
- [ ] C9. **Turning sleep delivery off.** On the app's Configuration tab turn off
  **Also deliver when the X3 sleeps**, select **Save** and restart the app. Do **not**
  reinstall the plugin yet. Share a new item, then repeat C7 on the X3. No new
  delivery file appears in **Inbox**, and the item still shows `waiting`: the app
  refuses sleep delivery even for an X3 that still has the old plugin.
- [ ] C10. Reinstall the plugin (C2 and C3). Fetch the waiting item with **Get 1 new**
  (C4, C5): that still works. Leave delivery at sleep off unless you want it.

Note: when the message appears in C7, and the rule that sleep delivery needs at
least 20% battery and the Wi-Fi network the X3 last joined, come from reading the
firmware source (develop `3f387a9`); they were not watched on a real X3 for this page.
If C7 delivers nothing, charge the X3 and check that it last joined your home Wi-Fi.

### D. X3 with CrossPoint 1.6.5: OPDS

For an X3 on 1.6.5 (it has no plugins). This also works on 2.x.

- [ ] D1. Add the server, either on the X3 or from the Mac:
  - **On the X3** (1.6.5): **Home > Settings > System > OPDS Servers > Add Server**.
    On 2.x: **Home > Settings > Connectivity > OPDS Servers > Add Server**.
  - **From the Mac** (easier typing): on the X3 open **Home > File Transfer > Join a
    Network**, then on the Mac open `http://<X3 address>/`, choose **Settings**, and in
    the **OPDS Servers** card select **+ Add Server**.

  Fill in:

  | Field | Value |
  |---|---|
  | Server Name | `Send to X3` |
  | OPDS Server URL (on the Mac: URL) | `http://<Pi IP>:8765/opds` |
  | Username | `x3` |
  | Password | the **Device token** from the Setup page |

  Then save (**Save** on the Mac page).
- [ ] D2. On the X3, in **OPDS Servers**, set **Download Folder** to `/Inbox` (the
  default is the SD card's top folder).
- [ ] D3. Share one new item first. On 1.6.5: **Home > OPDS Browser**. On 2.x: **Home >
  Plugins > OPDS Browser**. The first entry is **Get 1 new · <date time>** without an
  author; below it your items, newest first, with a short origin (the site or sender)
  as author.
- [ ] D4. Select the newest single entry, the second one (on 2.x, confirm on the page
  that opens). The book downloads; in the web UI the item shows `opds`. **Browse Files
  > Inbox** has `<origin> - <title>.epub` (just `<title>.epub` for long titles, so the
  name fits).
- [ ] D4b. Share another item, open the feed again and select the first entry, **Get 1
  new**. **Inbox** gets `Get 1 new · <date time>.epub` and the web UI shows `fetch` for
  the item.
- [ ] D5. Change the device token? Then update the password here too (and reinstall
  the plugin on 2.x).

### E. LAN push (any firmware)

- [ ] E1. Give the X3 a fixed address in your router (often called "DHCP
  reservation"), so it does not change.
- [ ] E2. On the app's Configuration tab set **X3 address for LAN push** to the X3's
  address (for example `192.168.1.60`), save and restart the app. The queue page's
  **Channels** row **LAN push to the X3** is no longer `disabled`.
- [ ] E3. Share a new item. On the X3 open **Home > File Transfer > Join a Network**
  and leave the screen open for at least 30 seconds (the app looks for it every 20
  seconds).
- [ ] E4. In the web UI the item shows `push`. Leave File Transfer on the X3, open
  **Browse Files > Inbox**: `<title>.epub` is there (`.txt` if **Download format** is
  TXT).
- [ ] E5. Push the same title again (select **Requeue** on the item, then repeat E3).
  The second copy arrives as `<title> (2).epub`; the first is kept.

### F. Reading on the X3

For a few of the files from C, D and E:

- [ ] F1. The Library shows the article's real title (EPUB) or the file name (TXT).
- [ ] F2. The first page shows the title, the author or site and the date, then the
  text. No images, no menus, no "Subscribe" boxes.
- [ ] F3. Page forward a few times: text flows normally, paragraphs are separated,
  lists and headings look right, there are no boxes in place of characters.
- [ ] F4. The last page ends with "Source:" and the site name.
- [ ] F5. A long article opens within a few seconds.

### G. iPhone

Set up the shortcut as described in [phone-ios.md](phone-ios.md). The Setup page has
no ready-made iOS download: the shortcut must be signed on a Mac
(`python3 shortcuts/build_shortcut.py --sign people-who-know-me`, then AirDrop it to
the iPhone) or built by hand following that guide.

- [ ] G1. At home on Wi-Fi, in Safari open an article, tap **Share > Send to X3**. A
  notification "Sent to X3: <title>" appears. The item is the article.
- [ ] G2. Select a paragraph on a Safari page, then **Share > Send to X3**. The item
  contains only that paragraph, with the page as its source (open the item in the web
  UI to check).
- [ ] G3. In Notes, select a few sentences with no link and share them. The item is a
  note with your text.
- [ ] G4. In Notes, write a short comment and a link on the next line (for example
  "worth reading" and an article link), select both and share. The item is the
  article, with your comment kept as a note.
- [ ] G5. In Notes, write three article links, one per line, select them and share.
  Three items appear, one per link.
- [ ] G6. Turn Wi-Fi off (mobile data only) and share a page. With email ingest set up
  (section J), an email goes out and the item appears a little later.
- [ ] G7. Change the token in the shortcut to something wrong and share. An alert
  says the ingest token is invalid. Put the right token back.

### H. Android

- [ ] H1. On the Setup page select **Download Android HTTP Shortcuts file**. The file
  already contains the server address and your ingest token, so treat it like a
  password. Get it onto the phone (for example email it to yourself, or open the Setup
  page on the phone and download it there).
- [ ] H2. Install **HTTP Shortcuts** from Google Play or F-Droid. In it, open the menu
  (three dots) > **Import / Export > Import from File** and pick the file. A "Send to
  X3" category appears. Under menu > **Global Variables**, `sendtox3_url` and
  `sendtox3_token` are already filled in.
- [ ] H3. In Chrome, share an article to **HTTP Shortcuts** (or the "Send to X3"
  shortcut). A message "Sent to X3: <title>" appears and the item is in the queue.
- [ ] H4. Share selected text with a link, and several links, as in G3 to G5. The
  results match.

More details and troubleshooting: [phone-android.md](phone-android.md).

### I. Home Assistant Companion app

- [ ] I1. The queue page's **Channels** row **Home Assistant share sheet** is not
  `disabled` or `error`.
- [ ] I2. On the phone, share a page and choose **Home Assistant** in the share sheet,
  then **Send**. The item appears in the queue.
- [ ] I3. Share again, this time typing a short comment into the Companion app's text
  box. The item is the article with your comment kept as a note.

### J. Email (Gmail)

Use a dedicated Gmail account with 2-Step Verification and an app password, as the
app's Documentation tab ([DOCS.md](../send_to_x3/DOCS.md)) explains. Have a second,
personal address to send from.

- [ ] J1. **Refuses until restricted.** On the Configuration tab turn on **Receive by
  email**, fill in **IMAP user name** and **IMAP password**, and leave **Allowed
  senders** and **Secret plus-address tag** empty. Save and restart. On the queue page
  the **Channels** row **Email (IMAP)** shows **error**: "allowed_senders and
  imap_plus_tag are both empty, so no mail is accepted: list the addresses you send
  from in allowed_senders ('*' accepts any sender) and/or set imap_plus_tag". Send a
  message with a link to the dedicated address: nothing appears in the queue, and in
  Gmail the message is still **unread**.
- [ ] J2. Set **Allowed senders** to your personal address (and, if you like, a
  **Secret plus-address tag**). Save and restart. The **Email (IMAP)** row under
  **Channels** shows `ok`. The Setup page's **Email** section shows the address to
  send to.
- [ ] J3. From your personal address, send a message whose body is just an article
  link. Within a minute the article is in the queue. In Gmail the message is now
  read (and moved, if you set **Move processed mail to**).
- [ ] J4. Send from a different address that is not allowed. Nothing appears, and the
  message stays unread in Gmail.
- [ ] J5. Forward a newsletter, and send a message with a PDF attached. Each becomes an
  item (the PDF as its own item).
- [ ] J6. Optional: set **Allowed senders** to `*` only, leave **Secret plus-address
  tag** empty and **Mailbox folder** at `INBOX`. Save and restart. **Email (IMAP)**
  shows **error** with a message starting "allowed_senders is '*' (any sender) but the
  app watches INBOX without imap_plus_tag". Put your own address back.

### K. PDFs and slow shares

- [ ] K1. Turn on **OCR scanned PDFs**, save and restart. From the iPhone or Android
  shortcut, share a link to a scanned PDF of several pages. After about 25 seconds the
  phone shows a message that the share is still processing (currently: "Received
  <link>; still processing. It will appear on the X3 shortly."). A minute or two
  later the item appears in the queue with the recognised text.
- [ ] K2. Share another slow PDF link and, while it is still processing, restart the
  app (**Settings > Apps > Send to X3 > Restart**). After the restart the item still
  appears.
- [ ] K3. Read one PDF item on the X3 (section F). See [pdf.md](pdf.md) for what to
  expect from PDFs.

### M. The Send to X3 firmware (optional)

For an X3 that should read right from the plugin page. Follow
[FIRMWARE.md](FIRMWARE.md); build the file first (level 3b).

- [ ] M1. Note the **Firmware** row in **Settings > System > About** and keep the
  official `.bin` of that version (FIRMWARE.md, "Before you start").
- [ ] M2. Copy `firmware/dist/crosspoint-2.0.0-sx3+<hash>-x3-x4.bin` to the top level
  of the SD card over **File Transfer** (**Upload** in the X3 web page's **File
  Manager**). Then **Settings > System > SD Card Firmware Update**: the file is listed;
  selecting it shows **Update firmware?**; **Confirm** shows a progress bar and the X3
  restarts by itself. **About** shows `2.0.0-sx3+<hash>`. Books, settings and the saved
  Wi-Fi are still there.
- [ ] M3. Install the plugin over Wi-Fi from the Setup page. The third log line reads
  `Confirm opens the book: this X3's firmware opens catalog downloads in the reader.`
- [ ] M4. Share a note. In **Home > Plugins > Send to X3** the button on **Get 1 new**
  says **Open**. Press it: **Downloading...**, a short **Loading**, and the book opens
  in the reader. The web UI shows `fetch` right away (no **not confirmed yet**).
- [ ] M5. Read a few pages of the M4 book and go back to Home. Open it again from
  Home's recent books or the **Library**: it opens on the page where you stopped.
  Then open the plugin and press **Open** on that item's own row (below the first
  row): **Downloading...**, then it opens at page 1 (a separate file named after the
  item), and its **On the X3** column gains `catalog`. Read a few pages, go back to
  Home, open the plugin and press **Open** on the same row again: no
  **Downloading...**, it opens on the page where you stopped, and no new `catalog`
  time appears.
- [ ] M6. **Open** on **Nothing new right now** shows **Download failed**.
- [ ] M7. Share 9 or more items without fetching them. Page through the catalog with
  **Next Page**: every item appears once, no row repeats at the top of page 2, and
  there is no empty last page.
- [ ] M8. Recovery mode: with the X3 asleep, hold the side **Up** button and press
  **Power**. Note which button worked (FIRMWARE.md could not say for sure). The
  firmware picker (**Select firmware file (.bin)**) opens; **Recovery Mode** shows in
  the header after you select a file. **Back** only reopens the picker, so select the
  Send to X3 file (still on the card) and **Confirm**; the X3 restarts normally.
- [ ] M9. Go back: put the official `.bin` on the card and install it the same way.
  **About** shows the official version; the button in the plugin says **Fetch** again
  and **Get N new** still works.

### L. Clean up

- [ ] L1. Delete test items with **Delete** on the queue page (files already on the X3
  stay there; delete them on the X3 with **Browse Files**).
- [ ] L2. Put any options you changed back the way you want them, save and restart.
- [ ] L3. If you use the optional delivery at sleep and the X3 is on 2.0.0rc, consider
  a newer build or nightly with commit `facaa87`, where it needs no taps.
