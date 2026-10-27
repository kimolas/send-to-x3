"""The in-process pipeline: PDF bytes -> :class:`~sendtox3.textmodel.Document`.

Callers should use :func:`sendtox3.extract.pdf.pdf_to_document`, which runs this in a
separate process with time and memory limits. Everything here may raise on hostile
input; the worker turns exceptions into a Document with a note.
"""

from __future__ import annotations

import ctypes
import re
import time

import pypdfium2 as pdfium

from . import blocks as blockconv
from . import extract, layout, ocr, structure, textnorm
from .lang import guess_lang
from ...textmodel import Document, clean_text

FPDF_ERR_PASSWORD = 4

JUNK_TITLE = re.compile(
    r"(^untitled|^microsoft (word|powerpoint)|powerpoint presentation|\.(docx?|pptx?|pdf|tex|indd|rtf|odt)$"
    r"|^document\d*$|^slide \d+|^\s*$|^print$|^title$|^\d+$|^[\w\-]+\.\w{2,4}$)", re.I)
# "SE:W:CAR:MP", "HP_Administrator", "jsmith.ad", "user", "Owner"; real names (even
# "JOHN SMITH") stay
JUNK_AUTHOR = re.compile(r"([:_]|^user\d*$|^admin|^owner$|^\w+\.\w+$|^unknown$|^author$|^\w{1,2}$)")
JUNK_AUTHOR_I = re.compile(r"^(user\d*|admin\w*|administrator|owner|unknown|author|default|guest)$", re.I)
TITLE_PREFIX = re.compile(r"^(?:the project gutenberg e-?book,?\s*(?:#\d+\s*)?:?\s*(?:of\s+)?|microsoft (?:word|powerpoint) - )", re.I)

SCANNED_ALL = "Scanned PDF: no text layer, so its text could not be extracted."
SCANNED_HINT = "Turn on the pdf_ocr option to recognize scanned pages, or open the original PDF."


