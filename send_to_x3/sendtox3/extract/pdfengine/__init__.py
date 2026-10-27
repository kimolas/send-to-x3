"""PDF -> text-model conversion engine (PDFium via pypdfium2, standard library only).

Modules:
  extract    PDFium character records -> positioned lines and style spans
  layout     running headers/footers, figure text, columns/reading order, footnote zones
  structure  lines -> paragraphs, headings, list items, captions, footnotes, dehyphenation
  ligfix     repair of ligature glyphs mapped to a plain "f"
  textnorm   Unicode clean-up for PDF text
  blocks     structure blocks -> sendtox3.textmodel blocks
  ocr        optional OCR of scanned pages with the tesseract CLI
  lang       language guess
  convert    the in-process pipeline
  worker     subprocess entry point used by sendtox3.extract.pdf (limits, JSON I/O)

Ported from the PyMuPDF prototype (pdf2epub); only ``extract`` was rewritten.
"""
