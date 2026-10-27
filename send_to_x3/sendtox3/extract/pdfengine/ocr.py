"""Optional OCR for scanned pages with the ``tesseract`` command-line tool.

Pages are rendered with PDFium to an 8-bit grayscale PGM file (no Pillow needed) and
recognized with ``tesseract ... hocr``; the lines and word boxes become ordinary
:class:`Line` records, so reading order, paragraphs and headings are rebuilt by the same heuristics
as for born-digital PDFs.

Tesseract runs with ``OMP_THREAD_LIMIT=1``: with several OpenMP threads on a busy
single-board computer it is ~10x slower, not faster.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser

from . import textnorm
from .model import Line, Span

PGM_HEADER = "P5\n{w} {h}\n255\n"


def tesseract_path() -> str | None:
    return shutil.which("tesseract")


def ocr_junk(text: str) -> bool:
    """Tesseract reads photos/line art as noise like 'a - *.' or 'T 'atee'."""
    t = text.strip()
    alpha = sum(c.isalpha() for c in t)
    words = re.findall(r"[^\W\d_]{2,}", t)
    return alpha < 4 or alpha < 0.55 * len(t.replace(" ", "")) or not any(len(w) >= 3 for w in words)


MAX_PIXELS = 40_000_000  # ~40 MB gray bitmap: an A4 page at 300 dpi is 8.7 Mpx


def render_pgm(page, path: str, dpi: int) -> tuple[int, int, float]:
    """Render ``page`` (pypdfium2 PdfPage) to a binary PGM file; returns (w, h, dpi).
    Huge pages (posters, broken page boxes) are rendered at a lower resolution."""
    import pypdfium2.raw as R

    w_pt, h_pt = page.get_size()
    scale = dpi / 72.0
    if w_pt * h_pt * scale * scale > MAX_PIXELS:
        scale = (MAX_PIXELS / max(1.0, w_pt * h_pt)) ** 0.5
    bitmap = page.render(scale=scale, grayscale=True, force_bitmap_format=R.FPDFBitmap_Gray,
                         may_draw_forms=True)
    try:
        w, h, stride = bitmap.width, bitmap.height, bitmap.stride
        buf = memoryview(bitmap.buffer).cast("B")
        with open(path, "wb") as f:
            f.write(PGM_HEADER.format(w=w, h=h).encode("ascii"))
            if stride == w:
                f.write(buf[: w * h])
            else:
                for y in range(h):
                    f.write(buf[y * stride: y * stride + w])
        return w, h, scale * 72.0
    finally:
        bitmap.close()


LINE_CLASSES = ("ocr_line", "ocr_header", "ocr_textfloat", "ocr_caption")
_BBOX = re.compile(r"bbox (-?\d+) (-?\d+) (-?\d+) (-?\d+)")
_XSIZE = re.compile(r"x_size ([\d.]+)")
_BASE = re.compile(r"baseline (-?[\d.]+) (-?[\d.]+)")
_CONF = re.compile(r"x_wconf (-?\d+)")


class _HocrParser(HTMLParser):
    """Collect lines (bbox, x_size, baseline) and their words from tesseract hOCR."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[dict] = []
        self.block = 0
        self._stack: list[str] = []
        self._word: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        title = a.get("title", "") or ""
        self._stack.append(cls)
        if cls == "ocr_carea":
            self.block += 1
        elif cls in LINE_CLASSES:
            m = _BBOX.search(title)
            xs = _XSIZE.search(title)
            bl = _BASE.search(title)
            if m:
                self.lines.append({"bbox": tuple(int(v) for v in m.groups()),
                                   "x_size": float(xs.group(1)) if xs else 0.0,
                                   "base": float(bl.group(2)) if bl else 0.0,
                                   "block": self.block, "words": []})
        elif cls == "ocrx_word" and self.lines:
            m = _BBOX.search(title)
            c = _CONF.search(title)
            self._word = {"bbox": tuple(int(v) for v in m.groups()) if m else None,
                          "conf": int(c.group(1)) if c else 0, "text": ""}
            self.lines[-1]["words"].append(self._word)

    def handle_endtag(self, tag):
        if self._stack:
            cls = self._stack.pop()
            if cls == "ocrx_word":
                self._word = None

    def handle_data(self, data):
        if self._word is not None:
            self._word["text"] += data


def parse_hocr(hocr: str, pno: int, dpi: int) -> list:
    """tesseract hOCR -> one :class:`Line` per recognized text line. The font size
    comes from the line's ``x_size`` (stable across letter shapes, unlike ink boxes)."""
    k = 72.0 / dpi
    p = _HocrParser()
    p.feed(hocr)
    p.close()
    lines = []
    for n, ln in enumerate(p.lines):
        words = [w for w in ln["words"] if w["bbox"] and w["conf"] >= 0 and textnorm.clean(w["text"]).strip()]
        if not words:
            continue
        x0, y0, x1, y1 = ln["bbox"]
        x0 = min(x0, min(w["bbox"][0] for w in words))
        x1 = max(x1, max(w["bbox"][2] for w in words))
        height = ln["x_size"] or (y1 - y0)
        size = max(1.0, round(height * k / 0.9 * 2) / 2)
        text = " ".join(textnorm.clean(w["text"]).strip() for w in words)
        span = Span(text=text, size=size, bold=False, italic=False, sup=False, font="ocr", oy=(y1 + ln["base"]) * k)
        lines.append(Line(pno, x0 * k, y0 * k, x1 * k, y1 * k, [span], ln["block"]))
    return [ln for ln in lines if not ocr_junk(ln.text)]


def ocr_page(page, pno: int, *, lang: str = "eng", dpi: int = 300, timeout: float = 90.0,
             tesseract: str | None = None) -> list:
    """OCR one page; returns its lines. Raises ``RuntimeError`` with a short reason."""
    exe = tesseract or tesseract_path()
    if not exe:
        raise RuntimeError("tesseract is not installed")
    if not re.fullmatch(r"[A-Za-z_]{3,}(\+[A-Za-z_]{3,})*", lang or ""):
        raise RuntimeError(f"invalid OCR language {lang!r}")
    with tempfile.TemporaryDirectory(prefix="sendtox3-ocr-") as tmp:
        img = os.path.join(tmp, "page.pgm")
        try:
            _, _, real_dpi = render_pgm(page, img, dpi)
        except MemoryError:
            raise RuntimeError("the page is too large to render") from None
        except Exception as e:  # PdfiumError and friends
            raise RuntimeError(f"the page could not be rendered ({type(e).__name__})") from None
        dpi = max(1, round(real_dpi))
        env = dict(os.environ, OMP_THREAD_LIMIT="1")
        try:
            proc = subprocess.run(
                [exe, img, "stdout", "-l", lang, "--dpi", str(dpi), "--psm", "3", "hocr"],
                capture_output=True, timeout=timeout, env=env, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"tesseract timed out after {int(timeout)} s") from None
        except OSError as e:
            raise RuntimeError(f"tesseract could not run: {e.strerror or e}") from None
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError("tesseract failed: " + (err[-1][:200] if err else f"exit {proc.returncode}"))
        return parse_hocr(proc.stdout.decode("utf-8", "replace"), pno, dpi)
