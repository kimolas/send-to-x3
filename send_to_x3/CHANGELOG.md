# Changelog

All notable changes to this app are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the app uses
[semantic versioning](https://semver.org/).

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
