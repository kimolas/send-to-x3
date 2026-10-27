# Send to X3 — architecture and contracts

This is the design spec every component is built against. Firmware facts are from
CrossPoint source read at tag `1.6.5` and at `2.0.0rc`/develop `3f387a9` (2026-10-25).
Everything else was checked against the code in `send_to_x3/sendtox3/` after the second
review-fix round (2026-10-25).

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
 web UI (paste, upload) ──────────────────────────────────────▶┤
                                                               │
                                     device API (HTTP :8765) ◀─┼──── 2.x plugin: catalog + sleep auto-delivery
                                     OPDS feed  (HTTP :8765) ◀─┼──── 1.6.5+: OPDS browser (EPUB)
                                     LAN push worker ──────────┴───▶ 1.6.5+: File Transfer /upload
```

Everything server-side is one Python process (`sendtox3`) packaged as a Home Assistant
OS app. The same container also runs with plain `docker run` (no Supervisor) for
development and tests. PDF conversion runs in short-lived child processes (§12).

## 3. Repository layout

```
repository.yaml                 HA app repository manifest
send_to_x3/                     the HA app (folder name = slug)
  config.yaml  Dockerfile  run.sh  DOCS.md  README.md  CHANGELOG.md
  icon.png  logo.png  translations/en.yaml
  requirements.txt              pinned runtime deps
  sendtox3/                     python package (copied into the image)
    __main__.py                 entry: load options, start services, recover the spool
    config.py                   Options dataclass from /data/options.json (+ env overrides for dev)
    app_state.py                runtime state shared by web app and workers, channel status, secrets
    store.py                    SQLite queue + file storage under /data, spool, issued titles
    models.py                   IngestRequest, Item dataclasses
    textmodel.py                Document/Block intermediate representation, clean_text
    ingest.py                   IngestRequest -> Item (classification, dedupe, budgets, spool)
    delivery.py                 sleep auto-delivery (digest, replay), catalog pages, doc.json
    device_client.py            client for the X3's File Transfer web server
    plugin_bundle.py            CrossPoint 2.x SD plugin folder/zip, install, server address
    phone.py                    Android HTTP Shortcuts import file, iOS note for the Setup page
    extract/
      html.py                   HTML -> Document (article extraction), URL -> Document routing
      fetch.py                  safe URL fetcher (size/time/redirect limits, SSRF guard)
      sites.py                  site APIs (GitHub, Wikipedia, Substack, Bluesky, X)
      text.py                   pasted text -> Document (paragraphing, URL detection)
      email.py                  RFC822 -> list[IngestRequest], acceptance filter
      pdf.py                    PDF -> Document in an isolated worker process
      pdfengine/                the PDF layout engine (PDFium via pypdfium2, OCR via tesseract)
    render/
      txt.py                    Document -> .txt bytes (CrossPoint TXT conventions)
      epub.py                   Document(s) -> minimal EPUB bytes
      names.py                  firmware-safe titles/filenames
    web/
      app.py                    aiohttp app factory, routes, framing middleware
      common.py                 token checks, framing-safe responses
      device_api.py             2.x plugin catalog / item / deliver endpoints
      opds.py                   OPDS 1.2 acquisition feed for 1.6.5
      ingest_api.py             POST /api/v1/ingest
      admin.py                  admin UI (served via HA ingress + token on LAN)
      templates/ static/
    workers/
      imap.py                   IMAP IDLE poller
      ha_share.py               HA Core websocket subscriber (mobile_app.share)
      lan_push.py               pushes items to the device's File Transfer server
tests/                          pytest unit + integration tests (no network)
e2e/                            end-to-end harnesses (docker, firmware simulator, wire harness)
shortcuts/                      iOS Shortcut generator, Android import file generator
docs/                           user documentation
scripts/check_all.sh            runs every verification level
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
| `title` | str | firmware-safe, unique title (see §8); also the file name on the X3 |
| `byline` | str | author, or the sender for notes, email bodies and text files; may be empty |
| `site` | str | site name or host; may be empty |
| `source_url` | str | may be empty; never a `mailto:`/`tel:` link |
| `status` | `ready` \| `failed` | failed items keep an error message and a deliverable fallback body |
| `error` | str | |
| `word_count` | int | |
| `delivered` | json | `{channel: unix_ts}` for `catalog`, `auto`, `opds`, `push` |
| `dedupe_key` | str | normalized URL (plus a hash of the sharer's comment, if any), or sha256 of the text or file bytes; a ready item with the same key within 24 h is returned instead of a new one |

Storage under the data folder (`/data` in the add-on, `$SENDTOX3_DATA_DIR` elsewhere):

```
sendtox3.db                SQLite (WAL): items, deliveries, kv, pending_requests, issued_titles
items/<id>/item.epub       always present
items/<id>/item.txt        always present
items/<id>/doc.json        the Document (re-rendered into digests); absent for uploaded EPUBs
deliveries/<n>.epub        stored auto-delivery payloads (byte-identical replays)
spool/<id>.bin             file bytes of a share still being processed
```

- `deliveries(ev, ts, item_ids, file, created_at, hits)`: a retried device request
  gets byte-identical output; `hits` counts repeats (shown as "requested N×").
- `kv`: generated secrets (`secret:<name>`), IMAP marks (UIDVALIDITY, last UID,
  attempt counts), auto-delivery skips, one-time migration flags.
- `pending_requests` + `spool/`: the spool (§7.1, §14).
- `issued_titles(key, title, issued_at)`: titles of items downloaded through the
  catalog or OPDS (saved on the X3 as `<title>.<ext>`), kept 365 days so a later item
  never gets the same file name. A one-time migration at store open fills it from items
  downloaded before the table existed (flag in `kv`; retried at the next start if it
  fails).
- Folders are created 0700 and files 0600 whatever the umask (the database holds the
  tokens); stale `*.tmp` files older than an hour in `spool/` and `deliveries/` are
  removed at store open.
- Retention: `retention_days` (default 30) prunes items and stored deliveries daily,
  starting 60 s after start. Files on the X3 are never touched.

## 5. Intermediate text model

```python
Block = Heading(text, level: 1..3) | Paragraph(text) | ListItem(text, ordered, index, depth)
      | Quote(text) | Preformatted(text) | Rule()
Document(title, blocks, byline="", site="", source_url="", published="", lang="en", notes=[])
```

`notes` are short remarks shown at the top of the item ("Note: ..."), for example a
paywall notice or the sharer's `Comment: ...`.

All text is NFC-normalized, single-spaced, without control characters (except `\n`
inside `Preformatted`), with soft hyphens and zero-width characters removed, without
lone surrogates or noncharacters (U+FFFE, U+FFFF, U+FDD0–FDEF), and with typographic
punctuation kept (fonts on the device cover Latin-1, General Punctuation, Latin
Extended; unknown glyphs render as boxes, so emoji are stripped).

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
Note: <note>                              (one per note, if any)
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
space) into separate paragraphs (TXT layout cost is quadratic in line length). The TXT
keeps every URL in full.

