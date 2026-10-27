"""Optional OCR of scanned PDF pages (tesseract CLI). Skipped when tesseract is not
installed; the add-on image installs it with ``apk add tesseract-ocr
tesseract-ocr-data-eng``."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

pytest.importorskip("pypdfium2")
if not shutil.which("tesseract"):
    pytest.skip("tesseract is not installed", allow_module_level=True)

import pypdfium2 as pdfium  # noqa: E402
import pypdfium2.raw as R  # noqa: E402

from sendtox3.extract.pdf import convert_pdf  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "pdfs"
_spec = importlib.util.spec_from_file_location("pdfgen", FIX / "pdfgen.py")
pdfgen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pdfgen)

LINES = [
    "Scanned Letter About The Garden",
    "The old garden behind the house was quiet this morning.",
    "A small bird sang near the window while the rain fell.",
    "Everyone agreed that the roses had never looked better.",
]


def scanned_pdf(pages: int = 1, dpi: int = 150) -> bytes:
    """A text page rendered to a gray bitmap and wrapped as an image-only PDF."""
    p = pdfgen.Page()
    p.text(110, 120, LINES[0], size=20, font="B")
    y = 170
    for ln in LINES[1:]:
        p.text(72, y, ln, size=13)
        y += 20
    src = pdfium.PdfDocument(pdfgen.build([p]))
    bm = src[0].render(scale=dpi / 72, grayscale=True, force_bitmap_format=R.FPDFBitmap_Gray)
    w, h, stride = bm.width, bm.height, bm.stride
    buf = bytes(memoryview(bm.buffer).cast("B"))
    data = b"".join(buf[y * stride: y * stride + w] for y in range(h))
    out = []
    for _ in range(pages):
        q = pdfgen.Page()
        q.image(0, 0, 612, 792, w, h, data)
        out.append(q)
    return pdfgen.build(out)


def test_ocr_recognizes_a_scanned_page():
    r = convert_pdf(scanned_pdf(), ocr=True, timeout=120)
    assert r.ok, r.doc.notes
    t = " ".join(getattr(b, "text", "") for b in r.doc.blocks)
    assert "garden behind the house" in t
    assert "roses had never looked better" in t
    assert any("recognized by OCR" in n for n in r.doc.notes)
    assert r.doc.title == LINES[0]


def test_ocr_page_cap():
    r = convert_pdf(scanned_pdf(pages=3), ocr=True, ocr_max_pages=1, timeout=120)
    assert r.ok
    assert any("OCR was limited to 1 pages" in n for n in r.doc.notes)
    assert any("Page 2 is a scanned image" in getattr(b, "text", "") for b in r.doc.blocks)


def test_ocr_on_a_real_scan():
    r = convert_pdf((FIX / "archive_scan_page.pdf").read_bytes(), ocr=True, timeout=120)
    assert r.ok
    assert r.doc.word_count() > 100
