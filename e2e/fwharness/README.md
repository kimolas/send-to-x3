# fwharness: CrossPoint's own parsers, built on the host

These harnesses compile the **real, unmodified** CrossPoint reader code that decides
whether a delivered file works on the X3, and run it against our output on a Mac or
Linux box. Nothing is changed in the CrossPoint checkout; only small host stand-ins for
hardware (SD card, display, RTOS, image decoders) live here.

```sh
export CP_REPO=/path/to/crosspoint-reader   # the checkout to build against (read only)
export CP_SDK=/path/to/freeink-sdk          # 2.x only; default $CP_REPO/freeink-sdk
export FWH_WORK=/tmp/sendtox3-fwharness     # outside this repo; default $TMPDIR/sendtox3-fwharness
e2e/fwharness/build.sh                      # prints FWH_BIN=<dir> last; rebuilds only when inputs change
```

Tested with tag `1.6.5` and develop `3f387a9` (2.0.0rc). 2.x needs the `freeink-sdk`
submodule (`git -C "$CP_REPO" submodule update --init freeink-sdk`, or point `CP_SDK` at
an export of the pinned commit). Requirements: clang/clang++ (C++20) and libexpat. macOS:
when the Command Line Tools ship a broken `usr/include/c++/v1`, the script switches to the
SDK's libc++ headers automatically (the `CPLUS_INCLUDE_PATH` quirk).

| Binary | What runs | Output |
|---|---|---|
| `epub_dump` | `ZipFile` + inflate, `ContainerParser`, `ContentOpfParser`, `TocNavParser`/`TocNcxParser`, `BookMetadataCache`, `CssParser`, hyphenation, `ChapterHtmlSlimParser` + `ParsedText` layout, measured with the real built-in fonts (1.x Noto Serif 14, 2.x Noto Sans 14) | `LIBRARY`, `META`, `SPINE`, `TOC`, per-chapter pages and `CHAPTER n result ok=`; exit 0 only if metadata loads and every chapter parses |
| `txt_dump` | 1.x: verbatim `TxtReaderActivity.cpp` + `lib/Txt`; 2.x: `Epub` opening the `.txt` (`Txt::buildTxtCache` + `TxtToHtml`) + the chapter parser, as the 2.x reader does | `TITLE (...) = [name]`, pages, `totalPages=` / `indexMs=` |
| `name_check` | `StringUtils::sanitizeFilename`, `opdsBookFilename`, and on 2.x `sanitizeFilenamePreservingExtension` + the catalog download's `..` / `isPluginPath` checks | one tab-separated row per hex-encoded input line (see `src/name_check.cpp`) |

```sh
mkdir -p /tmp/sd/books && cp book.epub notes.txt /tmp/sd/books/
CP_SDROOT=/tmp/sd "$FWH_BIN/epub_dump" /books/book.epub      # [width=518] [height=768]
CP_SDROOT=/tmp/sd "$FWH_BIN/txt_dump" /books/notes.txt
printf 'Wait... what?' | xxd -p | "$FWH_BIN/name_check"
```

Environment for the dumps: `CP_PT=12|14|16|18`, `CP_FONT=serif|sans` (1.x), `CP_LOG=1`
(firmware log lines on stderr), `CP_HYPHENATE=1` (epub_dump), and for the 1.x TXT reader
`CP_MARGIN`, `CP_ALIGN`, `CP_STATUSBAR`.

The pytest suite drives all three (`tests/test_render_validation.py`):

```sh
SENDTOX3_FIRMWARE_TESTS=1 CP_REPO=/path/1.6.5:/path/develop CP_SDK=/path/freeink-sdk \
  python -m pytest -q tests/test_render_validation.py
```

Not simulated: bezel insets (unknown; the default 518x768 viewport assumes none),
right-to-left reordering, SD-card fonts, image decoding, encrypted EPUBs (2.x reports
them as an error), device heap limits and timing (index times are Mac times; the ESP32 is
much slower).

Stubs under `stubs/common` partly derive from CrossPoint's `test/` stubs (MIT License,
Copyright (c) 2025 CrossPoint Reader organization); `stubs/realfont*/GfxRenderer.h`
mirror the built-in-font path of `lib/GfxRenderer/GfxRenderer.cpp` for measurement only.