### 6.2 EPUB (`render/epub.py`)

Minimal, valid EPUB 3 with an EPUB 2 NCX for older readers:

```
mimetype                      (first entry, stored, "application/epub+zip")
META-INF/container.xml
OEBPS/content.opf             dc:identifier urn:uuid, dc:title, dc:creator, dc:language, dc:date, dcterms:modified
OEBPS/nav.xhtml               EPUB 3 nav (toc)
OEBPS/toc.ncx                 EPUB 2 NCX
OEBPS/text/item-NNN.xhtml     one or more per item (digest = several items)
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
  one `<p><i>Note: ...</i></p>` per note, `<hr/>`, blocks, then
  `<hr/><p><i>Source: host</i></p>`. Never two `<hr/>` in a row, and never one at the
  top of a chapter part.
- Never print long URLs in the body (hyphenated URLs overflow lines): links longer
  than 30 characters in paragraphs, list items, quotes, h2/h3 and notes become
  `[host]`; an attached `'s`/`'d`/`'ll`/`'re`/`'ve`/`'m` stays outside the link. A
  paragraph that is only the document's own URL (same host without `www.`, path, query
  without tracking parameters, and `#/`/`#!` routes) is dropped. Preformatted lines keep
  their URLs. The full URL goes in `dc:source` only.
- A chapter with no text left says "There is no text here, only a link to <host>.
  Open it on another device to read it." (or "There is no text here." without a host).
- Items over 60 KB of text are split into several chapters at level-2 headings (or
  paragraph boundaries), labelled with the heading or "<title> (continued)".
- Every XML part is parsed with lxml before zipping.

Single-item EPUB: title = the item's headline (§8), creator = byline (or site). Digest
EPUB (several items): title `Inbox: <n> items (<YYYY-MM-DD HH:MM>)`, one or more
chapters per item, nav lists them.

Must pass `epubcheck` 5.4.0 with zero errors/warnings, and load in CrossPoint's real
EPUB pipeline (host harness, 1.6.5 and 2.0).

## 7. Endpoints (port 8765, plain HTTP, LAN)

All responses: explicit `Content-Length`, never gzip, never 204/304, no redirects (a
handler that raises one is turned into a 500), `Connection: close` on device
endpoints. `GET /healthz` is open and answers `{"ok":true,"version":"..."}`.

### 7.1 Ingest — `POST /api/v1/ingest`

Auth: `Authorization: Bearer <ingest_token>` (or `?token=` for clients that cannot set
headers). Bodies (at most 30 MB):

- `application/json` `{"url"?, "text"?, "title"?, "html"?, "kind"?: "auto|url|text|html|pdf", "source"?}`
- `application/x-www-form-urlencoded` / `multipart/form-data` with the same field
  names, plus at most one file part (`.pdf`, `.txt`, `.html`, `.epub`)
- `text/plain` body (classified automatically), `text/html` body
- a raw file (`application/pdf`, `application/x-pdf`, `application/epub+zip`,
  `application/octet-stream`; name from `?filename=`)

Missing fields are also read from the query string. `source` is one of `http`,
`email`, `ha_share`, `admin` (anything else becomes `http`).

**Classification** (`kind=auto`, `Processor._split_links` then `_run_auto`). A "web
link" is `http(s)://...` or a bare `www.` host; `mailto:`, `tel:` and the like are
never fetched or used as a source. In the `url` field (not free text), a host name
without a scheme whose last label is a public-looking top-level domain
(`example.com/article`, `www.example.com`, `example.com:8080/x`) gets `https://`;
`notes.txt`, `report.pdf`, IP addresses and `localhost` stay notes.

1. A file part → by its type: PDF (converted, §12), EPUB (kept byte for byte, text
   extracted only for the TXT and preview), HTML, TXT. Unknown types → 422.
2. `html` without text → an HTML document (article extraction; an email body is
   converted whole).
