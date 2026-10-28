# Send to X3

Share a web page, some selected text, an email or a PDF from your phone or computer,
and read it a moment later on your **Xteink X3** as clean, image-free text.

Send to X3 is a Home Assistant app (Home Assistant's new name for add-ons) that runs
on your Raspberry Pi. It works with the X3's **stock CrossPoint firmware**: nothing on
the reader needs to be modified, and nothing needs to be reachable from the internet.
If you want to read right from the plugin page, the optional **Send to X3 firmware**
(CrossPoint plus one change) opens the book you pick in **Home > Plugins > Send to X3**
right away (books delivered at sleep or by LAN push wait in the Library as usual).

```
 Phone / computer                     Raspberry Pi (Home Assistant OS)        Xteink X3 (stock CrossPoint)
 ----------------                     --------------------------------        ----------------------------
 iPhone shortcut ------ home Wi-Fi --->+---------------------------+
 Android HTTP Shortcuts -------------->|       Send to X3 app      |<-- 2.x plugin: "Get 3 new"
 Home Assistant Companion share ------>|                           |    and a catalog, on request
 Web UI: paste text or upload a file ->|  fetch the article or PDF |<-- 1.6.5+: OPDS Browser
 Email, from anywhere --> Gmail <------|  -> clean text EPUB / TXT |--> any: LAN push while the
                         (the app checks the mailbox)              |    File Transfer screen is open
                                       +---------------------------+
```

## What it does

- **Articles without the clutter:** a shared link becomes the article's text with its
  title, author, site and date. No ads, menus, comments or images. Sites with an API
  (Wikipedia, GitHub READMEs, Substack, Bluesky, X) are read through it.
- **Notes and selections:** selected or typed text becomes a note; a Safari selection
  keeps the page as its source; a link with a short comment (up to 30 words) becomes
  the article with your comment at the top; a few links with little else (up to 10)
  become one item each.
- **PDFs:** columns, headings, paragraphs and footnotes are rebuilt into reflowable
  text. Optional OCR for scanned PDFs.
- **Email from anywhere:** forward newsletters, mail yourself links or PDFs. The app
  checks a dedicated mailbox, reads nothing until you have set a secret plus address
  and/or your allowed senders, and leaves every mail it does not accept unread.
- **Reads well on the X3:** small, valid EPUB files with proper titles (plain `.txt`
  if you prefer), with file names the firmware accepts.
- **You decide when it arrives:** on the X3 open **Home > Plugins > Send to X3** and
  press **Confirm** on the first row, **Get 3 new**: everything shared since last time
  arrives as one book. Nothing is downloaded by itself.
- **Read right away (optional):** on the [Send to X3 firmware](docs/FIRMWARE.md) that
  button says **Open**: the book downloads and opens in the reader, and **Open** again
  on a single item's row opens the copy it saved where you left off. It installs from
  the SD card with the X3's own firmware updater, and you can go back to the official
  firmware the same way. Not yet tried on a real X3 (only in the simulator).
- **Ways onto the X3:** the CrossPoint 2.x plugin (**Get 3 new**, a catalog of
  everything shared, and optional delivery at sleep), the OPDS feed for CrossPoint
  1.6.5 and later (with the same **Get 3 new** entry), and LAN push to the X3's File
  Transfer screen.
- **A web UI in Home Assistant:** the reading queue, previews, delivery status,
  requeue and delete, a paste and upload form, setup steps with QR codes, and a
  one-click plugin install onto the X3.
- **Stays at home:** one token-protected port on your home network, no cloud service
  and no port forwarding. Shared links are only fetched from the public internet, and
  PDFs are converted in an isolated process. See [Security](docs/SECURITY.md).

## Which setup is yours?

Check the X3's firmware version: open **Settings** on the X3; the version is at the top
right.

| Your X3 runs | With an iPhone | With an Android phone |
|---|---|---|
| **CrossPoint 2.x** (2.0.0rc or later) | Plugin on the X3 ([Setup 5a](docs/SETUP.md#part-5a-x3-with-crosspoint-2x-the-plugin)); the Send to X3 shortcut at home and email away ([iPhone guide](docs/phone-ios.md)) | Plugin on the X3 ([Setup 5a](docs/SETUP.md#part-5a-x3-with-crosspoint-2x-the-plugin)); HTTP Shortcuts at home and email away ([Android guide](docs/phone-android.md)) |
| **CrossPoint 1.6.5** | OPDS and/or LAN push ([Setup 5b](docs/SETUP.md#part-5b-x3-with-crosspoint-165-opds-and-lan-push)); the shortcut and email as above | OPDS and/or LAN push ([Setup 5b](docs/SETUP.md#part-5b-x3-with-crosspoint-165-opds-and-lan-push)); HTTP Shortcuts and email as above |

Good to know before you choose:

- **2.x** gives you the Send to X3 plugin: **Get 3 new** fetches everything new in one
  press, and the list below it has every item. Delivery at sleep is optional (on
  **2.0.0rc**, a prerelease from 2026-10-19, it only happens once Wi-Fi has been used
  since the X3 last woke up; develop commit `facaa87`, 2026-10-23, fixes that).
- **1.6.5** (stable) has no plugins: you pick items in the X3's OPDS Browser, and/or
  new items are copied whenever the X3's File Transfer screen is open. Staying on 1.6.5
  is fine.
- The **Send to X3 firmware** is 2.x (CrossPoint develop of 2026-10-28) plus one
  change: pressing **Open** in the plugin opens the book. You build it on a Mac with
  Docker; see [the firmware guide](docs/FIRMWARE.md).
- On either phone, the **Home Assistant Companion app**'s share sheet works with no
  setup at all, and **email** works from anywhere once
  [Part 6](docs/SETUP.md#part-6-email-optional-works-from-anywhere) is done.
- The ready-made **iPhone shortcut** has to be signed on a Mac; without a Mac you build
  it by hand in about ten minutes. The **Android** import file comes from the app's
  Setup page, already filled in.

## Documentation

| Read | For |
|---|---|
| [Setup guide](docs/SETUP.md) | Everything, step by step: network, firmware, installing the app, the X3, email, phones, final check |
| [Using Send to X3](docs/USING.md) | Daily use: what to share, how it looks, Get 3 new vs catalog vs OPDS vs push vs delivery at sleep, PDFs, deleting and requeueing |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Symptom, cause and fix tables, and how to collect logs |
| [Security](docs/SECURITY.md) | What is exposed on your network, the email rules, recommendations |
| [iPhone and iPad](docs/phone-ios.md) | The Send to X3 shortcut, the Companion app, email |
| [Android](docs/phone-android.md) | HTTP Shortcuts, the Companion app, email |
| [PDFs](docs/pdf.md) | What converts well, limits, OCR |
| [The Send to X3 firmware](docs/FIRMWARE.md) | Optional: read right from the plugin page. What it changes, installing it from the SD card, going back, building it |
| [App options](send_to_x3/DOCS.md) | Every option (also the app's Documentation tab in Home Assistant) |

For developers: [developing](docs/DEVELOPMENT.md), [testing](docs/TESTING.md),
[architecture and contracts](docs/ARCHITECTURE.md),
[phone shortcut generators](shortcuts/README.md),
[firmware end-to-end tests](e2e/README.md),
[building the Send to X3 firmware](firmware/README.md),
[Home Assistant packaging checks](e2e/ha/README.md) and
[CrossPoint parser harnesses](e2e/fwharness/README.md). `scripts/check_all.sh` runs the
unit and integration tests; `scripts/check_all.sh --all` runs every verification level
(Docker and the firmware toolchains are needed for the later levels; see
[testing](docs/TESTING.md)).

## Licence

Send to X3 is released under the **MIT License**: see the [LICENSE](LICENSE) file.

It uses these open-source components, each under its own licence:

| Component | Licence |
|---|---|
| trafilatura | Apache-2.0 |
| pypdfium2 | Apache-2.0 / BSD-3-Clause |
| aiohttp | Apache-2.0 |
| lxml | BSD |
| Jinja2 | BSD |
| segno | BSD |
| httpx | BSD |
| charset-normalizer | MIT |
| Tesseract OCR (installed in the app image) | Apache-2.0 |

Some test stubs under `e2e/fwharness/stubs/` derive from CrossPoint's test stubs (MIT
License, Copyright (c) 2025 CrossPoint Reader organization).

Send to X3 is an independent project. It is not affiliated with Xteink, the CrossPoint
Reader project or Home Assistant.
