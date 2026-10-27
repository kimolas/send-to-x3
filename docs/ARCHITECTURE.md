# Send to X3 — architecture and contracts

This is the design spec every component is built against. Firmware facts are from
CrossPoint source read at tag `1.6.5` and at `2.0.0rc`/develop `3f387a9` (2026-10-25).

## 1. Goal

Share selected text, a URL, an email (body, link, or PDF attachment) from a phone or
computer, and have it show up on an Xteink X3 running **stock, unmodified CrossPoint**
as clean, image-free text, with no cable.

## 2. Components

```
 phone/computer                      Raspberry Pi (Home Assistant OS)                 Xteink X3 (stock CrossPoint)
 ───────────────                     ────────────────────────────────                 ────────────────────────────
 iOS Shortcut ──HTTP (home Wi-Fi)──▶ ┐
 iOS Shortcut ──email (away)──┐      │  Send to X3 app (HA add-on/"app")
 any mail client ─────────────┼─▶ Gmail ◀─IMAP IDLE (outbound)─┤
 HA Companion app share ──────┼─▶ HA Core ─mobile_app.share WS─┤  ingest → extract → queue
 Android HTTP Shortcuts ──────┘                                │  render .txt + .epub
                                                               │
                                     device API (HTTP :8765) ◀─┼──── 2.x plugin: catalog + sleep auto-delivery
                                     OPDS feed  (HTTP :8765) ◀─┼──── 1.6.5+: OPDS browser (EPUB)
                                     LAN push worker ──────────┴───▶ 1.6.5+: File Transfer /upload (.txt)
```

Everything server-side is one Python process (`sendtox3`) packaged as a Home Assistant
OS app. The same container also runs with plain `docker run` (no Supervisor) for
development and tests.

## 3. Repository layout

```
repository.yaml                 HA app repository manifest
send_to_x3/                     the HA app (folder name = slug)
  config.yaml  Dockerfile  run.sh  DOCS.md  README.md  CHANGELOG.md
  icon.png  logo.png  translations/en.yaml
  requirements.txt              pinned runtime deps
  sendtox3/                     python package (copied into the image)
    __main__.py                 entry: load options, start services
    config.py                   Options dataclass from /data/options.json (+ env overrides for dev)
    store.py                    SQLite queue + file storage under /data
    models.py                   Item, Delivery dataclasses
    textmodel.py                Document/Block intermediate representation
    extract/
      html.py                   HTML -> Document (article extraction)
      fetch.py                  safe URL fetcher (size/time/redirect limits, SSRF guard)
      text.py                   pasted text -> Document (paragraphing, URL detection)
      email.py                  RFC822 -> list[IngestRequest]
      pdf.py                    PDF -> Document (stretch goal)
    render/
      txt.py                    Document -> .txt bytes (CrossPoint TXT conventions)
      epub.py                   Document(s) -> minimal EPUB bytes
      names.py                  firmware-safe titles/filenames
    ingest.py                   IngestRequest -> Item (classification, dedupe)
    web/
      app.py                    aiohttp app factory, routes, auth middleware
      device_api.py             2.x plugin catalog / item / deliver endpoints
      opds.py                   OPDS 1.2 acquisition feed for 1.6.5
      ingest_api.py             POST /api/v1/ingest
      admin.py                  admin UI (served via HA ingress + token on LAN)
      templates/ static/
    workers/
      imap.py                   IMAP IDLE poller
      ha_share.py               HA Core websocket subscriber (mobile_app.share)
      lan_push.py               pushes .txt to device File Transfer server
    plugin_bundle.py            generates the CrossPoint 2.x SD plugin folder/zip
tests/                          pytest unit + integration tests (no network)
e2e/                            end-to-end harnesses (docker, firmware simulator, wire harness)
shortcuts/                      iOS Shortcut generator + manual build guide
docs/                           user documentation
.github/workflows/              CI: tests, image build to GHCR
```

## 4. Data model

`Item` (one shared thing):