3. **Several bare links**: 2 to 10 distinct web links and at most 30 other words
   (300 characters) → one `url` item per link. The share's title is dropped; the
   remark, unless it is filler, is kept on each item as a `Comment:` note.
4. **Only a link** (no text) → a `url` item: the fetched article. A non-web `url` with
   no text → a text note of it.
5. **A `url` the text does not mention** → the text was selected on that page. If the
   text is (part of) the page title (`matches_title`) or share-sheet filler → plain
   `url` item. A short text (≤ 30 words) with no title given makes the app fetch the
   page once to compare with its title. Otherwise a `text` item citing the page
   (`source_url`), whose byline is not the sender.
6. **One link plus at most 30 words** (300 characters) → a `url` item: the fetched
   article, with the words kept as a `Comment: ...` note, unless they are filler
   ("Check out this article", "Shared via ...", "via @x") or the page title. The
   dedupe key then includes a hash of the comment, so a new remark on a link already in
   the queue is a new item.
7. **Anything else** → a `text` item; `source_url` is the given web `url`, else the
   first web link in the text (for `source=email`, only the link the email parser
   chose).

`matches_title(text, title, *, url, site)` matches when, after normalising case,
separators and outer punctuation, the text is the title or part of it; one is the other
plus an affix of at most 5 words split at ` | `, ` - `, ` – `, ` — `, ` · ` or `: `; the
text is the title plus only the site name or host; or the text holds a title of 12+
characters with at most 60 more.

Explicit kinds: `url` fetches `url` or the first link in `text`; `text` stores a
note; `html` converts `html`; `pdf` without a file is a link to a PDF.

**Responses** (`web/ingest_api.py`):

- `200 {"ok":true,"id","title","status","duplicate","error","message","items":[...]}`:
  the first item's fields at the top level (what the phone shortcuts read), every item
  in `items`. `message` is "Sent to X3: <title>", "Already in the queue: <title>",
  "Saved, but could not extract the text: <reason>", or "Sent <n> items to X3: ...".
- `202 {"ok":true,"status":"processing","message":"Received <what>; still processing.
  It will appear on the X3 shortly."}` when processing takes longer than 25 s
  (`RESPONSE_WAIT_SECONDS`). Processing runs in a detached task and continues; a client
  that hangs up does not lose the share. A share still inside its 25 s wait when the
  app shuts down also gets this 202 and is redone at the next start.
- `401/413/422 {"ok":false,"error"}`.
- `503 {"ok":false,"error":"Not saved: the server could not store it (...). Try again
  later."}` on `StorageError` (disk full, database error): nothing was stored. A
  storage failure after the 202 is logged and shown as a warning on the admin queue
  page until the next successful share.

**Budgets.** Every request, from every channel, gets the background budget:
150 s (`BACKGROUND_BUDGET_SECONDS`), plus 510 s when `pdf_ocr` is on (660 s in all).
All network work for one link shares `fetch_timeout_seconds` (default 20 s). A linked
PDF is converted with its own limit of 120 s (600 s with OCR); an uploaded or attached
PDF gets the remaining budget minus 10 s (at least 1 s), so the converter stops first
and its reason is kept. When the budget runs out, the worker is killed, and the item is
stored as `failed` with "Timed out after <n> s while <converting the PDF | fetching the
page | reading the EPUB | processing the share>" and a fallback body (title, link,
the reason, any shared text), so nothing is lost. A link that turns out to be a PDF is
converted inside `extract/html.py`, which reports the phase back through
`url_to_document(..., on_phase=...)`, so its message also says "converting the PDF".

**Spool.** Before processing, each request is written to the spool
(`pending_requests` row, file bytes in `spool/<id>.bin`, keyed by a content hash so
retries and double-taps share one entry) and removed once its items are stored.
`Processor.recover_pending()` runs once in the background after the web server is up:
it redoes entries left by an earlier run, counts attempts (the first included) and
stores a request tried 3 times as a failed item, records a failed item when a spooled
file is missing, and stops at the first `StorageError`. At shutdown, shares still
processing get 5 s, the rest are cancelled and stay spooled. Shares still waiting in
the HA share worker's in-memory queue are not spooled. Entries are treated as in
flight by wall-clock time (`created_at` ≥ start of this run).

### 7.2 Device API for CrossPoint 2.x plugin (`Authorization: Bearer <device_token>`)

- `GET /device/v1/catalog?page=P&limit=L` → `{"items":[{"id","title","subtitle"}]}`.
  `limit` is clamped to 2..17. Returns up to `L` items starting at offset
  `(P-1)*(L-1)` (firmware sends `limit=page_size+1`, shows `page_size`, uses the extra
  row to detect a next page). Newest first. `subtitle` =
  `"<New|Read on X3 date> · <words> words|extraction failed · <host|site|note|email|PDF>"`.
- `GET /device/v1/items/<id>.epub` → application/epub+zip; `GET /device/v1/items/<id>.txt`
  → text/plain; charset=utf-8. Either marks `delivered.catalog`. The bundle's
  `download.url`/`filename` use the configured `delivery_format` (default epub).
