# Developing Send to X3

How the repository is organised, how to run the app on a Mac, the conventions the
code follows, how to add an option, and how to publish a release.

The design spec every component is built against is [ARCHITECTURE.md](ARCHITECTURE.md);
how everything is tested is in [TESTING.md](TESTING.md). Set up Python, Docker and the
project environment first, as described in
[TESTING.md, One-time setup on a Mac](TESTING.md#one-time-setup-on-a-mac). Every
command below assumes a Terminal in the repository folder with the environment
activated:

```sh
cd "$HOME/My Drive/Claude/Projects/xteink"
source ~/.venvs/sendtox3/bin/activate
```

## Repository layout

```
repository.yaml                 Home Assistant app repository manifest
send_to_x3/                     the Home Assistant app (the folder name is the app's slug)
  config.yaml                   app manifest: version, ports, ingress, options and their schema
  translations/en.yaml          names and help texts of the configuration form
  DOCS.md                       the app's Documentation tab: every option
  README.md                     the app store description
  CHANGELOG.md                  release notes, one section per version
  Dockerfile  run.sh            the image; run.sh starts python -m sendtox3
  requirements.txt              pinned runtime dependencies
  icon.png  logo.png            store images (regenerate: e2e/ha/make_icons.py)
  sendtox3/                     the Python package (see the module map)
tests/                          pytest: unit and integration tests
  conftest.py                   shared fixtures (data dir, Options, Store) and marker handling
  fakes/                        fake CrossPoint File Transfer server, fake Home Assistant websocket
  fixtures/                     emails, web pages and PDFs used by the tests
  integration/                  flows through the real extractors and renderers
e2e/                            harnesses that run outside a plain pytest run
  run_e2e.py  mock_backend.py   simulator scenario runner, stand-in backend
  scenarios/                    S1-S5, W1, the backends, the recording relay
  sim/                          builds and drives the CrossPoint simulator
  wire/                         the X3's own HTTP client compiled for the Mac
  fwharness/                    CrossPoint's EPUB, TXT, OPDS and file-name code compiled for the Mac
  epubcheck/                    W3C epubcheck image
  ha/                           Supervisor validation, devcontainer install test, icon generator
shortcuts/                      phone shortcuts
  build_shortcut.py             iOS/macOS Shortcut generator (and signing)
  build_android.py              Android HTTP Shortcuts import file generator
  dist/                         generated, unsigned iOS Shortcut and its XML form (committed)
  android/                      generated HTTP Shortcuts import file (committed)
  tools/                        checkers for the generated files
firmware/                       the optional Send to X3 firmware (see firmware/README.md)
  UPSTREAM  patches/            the pinned CrossPoint commit and the patch series on top
  build.sh                      builds it in Docker; output in firmware/dist/ (gitignored)
scripts/check_all.sh            runs every verification level
docs/                           user and developer documentation
.github/workflows/              tests.yml (CI), build.yml (images to GHCR)
requirements-dev.txt            runtime dependencies plus test tools
pytest.ini                      test paths, pythonpath, markers
LICENSE                         MIT
```

Never add a file named `config.yaml`, `config.yml` or `config.json` anywhere else in
the repository (outside hidden folders): Home Assistant treats every such file as an
app. `tests/test_packaging.py` fails if you do.

## Module map

Everything lives in `send_to_x3/sendtox3/`. Section numbers refer to
[ARCHITECTURE.md](ARCHITECTURE.md).

| Module | What it does | Tests |
|---|---|---|
| `__main__.py` | Entry point: loads options, opens the store, resolves tokens, starts the web server and the enabled workers (each restarted with backoff if it crashes), redoes shares a restart interrupted (`Processor.recover_pending`), keeps retrying the server address detection while it is only a guess, prunes old items daily, shuts down on SIGTERM (shares still processing get 5 s). `--show-tokens` prints the tokens | `test_web_main.py`, `test_fix_E_main.py` |
| `config.py` | `Options` (every option and its default) and `load_options` (reads `options.json`, then `SENDTOX3_<OPTION>` environment overrides) | `test_packaging.py`, `e2e/ha/validate_config.py` |
| `app_state.py` | Runtime state shared by the web app and the workers: options, store, processor, the detected server address, channel status for the admin page, token generation and rotation | `test_web_main.py`, `test_web_admin.py` |
| `models.py` | `IngestRequest` (what arrives) and `Item` (what is queued), item ids | `test_store.py` |
| `textmodel.py` | The format-neutral `Document` and its blocks, `clean_text`, font coverage (§5) | `test_render_*.py` |
| `store.py` | SQLite queue and rendered files under the data folder (owner-only permissions), secrets, delivery records, the spool of shares being processed, titles already used on the X3, retention (§4) | `test_store.py`, `test_fix_A_store.py`, `test_fix2_A_store.py` |
| `ingest.py` | `Processor`: classifies a share, removes duplicates, extracts, titles, renders and stores it; raises only `StorageError` (any other failure becomes an item with status `failed`); processing budgets; the spool and redoing shares interrupted by a restart (§7.1) | `test_ingest.py`, `test_fix_A_ingest.py`, `test_fix2_A_ingest.py` |
| `delivery.py` | The "Get N new" bundle (the catalog's first row and first OPDS entry), sleep-time auto-delivery (digest, byte-identical replay, the "too large" note) and the plugin catalog pages (§7.2) | `test_fetch_bundle.py`, `test_delivery.py`, `test_fix_A_delivery.py` |
| `device_client.py` | Client for the X3's File Transfer web server (stock firmware) | `test_device_client.py` |
| `plugin_bundle.py` | The CrossPoint 2.x plugin: `device.json`, the SD-card zip, installing it over Wi-Fi, detecting and checking the server address, checking the device token (§10) | `test_plugin_bundle.py` |
| `phone.py` | The Android HTTP Shortcuts import file (the Setup page's download and `shortcuts/build_android.py` use it) and the iOS note on the Setup page | `test_phone.py` |
| `extract/fetch.py` | Safe URL fetcher (blocks private addresses and fetches over IPv4 only unless allowed, limits size, time and redirects), URL cleaning, charset decoding (§12) | `test_extract_fetch.py` |
| `extract/html.py` | HTML to `Document` (trafilatura) and URL to `Document` routing | `test_extract_html.py` |
| `extract/sites.py` | Site APIs for pages that extract badly from HTML (GitHub, Wikipedia, Substack, Bluesky, X) | `test_extract_sites.py` |
| `extract/text.py` | Shared text to `Document`, link detection, classification, titles | `test_extract_text.py` |
| `extract/email.py` | Email (RFC 822) to share requests, and the acceptance filter, which fails closed (§9) | `test_email.py`, `test_email_accept.py` |
| `extract/pdf.py`, `extract/pdfengine/` | PDF to `Document` with PDFium (pypdfium2) in a separate, unprivileged process with a minimal environment: layout heuristics, reading order, headings, optional OCR with `tesseract`. From async code use `pdf_to_document_async` (cancellable) | `test_pdf.py`, `test_pdf_fixtures.py`, `test_pdf_ocr.py`, `test_fix_D_pdf_worker.py` |
| `render/txt.py`, `render/epub.py`, `render/names.py` | `.txt` and EPUB output following the firmware's rules, firmware-safe titles (§6, §8) | `test_render_txt.py`, `test_render_epub.py`, `test_names.py`, `test_render_validation.py` |
| `web/app.py` | aiohttp application factory: routes, response framing rules, error handling, access log | `test_web_*.py` |
| `web/common.py` | Shared web pieces: token checks (constant time), framing-safe responses | `test_web_*.py` |
| `web/ingest_api.py` | `POST /api/v1/ingest`, the share endpoint (§7.1) | `test_web_ingest.py` |
| `web/device_api.py` | `/device/v1/catalog`, `/device/v1/items/<id>.epub` and `.txt` (also `all-<key>` bundles and `none`), `POST /device/v1/fetched` (the X3 confirms a saved bundle), `/device/v1/deliver` for the 2.x plugin | `test_web_device.py`, `test_fetch_bundle.py` |
| `web/opds.py` | `/opds` feed and `/opds/items/<id>.epub` for 1.6.5 and later (§7.3) | `test_opds.py` |
| `web/admin.py`, `web/templates/`, `web/static/` | The admin web UI: queue, previews, paste and upload, channel health, tokens and **New device token**, Setup page, plugin bundle and install, the Android import file | `test_web_admin.py` |
| `workers/imap.py` | Email ingest: IMAP IDLE (or polling), acceptance filter, marking read and moving, the per-message attempt counter | `test_imap_worker.py`, `test_imap_greenmail.py` |
| `workers/ha_share.py` | Home Assistant Companion app shares (`mobile_app.share` over the Core websocket) | `test_ha_share.py` |
| `workers/lan_push.py` | LAN push to an X3 showing File Transfer | `test_lan_push.py` |

### Endpoints on port 8765

| Path | Who calls it | Authentication |
|---|---|---|
| `GET /healthz` | Home Assistant's watchdog, you | none |
| `POST /api/v1/ingest` | phone shortcuts, scripts | `Authorization: Bearer <ingest token>` or `?token=` |
| `GET /device/v1/catalog`, `/device/v1/items/<id>.epub`, `/device/v1/items/<id>.txt`, `/device/v1/deliver`, `POST /device/v1/fetched` | the 2.x plugin | `Authorization: Bearer <device token>` |
| `GET /opds`, `/opds/items/<id>.epub` | the OPDS browser | HTTP Basic, user `x3`, password = device token |
| `/admin` and below | you, in a browser | Home Assistant ingress, or the admin token on the LAN |

### How a share moves through the app

1. A share arrives: `web/ingest_api.py` (phones, scripts), `web/admin.py` (paste and
   upload), `workers/imap.py` (email) or `workers/ha_share.py` (Companion app). Each
   turns it into one or more `IngestRequest`s.
2. `ingest.Processor` writes it to the spool (so a restart cannot lose it), classifies
   it (a link, a link with a comment, several links, a selection, text, an email, a
   file), fetches and extracts the article (`extract/`), picks a firmware-safe title
   (`render/names.py`) and the headline for the book, renders the EPUB and TXT
   (`render/`), stores one or more `Item`s (`store.py`) and removes the spool entry.
3. The X3 receives it when the user asks: the 2.x plugin catalog's first row, "Get N
   new", or a single row (`web/device_api.py`, `delivery.py`), or OPDS (`web/opds.py`);
   or, if turned on, sleep delivery (`delivery.py`) or LAN push (`workers/lan_push.py`). Each records the delivery on the item, so the admin
   page shows where it went.

## Run the app on your Mac

The app is one Python process. Outside Home Assistant it reads its options from
`$SENDTOX3_DATA_DIR/options.json` instead of `/data/options.json`.

1. Make a data folder outside Google Drive and write an `options.json`:

   ```sh
   export SENDTOX3_DATA_DIR="$HOME/.cache/sendtox3-dev/data"
   mkdir -p "$SENDTOX3_DATA_DIR"
   cat > "$SENDTOX3_DATA_DIR/options.json" <<'EOF'
   {
     "log_level": "debug",
     "server_host": "",
     "ha_share_enabled": false,
     "imap_enabled": false
   }
   EOF
   ```

   Any option from [DOCS.md](../send_to_x3/DOCS.md) can go in this file. Options you
   leave out keep their defaults. With `server_host` empty the app uses the Mac's
   LAN address in the URLs it gives to phones and the X3; set it to
   `127.0.0.1:8765` if only the Mac itself will connect.

2. Start the app from the repository folder:

   ```sh
   PYTHONPATH="$PWD/send_to_x3" python -m sendtox3
   ```

   You should see, among others:

   ```
   ... INFO sendtox3: Send to X3 0.1.0 listening on 0.0.0.0:8765 (data in /Users/<you>/.cache/sendtox3-dev/data, 0 items)
   ... INFO sendtox3: tokens: ingest=generated, device=generated, admin=generated (values are on the admin Setup page; never logged)
   ```

   Stop it with Ctrl-C (it logs `shutting down`). The app listens on all network
   interfaces by default, so phones and an X3 on your Wi-Fi can reach it. To keep it
   on the Mac only, start it with `SENDTOX3_BIND=127.0.0.1` in front.

3. Get the tokens. They are generated on the first start and kept in the data folder.
   In a second Terminal window (repository folder, environment activated):

   ```sh
   PYTHONPATH="$PWD/send_to_x3" SENDTOX3_DATA_DIR="$HOME/.cache/sendtox3-dev/data" python -m sendtox3 --show-tokens
   ```

   It prints `ingest_token: ...`, `device_token: ...` and `admin_token: ...`, then
   exits.

4. Open the admin page in a browser: `http://127.0.0.1:8765/admin?token=<admin token>`.
   After the first visit a cookie keeps you signed in.

5. Send something from Terminal:

   ```sh
   curl -H "Authorization: Bearer <ingest token>" --data-urlencode "text=https://example.com/" \
     http://127.0.0.1:8765/api/v1/ingest
   ```

   The answer is JSON starting with `{"ok":true` and the item appears on the admin
   page.

Port 8765 must be free. Check with `lsof -nP -iTCP:8765 -sTCP:LISTEN`: it should list
only the app's Python process. On macOS another program listening on
`127.0.0.1:8765` does not stop the app from starting, but requests to `127.0.0.1` then
go to that other program (you get its error pages instead of the app's).

The listening port is not in the Home Assistant configuration form (it is fixed at
8765 inside the container), but `Options.port` still exists for development: start the
app with `SENDTOX3_PORT=9000` in front to listen on another port. `SENDTOX3_BIND`
chooses the listening address.

### Overriding options without editing the file

Every option can also be set as an environment variable named `SENDTOX3_` plus the
option name in capitals. Environment variables win over `options.json`:

```sh
SENDTOX3_DELIVERY_FORMAT=txt SENDTOX3_LOG_LEVEL=debug PYTHONPATH="$PWD/send_to_x3" python -m sendtox3
```

Booleans accept `1`, `true`, `yes` or `on` (anything else is false); lists are
comma-separated (`SENDTOX3_ALLOWED_SENDERS=me@example.com,you@example.org`).
`--options <file>` reads another options file instead of the one in the data folder.

Without Home Assistant there is no `SUPERVISOR_TOKEN`, so the Companion app share
worker does not start (its row on the admin page says it is only available as a Home
Assistant app). The tests point it at a fake Home Assistant with
`SENDTOX3_HA_WS_URL`.

### Run the image with plain Docker

The same image runs without Home Assistant:

```sh
docker build -t send_to_x3:dev send_to_x3
mkdir -p "$HOME/.cache/sendtox3-dev/docker-data"
echo '{"log_level":"debug","server_host":"<your Mac LAN IP>:8765"}' > "$HOME/.cache/sendtox3-dev/docker-data/options.json"
docker run --rm -p 127.0.0.1:8765:8765 -v "$HOME/.cache/sendtox3-dev/docker-data:/data" send_to_x3:dev
```

Print the tokens of that data folder with:

```sh
docker run --rm -v "$HOME/.cache/sendtox3-dev/docker-data:/data" send_to_x3:dev python3 -m sendtox3 --show-tokens
```

`-p 127.0.0.1:8765:8765` keeps it on the Mac. To reach it from an X3 or phone, use
`-p 8765:8765` and set `server_host` to the Mac's LAN address (`ipconfig getifaddr en0`
shows it on most Macs).

### Try it with a simulated X3

The simulator scenarios start their own copy of the app with a temporary data folder,
so they do not touch yours:

```sh
python e2e/run_e2e.py -s S1 -f 2.0.0rc -b local
```

To see what the X3's own HTTP client receives from your running app, build the wire
harness ([e2e/README.md](../e2e/README.md)) and run:

```sh
python e2e/wire/wire.py --base-url http://127.0.0.1:8765 --token <device token> --fresh catalog
```

### Try it on the Raspberry Pi

Install the folder as a local app through the Samba share, as in
[TESTING.md, section A](TESTING.md#a-the-app-on-your-raspberry-pi). After you change
files, copy `send_to_x3` over again and select **Rebuild** on the app's page in Home
Assistant. Home Assistant only offers an **Update** when `version` in `config.yaml`
changes.

## Coding conventions

- **Python 3.14**, `from __future__ import annotations`, type hints everywhere,
  dataclasses for records. Keep lines to about 120 characters, as the existing code
  does (there is no formatter configured).
- **Module docstrings explain the why.** Each module starts with what it does, the
  guarantees it gives and, where firmware behaviour drives a rule, the CrossPoint
  file and line it comes from. Keep those references current when you change the rule.
- **Contracts between modules** are listed in [ARCHITECTURE.md §14](ARCHITECTURE.md).
  Do not change a signature there without updating every caller and the spec.
- **Never raise for remote problems.** `Processor.process` and its siblings turn any
  failure into an item with status `failed` that still keeps the link or text; the only
  exception they raise is `StorageError` (the disk or database failed, nothing was
  saved), which callers answer with HTTP 503 or a later retry. Extractors return a
  `Document` with a note when a page is blocked or empty; workers catch everything
  except `asyncio.CancelledError` and report problems in their status entry, which the
  admin page shows.
- **Asynchronous code** runs on aiohttp's event loop; slow or blocking work runs in a
  thread or, for PDF conversion and OCR, a separate process. Await
  `pdf_to_document_async` rather than `asyncio.to_thread(pdf_to_document, ...)`: a
  cancelled thread keeps converting and holds one of the two converter slots.
- **HTTP responses** always have an explicit `Content-Length`, are never compressed,
  and are never 204, 304 or redirects (CrossPoint's HTTP client mishandles those). Use
  the helpers in `web/common.py`.
- **Everything the X3 sees is firmware-safe**: titles go through `render/names.py`,
  EPUBs follow the rules in [ARCHITECTURE.md §6.2](ARCHITECTURE.md).
- **Secrets**: compare tokens with `tokens_equal` (constant time); never log a token
  or password; never put real secrets in tests, fixtures, docs or generated files
  (use obvious placeholders such as `ingest-test-token`).
- **Logging**: `log = logging.getLogger(__name__)`; messages are plain sentences a
  user can act on (they show up in Home Assistant's Log tab).
- **Dependencies**: runtime packages go in `send_to_x3/requirements.txt`, pinned to an
  exact version (`name==1.2.3`), and must ship wheels for musllinux on aarch64 and
  x86_64 (the image installs wheels only, so a missing wheel fails the build instead of
  compiling for an hour on a Pi). Test-only packages go in `requirements-dev.txt`.
- **Tests**: one file per module, `tests/test_<module>.py`; cross-module flows in
  `tests/integration/`. Unit tests never use the network: use the fixture servers and
  `tests/fakes/`. A test that needs Docker gets `@pytest.mark.docker`, one that needs
  the internet `@pytest.mark.network`; firmware tests skip unless
  `SENDTOX3_FIRMWARE_TESTS=1`. Synthetic test text only, or fixtures with their source
  noted.
- **Text files** are UTF-8 without BOM, LF line endings, exactly one trailing newline,
  no tabs (except the Dockerfile). `run.sh` stays executable and starts with
  `#!/command/with-contenv bashio`; CI runs `shellcheck` on it.
- **GitHub Actions** are pinned to exact releases, never `main` or `latest`.
- **Generated files are committed and deterministic.** After changing a generator,
  regenerate and commit the output; the tests fail when it is stale:

  ```sh
  python3 shortcuts/build_shortcut.py      # shortcuts/dist/Send to X3.shortcut and .xml
  python3 shortcuts/build_android.py       # shortcuts/android/http-shortcuts-send-to-x3.json
  ```

  Never commit a signed shortcut or one built with `--token`.
- **Docs**: link between documents with relative links; take every UI label from the
  code or the firmware; mark anything not verified with an inline "Note:".

## How to add an option

Options are defined once in code and mirrored in three files that Home Assistant
reads. `tests/test_packaging.py` checks that all of them agree. The one deliberate
exception is `port`, which stays out of the Home Assistant files (`DEV_ONLY_OPTIONS`
in `tests/test_packaging.py`). As an example, adding
a yes/no option `example_flag` that is off by default:

1. **`send_to_x3/sendtox3/config.py`**: add a field to `Options` with its default, in
   the group it belongs to (the order matters: the other files must list the options
   in the same order):

   ```python
   example_flag: bool = False  # one-line explanation
   ```

   Supported types are `bool`, `int`, `str` and `list[str]`. `load_options` converts
   values and adds the `SENDTOX3_EXAMPLE_FLAG` override automatically. If the value
   needs cleaning up (like the folder options), do it at the end of `load_options`.

2. **`send_to_x3/config.yaml`**: add the option at the same position under `options:`
   with the same default, and under `schema:` with its type:

   | Kind of option | `schema` entry |
   |---|---|
   | yes/no | `bool` |
   | number | `int(<min>,<max>)`, always with a range (`port` for a network port) |
   | text that may be empty | `str?` |
   | text that must be filled in | `str` |
   | secret | `password?` |
   | one of a few choices | `list(<a>\|<b>)` |
   | list of texts | a list holding `str?` (see `allowed_senders`) |

3. **`send_to_x3/translations/en.yaml`**: add an entry under `configuration:` at the
   same position, with a `name` (at most 40 characters; this is the label in the
   configuration form) and a `description` written for a non-developer. Say if the
   user has to reinstall the plugin or restart for it to take effect.

4. **`send_to_x3/DOCS.md`**: add a row to the right table with the option name in
   backticks (`` `example_flag` ``), its default and what it does.

5. **Use it and test it.** Read it as `opts.example_flag` where it matters, and add
   tests for the behaviour in the module's test file. If it changes what the X3 gets
   from the plugin, cover it in `tests/test_plugin_bundle.py`. If it has a range or
   choices, add a valid non-default value to "email ingest, LAN push, fixed tokens" in
   `valid_option_sets` and an out-of-range value to `invalid_option_sets` in
   `e2e/ha/validate_config.py`.

6. **Check**:

   ```sh
   python -m pytest -q tests/test_packaging.py
   scripts/check_all.sh --supervisor
   ```

   `test_packaging.py` fails with a message naming the file that is out of step. The
   Supervisor validation confirms Home Assistant accepts the option and shows it in
   the form.

Add a line to `send_to_x3/CHANGELOG.md` under the next version.

## Release process

Releases are numbered with [semantic versioning](https://semver.org/) and published as
prebuilt images on GitHub Container Registry (GHCR), so Home Assistant downloads the
image instead of building it on the Pi.

1. **Check everything.** `scripts/check_all.sh --all` passes, the Home Assistant
   install test passes (`e2e/ha/devcontainer_install.sh`, see
   [TESTING.md](TESTING.md#install-in-a-real-supervisor-devcontainer)), and for
   user-visible changes the relevant parts of the
   [manual checklist](TESTING.md#manual-acceptance-checklist-on-your-real-devices) were
   done on your devices.
2. **Write the changelog.** In `send_to_x3/CHANGELOG.md` add a section at the top,
   `## [X.Y.Z] - YYYY-MM-DD`, with `### Added`, `### Changed`, `### Fixed` or
   `### Removed` lists. Home Assistant shows this file to users before they update.
3. **Bump the version** in two places, to the same value:
   - `version: "X.Y.Z"` in `send_to_x3/config.yaml` (keep the quotes; it is also the
     image tag);
   - `__version__ = "X.Y.Z"` in `send_to_x3/sendtox3/__init__.py`.

   `tests/test_packaging.py` checks that the two match and that the changelog has the
   section.
4. **Push to `main`** (directly or by merging a pull request). The **Build** workflow
   (`.github/workflows/build.yml`) builds the aarch64 and amd64 images with Home
   Assistant's builder actions, signs them with cosign, and pushes:

   ```
   ghcr.io/<owner>/aarch64-send-to-x3:X.Y.Z   (and :latest)
   ghcr.io/<owner>/amd64-send-to-x3:X.Y.Z
   ghcr.io/<owner>/send-to-x3:X.Y.Z           the multi-arch image Home Assistant uses
   ```

   `<owner>` is your GitHub user or organisation in lower case. Pull requests only build
   and test the images. An existing version is never pushed again: to publish a fix,
   bump the version. Watch the run under the repository's **Actions** tab; all jobs
   must be green.

5. **First release only: make the packages public.** GHCR creates new packages as
   private, and Home Assistant cannot download a private image. On GitHub open your
   profile or organisation, **Packages**, then each of `send-to-x3`,
   `aarch64-send-to-x3` and `amd64-send-to-x3`: **Package settings**, **Change
   visibility**, **Public**.

   Note: these GitHub menu names were not checked for this page. At least the package
   named in `image:` must be public.

6. **First release only: switch Home Assistant to the prebuilt image.**
   - In `send_to_x3/config.yaml` replace `REPLACE_OWNER` with your lower-case owner and
     remove the `# ` in front of `image:`, so it reads
     `image: "ghcr.io/<owner>/send-to-x3"`.
   - In `repository.yaml` fill in `url:` and `maintainer:` and remove their `# `.
   - `tests/test_packaging.py` (`test_required_keys_and_identity`) currently insists
     that `image:` stays commented out and that the `REPLACE_OWNER` placeholder is
     present, for local builds. Change those two checks to expect your image name in
     the same commit.
   - Commit and push. New installs now download version X.Y.Z instead of building it.
     Existing installs switch at their next update.

7. **Check the update in Home Assistant.** **Settings > Apps > Install app**, menu at
   the top right > **Check for updates**. The app's page offers the new version;
   update it and confirm the **Log** tab shows `Send to X3 X.Y.Z listening on ...`.

Home Assistant may show an update as soon as it sees the new `version`, a few minutes
before the Build workflow has finished pushing the image. Installing in that window
fails with a download error; wait for the workflow and try again.

The Build workflow's smoke test imports exactly the packages in
`send_to_x3/requirements.txt` (plus `sendtox3`) and checks that `tesseract` and its
English data are in the image; `tests/test_packaging.py` keeps that list in step with
`requirements.txt`.