| field | type | notes |
|---|---|---|
| `id` | str | 10-char lowercase base32 random, `[a-z2-7]{10}` (URL-safe, firmware-safe) |
| `created_at` | int | unix seconds |
| `kind` | `url` \| `text` \| `email` \| `pdf` | |
| `source` | str | `http`, `email`, `ha_share`, `admin` |
| `title` | str | display title, already sanitized (see §8) |
| `byline` | str | author / site / sender, may be empty |
| `source_url` | str | may be empty |
| `status` | `ready` \| `failed` | failed items keep an error message and a fallback body |
| `error` | str | |
| `word_count` | int | |
| `txt_path`, `epub_path` | str | files under `/data/items/<id>/` |
| `delivered` | json | `{channel: unix_ts}` for `catalog`, `auto`, `opds`, `push` |
| `dedupe_key` | str | normalized URL or sha256 of text, unique within 24 h |

Storage: SQLite `/data/sendtox3.db` (WAL), files under `/data/items/<id>/`. Retention
option `retention_days` (default 30) prunes old items daily. Auto-delivery bookkeeping
table `deliveries(ev, ts, item_ids, epub_path, created_at)` so a retried device request
gets byte-identical output.

## 5. Intermediate text model

```python
Block = Heading(level:int 1..3, text) | Paragraph(text) | ListItem(text, ordered:bool, index:int)
      | Quote(text) | Preformatted(text) | Rule()
Document(title, byline, source_url, created_at, blocks: list[Block], lang="en")
```

All text is NFC-normalized, single-spaced, without control characters (except `\n`
inside `Preformatted`), with soft hyphens and zero-width characters removed, and with
typographic punctuation kept (fonts on the device cover Latin-1, General Punctuation,
Latin Extended; unknown glyphs render as boxes, so emoji are stripped).

## 6. Renderers

**Default delivery format is EPUB** (text-only). CrossPoint's TXT reader cannot justify,
ignores line spacing, wraps only at ASCII spaces, and its layout cost grows with the
square of paragraph length (an 8 KB paragraph took 25 s to index on a Mac). An EPUB
with only paragraphs/headings is still "plain text" but reads properly, carries its
title in metadata, and supports bookmarks/progress sync. TXT is an option
(`delivery_format: txt`) for people who want literal `.txt` files.

### 6.1 TXT (`render/txt.py`)

CrossPoint renders `.txt` by turning every `\n` into a hard line break (no paragraph
spacing, no indent); the Library title is the **filename**.

```
<Title>
<byline> · <site> · <YYYY-MM-DD>          (empty parts omitted; line omitted if all empty)
———                                       (three U+2014)
                                          (blank)
<paragraph 1 on ONE line>
                                          (blank)
<HEADING>                                 (h1/h2 upper-cased, h3 as-is)
                                          (blank)
• list item / 1. list item                (two spaces of indent per nesting level)
                                          (blank)
> quoted text
                                          (blank)
<preformatted lines kept, tabs -> 4 spaces>
                                          (blank)
* * *                                     (rule)
                                          (blank)
———
Source: <full URL>                        (omitted when empty)
```

Rules: UTF-8 without BOM, `\n` only, trailing newline, no tabs/CR/NUL, single spaces,
any paragraph line longer than 900 bytes is split at a sentence boundary (else at a
space) into separate paragraphs (TXT layout cost is quadratic in line length).

### 6.2 EPUB (`render/epub.py`)

Minimal, valid EPUB 3 with an EPUB 2 NCX for older readers:

```
mimetype                      (first entry, stored, "application/epub+zip")
META-INF/container.xml
OEBPS/content.opf             dc:identifier urn:uuid, dc:title, dc:creator, dc:language, dc:date, dcterms:modified
OEBPS/nav.xhtml               EPUB 3 nav (toc)
OEBPS/toc.ncx                 EPUB 2 NCX
OEBPS/text/item-NNN.xhtml     one per item (digest = several)
```

