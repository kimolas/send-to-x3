# Changelog

All notable changes to this app are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the app uses
[semantic versioning](https://semver.org/).

## [0.3.0] - 2026-10-28

### Added

- Read right from the plugin page with the **Send to X3 firmware**: the latest
  CrossPoint (develop `8f4d035`) plus one change, built from `firmware/` and
  installed with the X3's own **SD Card Firmware Update** (see `docs/FIRMWARE.md`).
  On it, Confirm in Home > Plugins > Send to X3 reads **Open**: the book downloads,
  the X3 reports it to the app straight away, and the book opens in the reader.
  **Open** again on a single item's row opens the copy that row saved where you left
  off, without downloading it again. On other CrossPoint builds Confirm downloads as
  before. Not yet tried on a real X3, only in the simulator.
- The Wi-Fi plugin install says which of the two this X3 does (the firmware
  advertises `catalog.open_after_download` in `/api/status`).
- The status shows a warning while the X3 still uses a plugin from 0.2.0 or older
  on a new CrossPoint build: install the plugin again.

### Changed

- File names the app has used on the X3 stay reserved for good (they were freed
  after a year), and LAN push names are reserved too, so a new item never gets a
  name a file on the card already has.

### Fixed

- Plugin catalog paging on new CrossPoint builds (develop `c3b16da` and later,
  including the Send to X3 firmware): every page after the first started with the
  last row of the page before, and a full last page could be followed by a page
  holding only that repeated row. The plugin now asks for rows by position
  (`offset`), and the app also pages correctly for a plugin installed by 0.2.0 on
  those builds. 2.0.0rc and older develop builds page as before.

### Upgrading from 0.2.0

- Install the plugin on the X3 again (Setup page, or the SD-card bundle), so it
  gets the new `device.json`: without it, Confirm keeps downloading even on the
  Send to X3 firmware, and new CrossPoint builds can show an empty last page.
  Before reinstalling from the SD-card bundle, go back to Home and open Home >
  Plugins > Send to X3 once, so the X3 reports your last fetch; the bundle empties
  the X3's queue of reports. The Wi-Fi install from the Setup page does not need
  this.

## [0.2.0] - 2026-10-26

### Changed

- New items no longer arrive on the X3 by themselves. The first row of Home >
  Plugins > Send to X3 is now **Get 3 new · Oct 26 14.05**: one press downloads
  everything shared since the last fetch as one book in `/Inbox` (or **Nothing new
  right now**). The OPDS feed starts with the same entry when something is new.
- The X3 confirms what it saved: the plugin reports each download (its
  `book.downloaded` event) the next time the X3 joins Wi-Fi, and only then do the
  items count as delivered. A cancelled or failed download is offered again; a fetch
  awaiting its report is not (the queue shows "sent, not confirmed yet"), and comes
  back after two hours under a new name if no report arrives. Two "Get N new"
  downloads never share a file name, so one never replaces another on the X3.
- A download counts as received only once the X3's network stack acknowledged every
  byte, not when the app handed it to the network (a cancelled small download used
  to count).
- Installing the plugin clears its old event queue on the X3, so leftover 0.1.0
  sleep-delivery requests no longer delay the download reports.
- If the bundle cannot be built (for example a full disk), the catalog and the OPDS
  feed still list every item instead of failing.
- `auto_delivery` (delivery when the X3 sleeps) is now off by default.

### Upgrading from 0.1.0

- Install the plugin on the X3 again (Setup page): the 0.1.0 plugin has delivery at
  sleep built in, so the X3 keeps joining Wi-Fi at every sleep until you do, and it
  cannot confirm downloads.
- If you saved the app's Configuration under 0.1.0, it may still have **Also deliver
  when the X3 sleeps** on: turn it off and restart the app before reinstalling.

## [0.1.0] - 2026-10-25

### Added

- First release.
- Share API (`POST /api/v1/ingest`) for shortcuts and scripts, Home Assistant
  Companion app share sheet, and email ingest over IMAP. Email fails closed: mail
  is read only when `allowed_senders` and/or `imap_plus_tag` is set, `'*'` is
  accepted only with a tag or a folder other than INBOX, and refused mail is left
  unread.
- Article extraction to clean, image-free EPUB and TXT; PDFs (pypdfium2, optional
  Tesseract OCR) and emailed newsletters.
- CrossPoint 2.x SD-card plugin: on-device catalog and automatic delivery when the
  X3 goes to sleep on builds newer than 2.0.0rc (develop commit facaa87); on
  2.0.0rc new items arrive the next time the X3 uses Wi-Fi.
- OPDS feed for CrossPoint 1.6.5 and later.
- LAN push to the X3's File Transfer server.
- Web UI in Home Assistant (ingress) with setup steps and the reading queue.
- Cold backups: the app stops briefly while Home Assistant backs it up, so the
  database is always copied in a consistent state.