- `GET /device/v1/deliver?ev=<event.id>&ts=<event.ts>` → zero-tap auto-delivery:
  - With `auto_delivery` off: always `404 nothing new`, nothing is marked, and the
    device channel status shows "The X3 still requests sleep delivery although
    auto_delivery is off: reinstall the plugin to stop it (it also joins Wi-Fi at each
    sleep)." until the next catalog request.
  - A repeat of a known `(ev, ts)` returns the same stored bytes.
  - Otherwise, if any `ready` or `failed` item has no delivery on any channel: build an
    EPUB (single item, or a digest of all pending items up to 900 KB, oldest first),
    store it under `(ev, ts)`, mark items `delivered.auto`, return `200
    application/epub+zip`. An oldest item over 900 KB goes alone up to 1,000,000 bytes
    (the device aborts event downloads above 1 MiB); larger, it gets a small "Too large
    to deliver: <title>" EPUB once, stays pending (catalog "New", LAN push still sends
    it) and is left out of later zero-tap deliveries until requeued. An item whose EPUB
    is missing or empty is marked failed and skipped. Uploaded EPUBs (no `doc.json`)
    always go alone.
  - Nothing pending → `404` with a small body and explicit Content-Length (the device
    keeps the event queued and retries later; harmless).
  - The device writes it to `<inbox_folder>/<ts>-<ev>.epub`; titles come from EPUB
    metadata.

### 7.3 OPDS for CrossPoint 1.6.5+ (`GET /opds`, HTTP Basic: any user, `x3` by convention, password = device token; a Bearer device token also works)

OPDS 1.2 Atom acquisition feed, newest first, 25 entries per page with
`rel="next"`/`rel="previous"` absolute hrefs. Each entry: `title`, `author/name`,
`id` (`urn:sendtox3:<id>`), `updated`, `link rel="http://opds-spec.org/acquisition"
type="application/epub+zip" href="/opds/items/<id>.epub"` (root-relative). Titles
≤ 120 bytes; the author is the short origin label, left out when `<author> - <title>`
would not fit the firmware's 100-byte file name. Text is cleaned of everything that is
not valid XML 1.0. Without credentials the feed answers 401 with `WWW-Authenticate:
Basic` (2.x sends Basic only after a 401; 1.6.5 sends it up front). Downloads mark
`delivered.opds`.

### 7.4 Admin UI

Served on HA ingress (trusted only from `172.30.32.2` with an `X-Ingress-Path` header;
HA has authenticated the user) and on the LAN port at `/admin` (`?token=<admin_token>`
once, or the sign-in form, then a signed session cookie for 30 days). Pages:

- **Queue** (`/admin`): warnings, **Send something** (paste text or a link, optional
  title; upload a PDF, TXT, HTML or EPUB up to 30 MB), **Channels** (status of the X3
  plugin, OPDS, LAN push, email and the HA share sheet, plus **Recent automatic
  deliveries**), and the queue (50 per page) with **Requeue** and **Delete**.
- **Item** (`/admin/items/<id>`): details, EPUB/TXT downloads, preview, **Deliver
  again**, **Delete**.
- **Setup** (`/admin/setup`): the detected server address and its source, the tokens
  with **Copy** buttons, **New device token** (POST `/admin/rotate-device-token`),
  **Install plugin on my X3 now** (POST `/admin/install-plugin`), **Download SD-card
  plugin bundle (zip)** (`/admin/plugin-bundle.zip`), the OPDS settings, the iOS
  note and a hand-built recipe, **Download HTTP Shortcuts file**
  (`/admin/setup/android.json`, prefilled with address, ingest token and, with email on,
  the plus address), a `curl` example and the email address. QR codes for the OPDS and
  ingest addresses and the ingest token.

The bundle, the install and the Android file answer 409 while the server address is
unknown, `server_host` is unusable, or (under Home Assistant only) the address is a
detected Docker-internal 172.16.0.0/12 guess.

## 8. Titles: firmware-safe file names and the displayed headline (`render/names.py`, `ingest.display_title`)

Every item has two titles. Both start from the document title, which for a fetched
page the extractor has already cleaned (`extract/html.py` `clean_title`): it drops
` - Wikipedia`, a leading `GitHub - ` (only on github.com or when the site name is
GitHub), a trailing segment naming the site (equal to, or starting with, the site name
or host label), and a leading segment only when it exactly equals the site name
(`og:site_name`); a leading `Site: ` additionally needs 4 or more words after it
("Python: the language" on python.org is kept). Never fewer than 3 characters remain.

**The safe title** (`safe_title`/`unique_title`) is the item title: the file name on
the X3, the catalog and OPDS row, the web UI. Rules:

- `clean_text` (NFC, no controls/emoji, font substitutions), single line.
- Strip well-known site decorations: ` - Wikipedia`, ` · GitHub`, `GitHub - `,
  ` - YouTube`, ` | Medium`.
- The firmware refuses any filename containing `..`: replace runs of 2+ `.` and `…`
  with `…` (U+2026, not a `.`).