Firmware-driven rules (CrossPoint's chapter parser is expat, strict XML, case-sensitive):

- Lowercase unprefixed tags only: `h1 h2 h3 p ul ol li blockquote hr br i`. No `pre`
  (unsupported, whitespace collapses): render Preformatted as one `<p>` per line with
  leading spaces as U+00A0. No CSS except an optional `style.css` with
  `blockquote{margin-left:1.5em;margin-right:1.5em}`. No images, no `<a>`.
- Never emit named entities; escape `& < > " '` and use raw UTF-8.
- Every text run inside a block element (text directly before a block tag glues).
- Nested lists go inside the parent `<li>` (epubcheck RSC-005 otherwise).
- `mimetype` first, stored, no extra field; other entries deflated; no ZIP64; no
  `./` in hrefs; container `media-type="application/oebps-package+xml"` exactly.
- Nav must be `nav epub:type="toc"` > `ol` > `li` > `a`, labels without newlines, nav
  **not** in the spine. Always one TOC entry per chapter labelled with the item title
  (status bar shows the chapter title; "Unnamed" otherwise).
- `dc:language` set (drives hyphenation); `dc:title` ≤ 500 bytes.
- Chapter body: `<h1>title</h1>`, `<p><i>byline</i></p>`, `<p><i>site · date</i></p>`,
  `<hr/>`, blocks, then `<hr/><p><i>Source: host</i></p>`. Never print long URLs in the
  body (hyphenated URLs overflow lines); the full URL goes in `dc:source` only.
- Every XML part is parsed with lxml before zipping.

Single-item EPUB: title = item title, creator = byline (or site). Digest EPUB (several
items): title `Inbox: <n> items (<YYYY-MM-DD HH:MM>)`, one chapter (spine item) per
item, nav lists them.

Must pass `epubcheck` 5.4.0 with zero errors/warnings, and load in CrossPoint's real
EPUB pipeline (host harness, 1.6.5 and 2.0).

## 7. Endpoints (port 8765, plain HTTP, LAN)

All responses: explicit `Content-Length`, never gzip, never 204/304, no redirects,
`Connection: close` on device endpoints.

### 7.1 Ingest — `POST /api/v1/ingest`

Auth: `Authorization: Bearer <ingest_token>` (or `?token=` for clients that cannot set
headers). Accepts:

- `application/json` `{"url"?, "text"?, "title"?, "html"?, "kind"?: "auto|url|text|html", "source"?}`
- `application/x-www-form-urlencoded` / `multipart/form-data` with the same field names, plus an optional `file` part (`.pdf`, `.txt`, `.html`, `.epub`)
- `text/plain` body (classified automatically)

Classification (`kind=auto`): body that is a single URL → `url`; URL plus short text
that is a substring of the page title → `url`; otherwise `text` with the first URL kept
as `source_url`. Response `200 {"ok":true,"id","title","status"}`; errors
`401/413/422 {"ok":false,"error"}`. Processing (fetch/extract) happens synchronously
with a 25 s budget so the Shortcut can show the resulting title; on timeout the item
is stored as `failed` with the raw URL so nothing is lost.

### 7.2 Device API for CrossPoint 2.x plugin (`Authorization: Bearer <device_token>`)

- `GET /device/v1/catalog?page=P&limit=L` → `{"items":[{"id","title","subtitle"}]}`.
  Returns up to `L` items starting at offset `(P-1)*(L-1)` (firmware sends
  `limit=page_size+1`, shows `page_size`, uses the extra row to detect a next page).
  Newest first. `subtitle` = `"<New|Read on X3 date> · <words> words · <host>"`.
- `GET /device/v1/items/<id>.epub` → application/epub+zip; `GET /device/v1/items/<id>.txt`
  → text/plain; charset=utf-8. Either marks `delivered.catalog`. The bundle's
  `download.url`/`filename` use the configured `delivery_format` (default epub).
- `GET /device/v1/deliver?ev=<event.id>&ts=<event.ts>` → zero-tap auto-delivery:
  - A repeat of a known `(ev, ts)` returns the same stored bytes.
  - Otherwise, if any `ready` item has no delivery on any channel: build an EPUB
    (single item, or digest of all pending up to 900 KB, oldest first), store it under
    `(ev, ts)`, mark items `delivered.auto`, return `200 application/epub+zip`.
  - Nothing pending → `404` with a small body and explicit Content-Length (the device
    keeps the event queued and retries later; harmless).
  - The device writes it to `/Inbox/<ts>-<ev>.epub`; titles come from EPUB metadata.

### 7.3 OPDS for CrossPoint 1.6.5+ (`GET /opds`, optional HTTP Basic: user `x3`, password = device_token)

OPDS 1.2 Atom acquisition feed, newest first, max 50 entries per page with
`rel="next"`/`rel="previous"` absolute hrefs. Each entry: `title`, `author/name`,
`id` (`urn:sendtox3:<id>`), `updated`, `link rel="http://opds-spec.org/acquisition"
type="application/epub+zip" href="/opds/items/<id>.epub"` (root-relative). Titles
≤ 120 bytes. Downloads mark `delivered.opds`.

### 7.4 Admin UI

Served on HA ingress (auth by HA) and on the LAN port at `/admin` (requires
`admin_token` cookie/`?token=`). Shows the queue, item previews, delivery status,
manual paste form, delete/requeue, channel health (IMAP, HA share, LAN push), tokens,
SD-plugin bundle download, "Install plugin on my X3" (pushes the bundle to a device in
File Transfer mode), and setup instructions with QR codes.

## 8. Firmware-safe titles (`render/names.py`)

Applied to every title before it is stored:

- `clean_text` (NFC, no controls/emoji, font substitutions), single line.
- Strip site affixes (` - Wikipedia`, `GitHub - `, ` | Site Name` when it equals the site).
- The firmware refuses any filename containing `..`: replace runs of 2+ `.` and `…`
  with `…` (U+2026, not a `.`).
- Replace `/ \ : * ?  " < > |` with readable substitutes (`/`→`-`, `:`→` -`, `?`→``,
  `"`→`'`, `<`/`>`→``, `|`→`-`, `*`→``, `\`→`-`).
- Remove `{` and `}` (the firmware expands placeholders found inside earlier values).
- Break the 8.3 short-alias pattern `~<digit>` (`~1` → `- 1`).
- Leading dots/spaces removed (hidden-file rule), trailing dots/spaces trimmed.
- Cap at 90 UTF-8 bytes (firmware cap is 100 minus the extension), cut on a
  character boundary, preferring a word boundary. Empty → `Note <YYYY-MM-DD HH:MM>`.
- Unique among stored items: append ` (2)`, ` (3)`, ...

Display/OPDS/EPUB metadata may use the same safe title (keeps everything consistent).

## 9. Ingest workers

- **IMAP** (`workers/imap.py`): `imaplib` (Python 3.14 has `IMAP4.idle`). Connect TLS,
  select configured folder, `UID SEARCH UNSEEN`, process, flag `\Seen`, optionally move
  to a processed folder/label; IDLE ≤ 25 min; reconnect with exponential backoff
  5 s → 5 min. Acceptance filter: recipient contains the secret plus-tag (if set);
  From in `allowed_senders` (if set); topmost `Authentication-Results` shows
  `dmarc=pass` or aligned `dkim=pass` (when `require_dmarc` is on). Size cap 25 MB.
  Each message can yield several items: body text, each URL-only body, each PDF
  attachment, each `.txt`/`.html` attachment.
- **HA share** (`workers/ha_share.py`): when `SUPERVISOR_TOKEN` is present and
  `ha_share_enabled`, connect `ws://supervisor/core/websocket`, auth with the token,
  `subscribe_events` for `mobile_app.share`; event data `text`/`url`/`subject` →
  IngestRequest. Reconnect with backoff.
- **LAN push** (`workers/lan_push.py`): when `device_host` is set, poll
  `http://<device_host>/api/status` every `push_poll_seconds` (default 20 s, 2 s
  timeout). When it answers, `POST /mkdir` `/Inbox` (ignore "exists"), then for each
  ready item without `delivered.push`, `POST /upload?path=/Inbox` multipart with
  filename `<title>.txt`; on `400 File already exists` retry with ` (2)`... suffix; verify
  with `GET /api/files?path=/Inbox`. Mark `delivered.push`. One file per request.

## 10. CrossPoint 2.x plugin bundle (`plugin_bundle.py`)

Folder `/.crosspoint/plugins/sendtox3/` (hidden root so the Library does not list the
plugin's own files) containing a generated `device.json` with the server URL and
device token **hard-coded** (no `config.json`, so a missing config can never produce
literal `{cfg.server}` URLs), plus `manifest.json` (title/description) and `README.md`.
Also the empty `/Inbox/` folder (event downloads do not create folders).

```json
{
  "title": "Send to X3",
  "description": "Articles and notes you shared",
  "browse": {
    "format": "json",
    "url": "http://<host>:8765/device/v1/catalog?page={page}&limit={limit}",
    "headers": {"Authorization": "Bearer <device_token>"},
    "items": "items",
    "fields": {"title": "title", "author": "subtitle", "id": "id"},
    "page_size": 8
  },
  "download": {
    "url": "http://<host>:8765/device/v1/items/{id}.epub",
    "headers": {"Authorization": "Bearer <device_token>"},
    "dest_dir": "/Inbox",
    "filename": "{title}.epub"
  },
  "events": {
    "sleep.enter": {
      "download": {
        "url": "http://<host>:8765/device/v1/deliver?ev={event.id}&ts={event.ts}",
        "headers": {"Authorization": "Bearer <device_token>"},
        "dest": "/Inbox/{event.ts}-{event.id}.epub"
      },
      "toast": "Send to X3: new reading delivered"
    }
  }
}
```

Options: `auto_delivery` (bool, default true) removes the `events` block when false;
`delivery_format` (`epub`|`txt`, default `epub`) picks the catalog download format.
Auto-delivery is always EPUB (the filename cannot carry a title; EPUB metadata does).

## 11. HA app options

Defined once in `sendtox3/config.py` (`Options`); `config.yaml` `options`/`schema` must
mirror it exactly (a unit test enforces this). Secrets left empty are generated on
first start and persisted (`Store.secret`).

## 12. Security

- LAN port exposes only token-protected endpoints (`/healthz` is open). Tokens are 32
  random URL-safe chars, compared in constant time.
- The fetcher blocks private/loopback/link-local destinations unless
  `allow_private_urls` is on (SSRF), limits redirects to 5, body to 10 MB, time to
  `fetch_timeout_seconds`, and only follows http/https.
- Email acceptance filter as in §9.
- No inbound internet exposure is required for any feature.

## 13. Testing levels

1. **Unit** (`tests/`): extraction, text model, renderers (TXT layout, EPUB structure),
   names, ingest classification, email parsing, catalog paging math, OPDS XML,
   delivery idempotence, PDF heuristics.
2. **Integration** (`tests/integration/`): aiohttp test client against the real app with
   a temp `/data`; fixture HTTP server for article fetches; GreenMail (docker) IMAP;
   fake HA websocket; fake CrossPoint File Transfer server.
3. **Firmware e2e** (`e2e/`): the real CrossPoint firmware compiled with
   `crosspoint-simulator` for the X3 (`2.0.0rc` and `1.6.5`), headless, with an SD
   folder produced by our bundle generator, driving real catalog browse/download,
   sleep-entry auto-delivery, OPDS browse/download, File Transfer push, and opening
   the delivered files in the real reader with screenshots. Plus a wire harness that
   compiles the device's own HTTP client to check exact framing against our server.
4. **Packaging**: build the arm64 image, run it with an `options.json`, validate
   `config.yaml` with the Supervisor's schema, and run the app inside the HA
   devcontainer Supervisor.
5. **Validation**: `epubcheck` on every generated EPUB fixture.

## 14. Module interfaces (stable contracts between components)

Package root `send_to_x3/sendtox3`. Python 3.14. Tests run from the repo root with
`pytest` (`pytest.ini` sets `pythonpath = send_to_x3`). Network is never used in unit
tests; integration tests use local fixture servers. Each module owner writes tests in
`tests/test_<module>.py` (and `tests/integration/` for cross-module flows).

Already written (shared, do not change signatures without updating every caller):
`textmodel.py` (Document, blocks, `clean_text`, `font_covers`), `models.py`
(IngestRequest, Item, `new_item_id`), `config.py` (Options, `load_options`),
`store.py` (Store).

```python
# extract/fetch.py
class FetchError(Exception): ...            # .reason: str (user-facing)
@dataclass
class FetchResult:
    url: str                                # final URL after redirects, cleaned
    status: int
    content_type: str                       # lowercased mime without params
    charset: str | None
    headers: dict[str, str]                 # lowercased names
    body: bytes
    blocked: str | None                     # e.g. "Cloudflare challenge", "HTTP 403"
async def fetch_url(url: str, *, timeout: float = 20, max_bytes: int = 8_000_000,
                    allow_private: bool = False) -> FetchResult
def clean_url(url: str) -> str              # strip tracking params, unwrap redirectors
def decode_html(body: bytes, charset_hint: str | None) -> str

# extract/html.py
def html_to_document(html: str, *, url: str = "") -> Document
async def url_to_document(url: str, *, opts: Options, title_hint: str = "") -> Document
    # fetch + route by content type: HTML -> trafilatura; site APIs (GitHub README,
    # Wikipedia, Substack, Bluesky, X oEmbed); PDF -> extract.pdf; text/plain -> text.
    # Never raises for remote problems: returns a Document with a note and whatever
    # text was available (paywall/blocked/JS-only), plus the URL.

# extract/text.py
def text_to_document(text: str, *, title: str = "", source_url: str = "") -> Document
def classify(text: str, url: str = "", title: str = "") -> Literal["url", "text"]
def first_url(text: str) -> str
def derive_title(text: str) -> str          # first sentence/line, <= 80 chars

# extract/email.py
@dataclass
class ParsedEmail:
    subject: str; from_addr: str; to_addrs: list[str]; message_id: str
    auth_results: str                       # topmost Authentication-Results value
    dmarc_pass: bool; dkim_aligned_pass: bool
    requests: list[IngestRequest]           # body text/url, PDF/txt/html attachments
def parse_email(raw: bytes) -> ParsedEmail
def accept_email(p: ParsedEmail, opts: Options) -> tuple[bool, str]   # (ok, reason)

# extract/pdf.py
def pdf_to_document(data: bytes, *, title_hint: str = "", source_url: str = "") -> Document

# render/names.py
def safe_title(title: str, *, max_bytes: int = 90) -> str
def unique_title(title: str, taken: set[str], *, max_bytes: int = 90) -> str
# render/txt.py
def render_txt(doc: Document) -> bytes
# render/epub.py
def render_epub(docs: list[Document], *, book_title: str = "", identifier: str = "") -> bytes

# ingest.py
class Processor:
    def __init__(self, store: Store, opts: Options): ...
    async def process(self, req: IngestRequest) -> Item    # never raises; failed -> status="failed"
    async def process_many(self, reqs: list[IngestRequest]) -> list[Item]

# delivery.py
def build_auto_delivery(store: Store, ev: str, ts: str) -> bytes | None   # None -> 404
def catalog_page(store: Store, page: int, limit: int) -> dict             # {"items":[...]}

# plugin_bundle.py
def build_device_json(*, base_url: str, device_token: str, opts: Options) -> dict
def build_bundle_zip(*, base_url: str, device_token: str, opts: Options) -> bytes
    # zip with .crosspoint/plugins/sendtox3/{device.json,manifest.json,README.md} and Inbox/.keep
async def install_to_device(device_host: str, *, base_url: str, device_token: str,
                            opts: Options) -> list[str]   # via device web server; returns log lines

# web/app.py
def create_app(opts: Options, store: Store, processor: Processor) -> aiohttp.web.Application

# workers/*.py — each exposes:  async def run(app_state) -> None  (long-running, cancellable)
```
