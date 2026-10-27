# PDFs in Send to X3

Send to X3 turns a PDF (an email attachment, an uploaded file, or a link to a PDF)
into plain, reflowable text for the X3: headings, paragraphs, lists and footnotes,
without images. The PDF itself is not sent to the reader.

## What works well

- **Articles, reports, papers and books** with real text in them: most PDFs made by
  Word, LaTeX, InDesign or a "Save as PDF" button.
- **Two- and three-column layouts** are read column by column. Text that runs across
  both columns (titles, wide figures) is kept in place.
- **Running headers and footers**, page numbers, "Draft"/"Page proof" watermarks and
  table-of-contents dot leaders are removed.
- **Paragraphs** are rebuilt. A paragraph that continues on the next page or in the
  next column is joined back together, even when a figure caption sits in between
  (the caption follows the paragraph).
- **Hyphenation at line ends** is undone using the document's own words: "demon-
  stration" becomes "demonstration", but "fine-tuning", "state-of-the-art" and
  "COVID-19" keep their hyphens.
- **Footnotes** are collected into a **Notes** section at the end. In the text a
  reference reads `[1]`, and the note reads `[1] ...`. Notes that restart their
  numbering (per chapter or per opinion) are renumbered 1, 2, 3 ... in order.
- **Headings** are found from type size, bold type and numbering ("2.1 Methods" is a
  sub-heading of "2 Results"). Slide decks get one heading per slide.
- **Superscripts** that are not footnote references stay superscripts ("mc²").
- **Broken "fi"/"fl" letters**: some PDFs (US Supreme Court prints, for example) store
  "filed" in a way that every PDF program reads as "fled". These are repaired.
- **Title and author** come from the PDF's properties when they are meaningful.
  Placeholders such as "Microsoft Word - draft7.docx" or a user name like
  "SE:W:CAR:MP" are ignored; then the big title on the first page is used, then the
  email subject or file name.
- **Language** comes from the PDF, or is guessed from the text, so the X3 hyphenates
  correctly.

## Limits

- **Images, charts and diagrams are left out.** Their captions ("Figure 1: ...")
  are kept as plain paragraphs. Labels inside charts are dropped where they can be
  recognised as such; a few may slip through.
- **Tables are simplified**: each row becomes one line of text, and the item notes
  "Tables were simplified to plain lines of text." Complex tables can come out
  jumbled.
- **Maths** comes out as approximate plain text (fractions, sub- and superscripts and
  symbols lose their layout).
- **Forms** (tax forms, applications) are readable but cluttered.
- **Cover pages** sometimes produce a poor title (for example "OFFICIAL REPORTS").
  When you email a PDF, a descriptive subject line becomes the title in that case.
- **Bold headings in fonts without a style name** may be missed; such documents get
  fewer headings and read as one long chapter.
- **Right-to-left scripts, vertical CJK text and rotated text** (sideways tables,
  margin notes) are not supported; rotated text is skipped on purpose.
- **Password-protected PDFs** cannot be opened. PDFs that only restrict printing or
  copying open normally. The item says "This PDF is password-protected (encrypted), so
  it could not be opened."
- **Size and time limits**: a linked PDF may be at most 25 MB (an email at most
  25 MB, an upload in the web UI at most 30 MB). At most 400 pages are converted (the
  item says when it was cut short), a conversion may take about 2 minutes (about 10
  minutes with OCR on) and use at most 1 GB of memory, and at most two PDFs are
  converted at the same time (the others wait their turn). A PDF that exceeds a
  limit, or a damaged PDF, becomes a **failed** item with an explanatory note (for
  example "Converting this PDF took longer than 120 seconds, so it was stopped.")
  instead of breaking the app.

## Scanned PDFs and OCR

A scanned PDF contains pictures of pages, not text. By default Send to X3 detects
this and delivers a short item saying "Scanned PDF: no text layer, so its text could
not be extracted." Scans that
already carry a hidden text layer (most library and archive scans) are converted
from that layer, so their quality depends on the original recognition.

Turn on the **`pdf_ocr`** option to recognise the text of scanned pages with
Tesseract (English):

- Only pages without usable text are recognised, at most 20 pages per PDF; the item
  notes when pages were skipped.
- It is slow: about 3 seconds per page on a fast computer, roughly 10-20 seconds per
  page on a Raspberry Pi 4 (estimated). Long scans can hit the 10-minute limit; the
  pages recognised until then are kept and the item says OCR stopped early.
- Recognised text contains errors, especially on old or low-quality scans. The item
  notes "Text on N scanned pages was recognized by OCR and may contain errors."

## Options

| Option | Default | Effect |
|---|---|---|
| `pdf_enabled` | `true` | Convert PDFs at all. When off, a link to a PDF or a PDF attachment becomes a **failed** item that says PDF support is turned off, and a PDF upload in the web UI or the share API is refused. |
| `pdf_ocr` | `false` | Recognise the text of scanned pages with OCR (see above). |

## How the conversion is protected

Each PDF is converted in its own short-lived process, separate from the app. That
process gets none of the app's tokens or options, and in the Home Assistant app it
runs as an unprivileged account that cannot read the app's data folder. It is stopped
when it hits a limit above, or when the share's overall time budget (2.5 minutes, 11
minutes with OCR on) runs out. A malicious or broken PDF can therefore only end its
own conversion. Details: [Security](SECURITY.md) and
[architecture §12](ARCHITECTURE.md#12-security).