- Replace `/ \ : * ?  " < > |` with readable substitutes (`/`→`-`, `:`→` -`, `?`→``,
  `"`→`'`, `<`/`>`→``, `|`→`-`, `*`→``, `\`→`-`).
- Remove `{` and `}` (the firmware expands placeholders found inside earlier values).
- Break the 8.3 short-alias pattern `~<digit>` (`~1` → `- 1`).
- Leading dots/spaces removed (hidden-file rule), trailing dots/spaces trimmed.
- Cap at 90 UTF-8 bytes (firmware cap is 100 minus the extension), cut on a
  character boundary, preferring a word boundary. Empty → `Note <YYYY-MM-DD HH.MM>`.
- Unique (case-insensitively, like FAT) among stored items, titles reserved by
  requests in flight, and `issued_titles` (§4): append ` (2)`, ` (3)`, ...

**The headline** (`display_title`) is what the book shows: the chapter `<h1>`, the
nav label, `dc:title` and the TXT header. It is the cleaned original title, not the
safe one (which loses `?`, `:`, quotes and everything past 90 bytes):

- `strip_display_affixes` drops a trailing ` | Site`, ` - Site`, ` – Site`, ` — Site`
  or ` · Site` when Site is the page's site name, the host or the host's first label,
  or Wikipedia, GitHub or YouTube (Medium only after `|`); up to two such segments. A
  leading `Site: ` is removed only when it is exactly the site name and 4+ words
  remain; `GitHub - ` at the start only for github.com URLs. (On its own,
  `display_title` keeps "Python: the language" from python.org; but for a fetched page
  the extractor's `clean_title` above has already removed the leading "Python: ".)
- Capped at 255 bytes; a ` (n)` counter the safe title got for uniqueness is kept, so
  two books with the same headline stay distinguishable in the Library.
- Digests use the stored headline (`doc.json`) unless the item was renamed since.

## 9. Ingest workers

- **IMAP** (`workers/imap.py`): `imaplib` on one dedicated thread. Connect IMAPS (or
  STARTTLS on port 143), log in, select `imap_folder`.
  - **Fail closed.** The worker does not connect at all, reports state `not_configured`
    (an **error** badge) with the reason, and leaves the last-handled mark untouched
    when `imap_host`/`imap_user`/`imap_password` is empty ("set imap_user, imap_password
    to enable email ingest"), when neither `allowed_senders` nor `imap_plus_tag` is set
    (`NO_SENDER_FILTER`: "allowed_senders and imap_plus_tag are both empty, so no mail
    is accepted: list the addresses you send from in allowed_senders ('*' accepts any
    sender) and/or set imap_plus_tag"), or when `allowed_senders` contains `'*'` while
    the folder is INBOX (any case, padded or empty) and no plus-tag is set
    (`WILDCARD_IN_INBOX`). `accept_email` rejects everything in the same cases
    (`sender_filter_problem`).
  - **Finding mail.** First sync of a folder/UIDVALIDITY: `UID SEARCH UNSEEN`, limited
    to mail since yesterday in an INBOX without a plus-tag (the user's own mailbox, not
    a backlog of shares). Afterwards `UID SEARCH UID <last+1>:*` (new mail, even if a
    phone already opened it). Watching INBOX without a tag also puts a warning in the
    channel status.
  - **Acceptance filter** (`accept_email`, in order): size ≤ 25 MB; addressed to the
    secret plus-tag (if set); From in `allowed_senders` (an entry without `@` means the
    whole domain; `'*'` anyone, only with a tag or a dedicated folder; an empty list
    leaves the decision to the tag); topmost `Authentication-Results` shows `dmarc=pass`
    or aligned `dkim=pass` (when `require_dmarc` is on; when `imap_host` belongs to
    Gmail, Fastmail, Yahoo or Microsoft 365/Outlook.com, that header must also carry the
    provider's own authserv-id (Microsoft's has none); other hosts get a startup log
    warning); something readable in it. Headers are fetched first
    (`BODY.PEEK[HEADER]`, `RFC822.SIZE`), so unwanted or oversized mail is never
    downloaded; the full message is accepted again after parsing. Parsing runs off the
    event loop.
  - **Refused mail is always left untouched** (unread, in place). The reason is logged
    as a warning ("IMAP: message <uid> rejected: ...") when the mail was meant for the
    app, else only at debug level.
  - **Accepted mail** → `process_many(requests)`, then `\Seen` and, with
    `imap_processed_folder`, `UID MOVE` (or COPY + `\Deleted` + EXPUNGE).
  - **Attempt counter.** Before the download, the attempt is stored in `kv`. A lost
    connection (`IMAP4.abort`, `OSError`, `EOFError`), a shutdown (`CancelledError`,
    `KeyboardInterrupt`, `SystemExit`) or a `StorageError` puts the count back: the
    message stays unread and is retried later, however often. Anything else counts (a
    NO to the FETCH, `MemoryError`, `RecursionError` or a bug while parsing or
    processing); after 3 attempts, even across restarts, the message is marked read
    with no item and the error is logged. Headers that cannot be parsed reject the
    message (left unread).
  - **Waiting:** IDLE re-issued every 25 min, or polling every 60 s without IDLE;
    reconnect with exponential backoff 5 s → 5 min.
  - **What a message becomes** (`parse_email`): the body text, links in it (a body
    of 1 to 10 links plus at most 30 words / 300 characters is a link share, by the same
    rule as §7.1; each link with a comment goes out as `auto` text "comment + link"),
    each PDF, `.txt` and `.html` attachment; at most 50 requests per message. An email
    body in HTML is converted whole (`html_to_document(..., whole=True)`), without
    article extraction. Newsletter chrome is removed only in strict forms
    (`extract/html.py` `is_mail_chrome` / `is_mail_chrome_link`: a block or line that
    is only footer links such as "Unsubscribe", "View in browser", "Manage
    preferences", or that starts with "You are receiving this ... because" / "This
    email was sent to ..."), and only in the first 3 / last 8 blocks or lines (each
    forwarded message separately; `email.py` at any length, `html.py` only for bodies
    of 13+ blocks). Ordinary sentences that mention subscriptions or browsers stay. A `[x3]` mail whose body starts with a link (the iOS Shortcut's
    email route) is handed on exactly as the Shortcut's HTTP request: only the link →
    `url`; link + text → `auto` with `url=<link>`, `text=<rest>`, `title=<subject>`.
    The subject is the title (`Re:`, `Fwd:`, `[x3]` removed). Email text items keep
    only the URL the parser chose as their source.
- **HA share** (`workers/ha_share.py`): when `SUPERVISOR_TOKEN` (or the legacy
  `HASSIO_TOKEN`) is present and `ha_share_enabled`, connect
  `ws://supervisor/core/websocket`, auth with the token, `subscribe_events` for
  `mobile_app.share`; event data `url`/`text`/`entered`/`subject` → `IngestRequest`
  (`source="ha_share"`, `sender` = the phone's name). iOS's edited share box with a
  link is sent as "comment + link" text, so §7.1 rule 6 decides. Processing runs in a
  separate task from an in-memory queue (at most 200). Reconnect backoff 2 s up to
  5 s; a rejected token 30 s up to 120 s. On `StorageError` the same share is retried
  every 60 s and the status shows the error. Events fired while disconnected are lost
  (HA does not replay them).
- **LAN push** (`workers/lan_push.py`): when `device_host` is set, poll
  `http://<device_host>/api/status` every `push_poll_seconds` (default 20 s, 2.5 s
  timeout). While it answers, make sure `push_folder` exists, then upload each item
  never delivered on any channel as `<title>.<delivery_format>` (one request at a
  time) and mark `delivered.push` only after the device's file listing shows the file
  with the right size. The firmware never overwrites, so a taken name becomes
  `<title> (2).<ext>`, ...; an identical copy already there is marked without uploading
  again. An item the device refuses (full card) is retried the next time the device
  appears and never blocks the others.

## 10. CrossPoint 2.x plugin bundle (`plugin_bundle.py`)

Folder `/.crosspoint/plugins/sendtox3/` (hidden root so the Library does not list the
plugin's own files) containing a generated `device.json` with the server URL and
device token **hard-coded** (no `config.json`, so a missing config can never produce
literal `{cfg.server}` URLs), plus `manifest.json` (title, description, version) and
`README.md`. Also the empty `<inbox_folder>/` (event downloads do not create folders).
In the zip, folders are 0755 with the directory bit and files 0644.

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
`delivery_format` (`epub`|`txt`, default `epub`) picks the catalog download format;
`inbox_folder` (default `/Inbox`) is `dest_dir` and the event `dest` folder.
Auto-delivery is always EPUB (the filename cannot carry a title; EPUB metadata does).

The device token must be printable ASCII without spaces, `{`, `}`, `"` or `\`
(`device_token_problem`); otherwise no bundle is built, startup logs a warning and the
Setup page shows it. The server address must be `http(s)://host[:port]` without user
part, empty port, query or fragment (`normalize_base_url`); a `server_host` typed
without a scheme must be `host[:port]` or `[IPv6][:port]` with a port 1–65535.

**Firmware caveat: sleep delivery on 2.0.0rc.** CrossPoint 2.0.0rc looks up the saved
Wi-Fi password at sleep without loading `wifi.json` (`src/main.cpp:405`; only
`WifiSelectionActivity` loads it), and every wake is a reboot. So a plain wake → read
→ sleep cycle only queues the `sleep.enter` event; it is delivered at the next Wi-Fi
join (opening Home > Plugins > Send to X3, OPDS, File Transfer > Join a Network,
KOReader sync; every join drains the plugin outbox) or at a sleep taken while Wi-Fi is
still on. Fixed upstream after the tag by develop commit `facaa87` (2026-10-23, "Load
WiFi settings from file before sleep mode"). The text is
`plugin_bundle.SLEEP_DELIVERY_CAVEAT`, used in the plugin README; the Setup page and
the docs say the same. The catalog works on every 2.x build.

**Install over the LAN** (`install_to_device`): with the X3 in File Transfer, read
`/api/status` (refused for firmware below 2 without `pluginApi`), create the inbox
folder, write the three files (atomic `/api/plugin-fs` when Plugins is on, else
delete + upload), and report whether Plugins is switched off. It returns log lines; a
line starting with `Error:` means the install did not complete.

**Server address** (`resolve_base_url`): `server_host` (host, `host:port` or a full
URL) wins; else the Supervisor's `/network/info` (primary interface's IPv4) with the
mapped host port from `/addons/self/info`; else the first non-loopback IPv4 of the
machine. While the address is unknown, or under Home Assistant only a local guess,
`__main__` retries detection with backoff (5 s up to 300 s).

## 11. HA app options

Defined once in `sendtox3/config.py` (`Options`); `config.yaml` `options`/`schema`,
`translations/en.yaml` and `DOCS.md` must mirror it exactly (`tests/test_packaging.py`
enforces this). The one exception is `port`: it stays in `Options` (and
`SENDTOX3_PORT`) for development and plain Docker, but not in the Home Assistant form,
because ingress, the watchdog and the published host port are fixed at container port
8765 (a different host port is set in the app's **Network** section). Outside Home
Assistant, every option can be overridden with `SENDTOX3_<OPTION>`.

Secrets left empty are generated on first start (`secrets.token_urlsafe(24)`, 32
characters) and persisted (`Store.secret`, `kv` key `secret:<name>`). A value set in
the options always wins, so emptying an option brings back the generated token. A
generated device token can be replaced from the Setup page (**New device token**,
`app_state.rotate_secret`); a token set in the options answers 409 there.

Backups are cold (`backup: cold`): the Supervisor stops the app while it copies
`/data`, because a hot copy of the WAL-mode database could pair files from different
moments.

## 12. Security

- **LAN port** exposes only token-protected endpoints (`/healthz` is open). Tokens are
  32 random URL-safe chars, compared in constant time. The admin sign-in form refuses a
  missing `Content-Length` or one over 4096 bytes (413), reads the body with a 10 s
  deadline, and answers a wrong token after 0.5 s. Sessions are HMAC-signed HttpOnly,
  SameSite=Strict cookies (30 days; changing the admin token ends them). Every admin
  form carries a CSRF token; admin pages send a strict CSP and `Referrer-Policy:
  no-referrer`; no token or password is ever logged (the access log has no query
  strings).
- **Device token exposure**: it is written into `device.json` on the SD card, and the
  X3's File Transfer web server serves it to anyone on the Wi-Fi while open. It opens
  the catalog and the OPDS feed. Replace it with **New device token** (§11).
- **Fetcher SSRF guard** (`extract/fetch.py`), unless `allow_private_urls` is on:
  http/https only; every destination checked after DNS resolution and the connection
  made to the checked address (a resolver wrapper plus a check inside aiohttp's
  connector, which answers IP literals itself), on every hop of at most 5 redirects;
  hosts checked in the form aiohttp dials (full-width digits and ideographic dots
  become a plain IPv4 literal); private, loopback, link-local, multicast, CGNAT,
  reserved and unspecified addresses refused, including IPv4 embedded in IPv6.
  **Guarded fetches use IPv4 only**: a device on the home network can have a global
  IPv6 address that no range check can tell from a public server. IPv6 literals are
  refused, and a host with only public IPv6 addresses gets "This server only has an
  IPv6 address; Send to X3 fetches over IPv4 only (unless allow_private_urls is on,
  ...)". Environment proxies are ignored. Bodies are capped at 8 MB for pages and
  25 MB for PDFs (decompressed), and time at `fetch_timeout_seconds`.
- **PDF worker isolation** (`extract/pdf.py`): each conversion runs in its own process
  (own session, killed with its process group on timeout or cancel), with an
  address-space limit of 1 GB, 120 s (600 s with OCR), at most 400 pages (OCR at most
  20), and at most 2 conversions at once. Its environment is an allowlist (PATH,
  locale, TZ, temp dirs, PYTHONHOME, TESSDATA_PREFIX, LD_LIBRARY_PATH, SYSTEMROOT, plus
  PYTHONPATH): no `SUPERVISOR_TOKEN`, `HASSIO_TOKEN` or `SENDTOX3_*`. When the server
  runs as root (the add-on container), the worker runs as `sendtox3-pdf`, else `nobody`,
  without supplementary groups (`SENDTOX3_PDF_WORKER_USER` names another account;
  `root` keeps root). Every way of staying root logs a warning once per reason.
- **Data folder permissions**: folders 0700, files 0600 (§4), so the unprivileged PDF
  worker cannot read the database with the tokens.
- **Other caps**: share requests 30 MB; emails 25 MB and 50 requests; uploaded EPUBs
  64 MB declared size, 24 MB decompressed in all and 4 MB per chapter for the text
  copy, 4M characters, a deadline, and chapters over 1 MB through a cheap linear walker.
- **Email acceptance filter** as in §9 (fail closed).
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

How to run each level, and the latest results, are in [TESTING.md](TESTING.md).

## 14. Module interfaces (stable contracts between components)

Package root `send_to_x3/sendtox3`. Python 3.14. Tests run from the repo root with
`pytest` (`pytest.ini` sets `pythonpath = send_to_x3`). Network is never used in unit
tests; integration tests use local fixture servers. Each module owner writes tests in
`tests/test_<module>.py` (and `tests/integration/` for cross-module flows).

Shared, do not change signatures without updating every caller: `textmodel.py`
(Document, blocks, `clean_text`, `font_covers`), `models.py` (IngestRequest, Item,
`new_item_id`), `config.py` (Options, `load_options`), `store.py` (Store).

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
                    allow_private: bool = False, headers: Mapping[str, str] | None = None,
                    max_pdf_bytes: int | None = None) -> FetchResult
def clean_url(url: str) -> str              # strip tracking params, unwrap redirectors
def decode_html(body: bytes, charset_hint: str | None) -> str

# extract/html.py
def html_to_document(html: str, *, url: str = "", lang_hint: str = "", whole: bool = False) -> Document
    # whole=True (email bodies): convert everything, no article extraction, no notes
def html_fragment_to_blocks(fragment: str, *, xml: bool = False, drop_chrome: bool = False,
                            normalize: bool = True) -> list[Block]
async def url_to_document(url: str, *, opts: Options, title_hint: str = "") -> Document
    # fetch + route by content type: HTML -> trafilatura; site APIs (GitHub README,
    # Wikipedia, Substack, Bluesky, X); PDF -> extract.pdf.pdf_to_document_async;
    # text/plain -> text. Follows one redirect stub (meta refresh, apple.news, a
    # canonical link to another host) when the page has under 80 words.
    # Never raises for remote problems: returns a Document with a note and whatever
    # text was available (paywall/blocked/JS-only), plus the URL.

# extract/text.py
def text_to_document(text: str, *, title: str = "", source_url: str = "") -> Document
def classify(text: str, url: str = "", title: str = "") -> Literal["url", "text"]
def find_urls(text: str) -> list[str]
def first_url(text: str) -> str
def derive_title(text: str) -> str          # first sentence/line, <= 80 chars

# extract/email.py
@dataclass
class ParsedEmail:
    subject: str; from_addr: str; to_addrs: list[str]; message_id: str
    auth_results: str                       # topmost Authentication-Results value
    dmarc_pass: bool; dkim_aligned_pass: bool
    requests: list[IngestRequest]           # body text/url, PDF/txt/html attachments
    # extras: delivered_to, size, from_name, auth_summary, notes, ...
def parse_email(raw: bytes, *, headers_only: bool = False) -> ParsedEmail   # never raises by design
def accept_email(p: ParsedEmail, opts: Options) -> tuple[bool, str]       # (ok, "accepted: ..."/"rejected: ...")
def sender_filter_problem(opts: Options) -> str   # NO_SENDER_FILTER, WILDCARD_IN_INBOX or ""
def watches_personal_inbox(opts: Options) -> bool # INBOX (any case) without imap_plus_tag

# extract/pdf.py  (never raises; failures are a Document without blocks and a note)
def pdf_to_document(data: bytes, *, title_hint: str = "", source_url: str = "", ocr: bool | None = None,
                    timeout: float | None = None, ...) -> Document          # blocking
async def pdf_to_document_async(data: bytes, *, title_hint: str = "", source_url: str = "",
                                ocr: bool | None = None, timeout: float | None = None, ...) -> Document
    # cancellable: an outer wait_for kills the worker and frees its slot; use this from asyncio
def convert_pdf(...) -> PdfResult; async def convert_pdf_async(...) -> PdfResult   # .doc, .ok, .kind, .error

# render/names.py
def safe_title(title: str, *, max_bytes: int = 90, site: str = "", now: float | None = None) -> str
def unique_title(title: str, taken: Iterable[str], *, max_bytes: int = 90, site: str = "",
                 now: float | None = None) -> str
# render/txt.py
def render_txt(doc: Document) -> bytes
# render/epub.py
def render_epub(docs: list[Document], *, book_title: str = "", identifier: str = "", ...) -> bytes

# ingest.py
class StorageError(Exception): ...          # the store failed; nothing was saved
@dataclass
class ProcessResult:
    item: Item
    duplicate: bool = False                 # an identical share from the last 24 h was returned
class Processor:
    def __init__(self, store: Store, opts: Options): ...
    async def process_all(self, req: IngestRequest) -> list[ProcessResult]  # never empty; several bare links -> several
    async def process_detailed(self, req: IngestRequest) -> ProcessResult   # the first of process_all
    async def process(self, req: IngestRequest) -> Item                     # the first item
    async def process_many(self, reqs: list[IngestRequest]) -> list[Item]   # one after another
    async def recover_pending(self) -> int  # redo spooled requests at startup; returns how many
    def budget_for(self, req: IngestRequest) -> float  # 150 s, +510 s with pdf_ocr
    # Every process* method raises only StorageError; any other failure becomes a
    # stored item with status "failed" and a deliverable fallback document.
def display_title(raw: str, safe: str, *, site: str = "", url: str = "") -> str
def strip_display_affixes(title: str, site: str = "", url: str = "") -> str
def matches_title(text: str, title: str, *, url: str = "", site: str = "") -> bool

# delivery.py
def build_auto_delivery(store: Store, ev: str, ts: str) -> bytes | None   # None -> 404
def catalog_page(store: Store, page: int, limit: int) -> dict             # {"items":[...]}
def save_document(store: Store, item_id: str, doc: Document, *, item_title: str = "") -> None
def load_document(store: Store, item_id: str) -> Document | None

# plugin_bundle.py
def build_device_json(*, base_url: str, device_token: str, opts: Options) -> dict
def build_bundle_zip(*, base_url: str, device_token: str, opts: Options) -> bytes
    # zip with .crosspoint/plugins/sendtox3/{device.json,manifest.json,README.md} and <inbox>/.keep
async def install_to_device(device_host: str, *, base_url: str, device_token: str,
                            opts: Options, client=None, timeout: float = 15.0) -> list[str]
async def resolve_base_url(opts: Options, ...) -> tuple[str, str]   # (url, "options"|"supervisor"|"local address"|"invalid server_host"|"")
def normalize_base_url(base_url: str) -> str                         # raises ValueError
def device_token_problem(token: str) -> str                          # "" when usable
def under_supervisor() -> bool
def looks_container_internal(base_url: str) -> bool                  # 172.16.0.0/12

# phone.py
def android_import_json(base_url: str, ingest_token: str, *, email: str = "") -> bytes
IOS_SHORTCUT_NOTE: str

# store.py (selection)
class Store:
    def spool_add(self, req: IngestRequest) -> str
    def spooled_requests(self) -> list[SpooledRequest]   # .id, .request, .created_at, .attempts, .data_lost
    def spool_attempt(self, spool_id: str) -> int
    def spool_remove(self, spool_id: str) -> None
    def titles_in_use(self) -> set[str]; def issued_titles(self) -> set[str]
    def secret(self, name: str, configured: str = "") -> str

# web/app.py
def create_app(opts: Options, store: Store, processor: Processor, *, state: AppState | None = None
               ) -> aiohttp.web.Application

# workers/*.py — each exposes:  async def run(app_state) -> None  (long-running, cancellable;
#   returns with status enabled=False when it has nothing to do, so it is not restarted)
```