class PdfError(Exception):
    """A PDF that cannot be opened. ``reason`` is user-facing."""

    def __init__(self, reason: str, *, encrypted: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.encrypted = encrypted


def open_pdf(data: bytes, password: str | None = None) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(data, password=password)
    except pdfium.PdfiumError as e:
        if getattr(e, "err_code", None) == FPDF_ERR_PASSWORD:
            raise PdfError("This PDF is password-protected (encrypted), so it could not be opened.",
                           encrypted=True) from None
        raise PdfError("This file could not be read as a PDF (it may be damaged or not a PDF).") from None


def _final(text: str) -> str:
    return textnorm.final(textnorm.clean(text or ""))


def _weak_title(t: str) -> bool:
    t = t.strip()
    if len(t) < 4 or JUNK_TITLE.search(t):
        return True
    words = t.split()
    if not any(c.islower() for c in t) and len(words) <= 2:
        return True  # "OFFICIAL REPORTS", "SYLLABUS"
    if sum(bool(re.search(r"[\d_]", w)) for w in words) >= max(1, len(words) / 2):
        return True  # "601us1r05_1qm2", "IF13335", "scan 0042"
    return False


def _hint_title(hint: str) -> str:
    t = clean_text(hint or "")
    t = re.sub(r"\.pdf$", "", t, flags=re.I).strip()
    t = re.sub(r"^(?:(?:re|fw|fwd|aw|wg|tr)\s*:\s*)+", "", t, flags=re.I).strip()
    if t and " " not in t and re.search(r"[_\-]", t):
        t = re.sub(r"[_]+", " ", t).strip()
    return t


def guess_title_author(doc, pages, blocks, title_hint: str, bsize: float = 10.0) -> tuple[str, str]:
    md = {}
    try:
        md = doc.get_metadata_dict(skip_empty=True)
    except Exception:
        pass
    meta_title = _final(TITLE_PREFIX.sub("", md.get("Title", "") or ""))
    meta_title = re.sub(r"\.(docx?|pptx?|pdf)$", "", meta_title, flags=re.I).strip()
    author = _final(md.get("Author", "") or "")
    hint = _hint_title(title_hint)
    tblk = next((b for b in blocks if b.kind == "title"), None)
    printed = _final(tblk.text) if tblk is not None else ""
    if not printed:
        # the largest text near the top of the first page with text, if it stands out
        cand = [ln for p in pages[:2] for ln in p.lines if ln.role == "body" and len(ln.text.strip()) > 3]
        if cand:
            big = max(ln.size for ln in cand)
            top = [ln for ln in cand if ln.size == big and ln.page == cand[0].page]
            if big >= 1.2 * bsize and len(top) <= 4:
                printed = _final(" ".join(ln.text.strip() for ln in top))[:150]
    title = ""
    for t in (meta_title, printed, hint):
        if t and not _weak_title(t):
            title = t
            break
    if not title:
        title = next((t for t in (printed, hint, meta_title) if t), "") or "PDF document"
    if author and (JUNK_AUTHOR.search(author) or JUNK_AUTHOR_I.match(author)):
        author = ""
    author = re.sub(r"\s*;\s*", ", ", author)
    return title[:200], author[:200]


def pdf_language(doc) -> str:
    try:
        import pypdfium2.raw as R

        n = R.FPDFCatalog_GetLanguage(doc, None, 0)
        if n and 2 < n < 200:
            buf = (ctypes.c_ushort * (n // 2))()
            R.FPDFCatalog_GetLanguage(doc, buf, n)
            v = bytes(buf).decode("utf-16-le", "replace").rstrip("\x00").strip()
            if re.match(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$", v) and v.lower() not in ("x-none", "und"):
                return v
    except Exception:
        pass
    return ""


def pdf_date(doc) -> str:
    try:
        v = doc.get_metadata_value("CreationDate") or ""
    except Exception:
        return ""
    m = re.match(r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?", v.strip())
    if not m:
        return ""
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if not (1990 <= int(y) <= time.gmtime().tm_year + 1):
        return ""
    if mo and 1 <= int(mo) <= 12:
        if d and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}"
        return f"{y}-{mo}"
    return y


def run_ocr(doc, pages, *, lang: str, dpi: int, max_pages: int, deadline: float | None,
            notes: list, stats: dict, log: list) -> None:
    want = [p for p in pages if p.needs_ocr]
    if not want:
        return
    exe = ocr.tesseract_path()
    if not exe:
        notes.append("OCR is turned on, but the tesseract program is not installed, so scanned pages were not recognized.")
        return
    done = 0
    started = time.monotonic()
    for pg in want:
        if done >= max_pages:
            notes.append(f"OCR was limited to {max_pages} pages; later scanned pages were not recognized.")
            break
        if deadline is not None:
            avg = (time.monotonic() - started) / done if done else 0.0
            if time.monotonic() + max(avg * 1.2, 2.0) > deadline:
                notes.append(f"OCR stopped after {done} page{'s' if done != 1 else ''} to stay within the time limit."
                             if done else "OCR did not run: the time limit left no room for it.")
                break
        page = doc[pg.number]
        try:
            per_page = 90.0
            if deadline is not None:
                per_page = max(5.0, min(per_page, deadline - time.monotonic()))
            lines = ocr.ocr_page(page, pg.number, lang=lang, dpi=dpi, timeout=per_page, tesseract=exe)
        except MemoryError:
            log.append(f"OCR failed on page {pg.number + 1}: out of memory")
            pg.ocr_failed = "out of memory"
            continue
        except RuntimeError as e:
            log.append(f"OCR failed on page {pg.number + 1}: {e}")
            pg.ocr_failed = str(e)
            if "timed out" in str(e):
                notes.append("OCR stopped: a page took too long to recognize.")
                break
            continue
        finally:
            page.close()
        pg.lines = lines
        pg.chars = len("".join(ln.text for ln in lines).replace(" ", ""))
        pg.garbage = 0.0
        pg.ocr = True
        done += 1
    stats["ocr_pages"] = done
    if done:
        notes.append(f"Text on {done} scanned page{'s' if done != 1 else ''} was recognized by OCR and may contain errors.")
    elif any(p.ocr_failed for p in pages):
        reason = next(p.ocr_failed for p in pages if p.ocr_failed)
        notes.append(f"OCR failed ({reason}), so scanned pages were not recognized.")


def convert(data: bytes, *, title_hint: str = "", source_url: str = "", max_pages: int = 400,
            ocr_enabled: bool = False, ocr_lang: str = "eng", ocr_dpi: int = 300, ocr_max_pages: int = 20,
            deadline: float | None = None, stats: dict | None = None) -> Document:
    t0 = time.perf_counter()
    stats = stats if stats is not None else {}
    log: list = []
    stats["log"] = log
    notes: list = []
    doc = open_pdf(data)
    try:
        return _convert(doc, title_hint=title_hint, source_url=source_url, max_pages=max_pages,
                        ocr_enabled=ocr_enabled, ocr_lang=ocr_lang, ocr_dpi=ocr_dpi, ocr_max_pages=ocr_max_pages,
                        deadline=deadline, stats=stats, log=log, notes=notes, t0=t0)
    finally:
        doc.close()


def _convert(doc, *, title_hint, source_url, max_pages, ocr_enabled, ocr_lang, ocr_dpi, ocr_max_pages,
             deadline, stats, log, notes, t0) -> Document:
    n_total = len(doc)
    pages = extract.extract(doc, max_pages=max_pages, stats=stats, log=log, deadline=deadline)
    if n_total > len(pages):
        why = " to stay within the time limit" if stats.get("stopped_for_time") else ""
        notes.append(f"Only the first {len(pages)} of {n_total} pages were converted{why}.")
    stats["extract_s"] = round(time.perf_counter() - t0, 3)
    if ocr_enabled:
        run_ocr(doc, pages, lang=ocr_lang, dpi=ocr_dpi, max_pages=ocr_max_pages, deadline=deadline,
                notes=notes, stats=stats, log=log)
    stats.update(pages=len(pages), pages_total=n_total,
                 scanned_pages=sum(p.needs_ocr and not p.ocr for p in pages))
    bsize = layout.body_size(pages)
    stats["body_size"] = bsize
    slides = bool(pages) and sum(p.width > p.height for p in pages) > 0.8 * len(pages) and \
        sum(p.chars for p in pages) / max(1, len(pages)) < 900
    stats["slides_mode"] = slides
    layout.mark_furniture(pages, stats, bsize)
    fig = 0
    flows = {}
    for pg in pages:
        fig += layout.mark_figure_text(pg, bsize, pg.drawing_clusters if not pg.ocr else [])
        flow = layout.reading_order(pg, bsize)
        if not slides:
            layout.mark_footnotes(flow, pg, bsize)
        flows[pg.number] = flow
    stats["figure_lines_dropped"] = fig
    text_pages = [p for p in pages if p.chars > 50]
    ocr_like = bool(text_pages) and sum(p.image_cover > 0.8 for p in text_pages) > 0.5 * len(text_pages)
    stats["ocr_text_layer"] = ocr_like
    builder = structure.Builder(pages, bsize, slides=slides, ocr_like=ocr_like)
    raw_blocks = builder.build(flows)
    title, author = guess_title_author(doc, pages, raw_blocks, title_hint, bsize)
    out_blocks, has_tabular = blockconv.to_model(raw_blocks, builder, title)
    scanned = stats["scanned_pages"]
    if pages and scanned == len(pages):
        out_blocks = []  # no words: the ingest marks the item failed and shows notes[0]
        notes.insert(0, SCANNED_ALL)
        if not ocr_enabled:
            notes.insert(1, SCANNED_HINT)
    elif scanned:
        notes.append(f"{scanned} scanned page{'s' if scanned != 1 else ''} had no text layer and could not be converted."
                     + ("" if ocr_enabled else " " + SCANNED_HINT))
    if has_tabular:
        notes.append("Tables were simplified to plain lines of text.")
    if not pages:
        notes.insert(0, "This PDF has no pages.")
    elif not out_blocks and scanned != len(pages):
        notes.insert(0, "No text was found in this PDF.")
    lang = pdf_language(doc) or guess_lang(" ".join(getattr(b, "text", "") for b in out_blocks[:400]))
    stats.update(title=title, author=author, lang=lang, blocks=len(out_blocks), footnotes=len(builder.notes),
                 total_s=round(time.perf_counter() - t0, 3),
                 ms_per_page=round(1000 * (time.perf_counter() - t0) / max(1, len(pages)), 1))
    return Document(
        title=clean_text(title) or "PDF document",
        blocks=out_blocks,
        byline=clean_text(author),
        source_url=source_url or "",
        published=pdf_date(doc),
        lang=lang or "en",
        notes=notes,
    )
