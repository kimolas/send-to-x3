"""PDF extraction (sendtox3.extract.pdf + extract.pdfengine) on generated PDFs.

The PDFs are written from raw PDF syntax by tests/fixtures/pdfs/pdfgen.py, so every
heuristic is exercised on a page whose geometry we know exactly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("pypdfium2")

from sendtox3.extract.pdf import convert_pdf, pdf_to_document  # noqa: E402
from sendtox3.extract.pdfengine import convert as engine  # noqa: E402
from sendtox3.extract.pdfengine import ocr as ocrmod  # noqa: E402
from sendtox3.extract.pdfengine.lang import guess_lang  # noqa: E402
from sendtox3.textmodel import Heading, ListItem, Paragraph, Preformatted, Rule  # noqa: E402

_GEN = Path(__file__).parent / "fixtures" / "pdfs" / "pdfgen.py"
_spec = importlib.util.spec_from_file_location("pdfgen", _GEN)
pdfgen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pdfgen)
Page, build, text_width = pdfgen.Page, pdfgen.build, pdfgen.text_width

WORDS = ("the reader turns each page of this short report slowly while the quiet rain falls on the "
         "old garden and a small bird sings near the window of the house by the river where people "
         "walk their dogs every morning before work begins in the busy town center").split()


def wrap(words: list[str], width: float, size: float = 10, font: str = "R") -> list[str]:
    lines, cur = [], []
    for w in words:
        if cur and text_width(" ".join(cur + [w]), font, size) > width:
            lines.append(" ".join(cur))
            cur = []
        cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return lines


def sentences(tag: str, n: int) -> list[str]:
    """n sentences, recognisable by ``tag`` and a number; different tags give
    different word sequences (identical text at the same spot on several pages would
    rightly be taken for a running header)."""
    out = []
    seed = sum(ord(c) * (k + 1) for k, c in enumerate(tag))
    for i in range(n):
        start = (seed * 5 + i * 7) % 36
        body = WORDS[start:start + 9]
        out.append(f"{tag}{i} " + " ".join(body) + ".")
    return out


def body_page(paras: list[str], *, x: float = 72, width: float = 468, y: float = 90, size: float = 10,
              leading: float = 12, page: Page | None = None, indent: float = 0) -> tuple[Page, float]:
    p = page or Page()
    for para in paras:
        lines = wrap(para.split(), width, size)
        for k, ln in enumerate(lines):
            p.text(x + (indent if k == 0 else 0), y, ln, size=size)
            y += leading
        y += leading * 0.6
    return p, y


def run(data: bytes, **kw):
    """In-process engine (fast); robustness tests below go through the subprocess."""
    return convert_pdf(data, isolate=False, **kw)


def texts(doc, kind=None) -> list[str]:
    return [b.text for b in doc.blocks if kind is None or isinstance(b, kind)]


def plain(doc) -> str:
    return "\n".join(getattr(b, "text", "") for b in doc.blocks)


# ---------------------------------------------------------------------------------------
# layout and structure
# ---------------------------------------------------------------------------------------


def test_two_column_reading_order_and_column_stitching():
    p = Page()
    p.text(150, 60, "A Study of Two Columns", size=20, font="B")
    left = [f"L{i} " + ln for i, ln in enumerate(wrap(WORDS * 6, 200))][:28]
    right = [f"R{i} " + ln for i, ln in enumerate(wrap(WORDS * 6, 200))][:28]
    # the last left line stops mid-sentence and runs to the margin; the right column
    # continues it with a lower-case word
    left[-1] = "and so the long sentence keeps going past the"
    right[0] = "column break without stopping. Then more text."
    y = 100
    for a, b in zip(left, right):
        p.text(54, y, a)
        p.text(318, y, b)
        y += 12
    r = run(build([p]))
    assert r.ok
    doc = r.doc
    assert doc.title == "A Study of Two Columns"
    t = plain(doc)
    # the whole left column comes first, then the right column, each in order
    order = [t.index(f"L{i} ") for i in range(27)] + [t.index(f"R{i} ") for i in range(1, 28)]
    assert order == sorted(order)
    # the sentence broken by the column change is one paragraph
    assert any("keeps going past the column break without stopping." in s for s in texts(doc, Paragraph))


def test_running_headers_footers_and_page_numbers_are_removed():
    pages = []
    for n in range(1, 5):
        p, _ = body_page(sentences(f"P{n}S", 6), y=100)
        p.text(72, 40, "The Journal of Synthetic Tests", size=9, font="I")
        p.text(480, 40, f"Vol. 3, No. {n}", size=9)
        p.text(300, 760, f"{n}", size=9)
        pages.append(p)
    doc = run(build(pages)).doc
    t = plain(doc)
    assert "Journal of Synthetic Tests" not in t
    assert "Vol. 3" not in t
    assert not any(s.strip().isdigit() for s in texts(doc))
    for n in range(1, 5):
        assert f"P{n}S0" in t and f"P{n}S5" in t


def test_dehyphenation_rules():
    p = Page()
    lines = [
        "The first example shows a demon-",
        "stration of a word broken at the end of the line, and a well-",
        "known compound that keeps its hyphen; a state-of-the-",
        "art method also keeps it, and COVID-",
        "19 keeps it before a digit. The end.",
    ]
    y = 100
    for ln in lines:
        p.text(72, y, ln)
        y += 12
    t = plain(run(build([p])).doc)
    assert "demonstration of a word" in t
    assert "well-known compound" in t
    assert "state-of-the-art method" in t
    assert "COVID-19 keeps" in t


def test_soft_hyphen_drawn_on_its_own_is_dropped():
    """Typesetters that emit discretionary hyphens as a separate show operation (the
    Supreme Court's prints): PDFium reports every line-end hyphen the same way, the
    separate text object tells them apart."""
    p = Page()
    p.runs(72, 100, [("This sentence was carefully high", "R", 10, 0), ("-", "R", 10, 0)])
    p.text(72, 112, "lighted by the editor, and it is self-")
    p.text(72, 124, "evident that both words survive. The end of the paragraph.")
    t = plain(run(build([p])).doc)
    assert "carefully highlighted by the editor" in t
    assert "self-evident that" in t


def test_headings_levels_and_title():
    p = Page()
    p.text(160, 70, "Synthetic Report Title", size=22, font="B")
    p.text(72, 120, "1 Introduction", size=14, font="B")
    _, y = body_page(sentences("I", 3), y=140, page=p)
    p.text(72, y + 10, "1.1 Background", size=11.5, font="B")
    _, y = body_page(sentences("B", 3), y=y + 30, page=p)
    p.text(72, y + 10, "2 Methods", size=14, font="B")
    body_page(sentences("M", 2), y=y + 30, page=p)
    doc = run(build([p])).doc
    assert doc.title == "Synthetic Report Title"
    heads = [(b.text, b.level) for b in doc.blocks if isinstance(b, Heading)]
    assert ("1 Introduction", 2) in heads
    assert ("1.1 Background", 3) in heads
    assert ("2 Methods", 2) in heads
    assert not any(b.text == "Synthetic Report Title" for b in doc.blocks)  # title is metadata


def test_footnote_marker_and_notes_section():
    p, y = body_page(sentences("F", 8), y=100)
    p.runs(72, y + 10, [("This claim needs a source.", "R", 10, 0), ("1", "R", 6, 4),
                        (" More text follows here.", "R", 10, 0)])
    p.runs(72, 730, [("1", "R", 5.5, 3), ("The source is a synthetic test document.", "R", 8, 0)])
    p.text(72, 740, "It has a second line of note text.", size=8)
    doc = run(build([p])).doc
    t = texts(doc)
    assert any("This claim needs a source.[1] More text follows here." in s for s in t)
    assert isinstance(doc.blocks[-2], Heading) and doc.blocks[-2].text == "Notes"
    assert doc.blocks[-1] == Paragraph("[1] The source is a synthetic test document. It has a second line of note text.")


def test_unlinked_superscripts_become_unicode_superscripts():
    p = Page()
    p.runs(72, 100, [("Einstein wrote E = mc", "R", 10, 0), ("2", "R", 6.5, 4), (" in 1905, a famous result.", "R", 10, 0)])
    t = plain(run(build([p])).doc)
    assert "E = mc² in 1905" in t


def test_lists_bullets_and_numbers():
    p = Page()
    p.text(72, 100, "Things to remember:")
    p.text(72, 116, "• the first bullet item")
    p.text(72, 130, "• the second bullet item")
    p.text(72, 150, "1. the first numbered step.")
    p.text(72, 164, "2. the second numbered step.")
    doc = run(build([p])).doc
    items = [(b.text, b.ordered, b.index) for b in doc.blocks if isinstance(b, ListItem)]
    assert ("the first bullet item", False, 1) in items
    assert ("the second bullet item", False, 1) in items
    assert ("the first numbered step.", True, 1) in items
    assert ("the second numbered step.", True, 2) in items


def test_number_heavy_lines_become_a_simplified_table():
    p, y = body_page(sentences("T", 3), y=100)
    rows = ["Region 2019 2020 2021", "North 12.5 13.1 14.0", "South 9.8 10.2 11.7", "West 7.1 7.9 8.4"]
    for r_ in rows:
        p.text(72, y + 10, r_)
        y += 12
    doc = run(build([p])).doc
    pre = [b for b in doc.blocks if isinstance(b, Preformatted)]
    assert pre and "North 12.5 13.1 14.0" in pre[0].text.split("\n")
    assert "Tables were simplified to plain lines of text." in doc.notes


def test_section_break_dinkus_becomes_rule():
    p, y = body_page(sentences("D", 3), y=100)
    p.text(290, y + 10, "* * *")
    body_page(sentences("E", 2), y=y + 30, page=p)
    doc = run(build([p])).doc
    assert any(isinstance(b, Rule) for b in doc.blocks)
    assert "* * *" not in plain(doc)


def test_rotated_page():
    p = Page(rotate=90)
    for k, ln in enumerate(["Rotated page first line of text.", "Second line keeps the order.",
                            "Third line ends the paragraph."]):
        p.rotated_text(80 + 14 * k, 72, ln, size=11)
    t = plain(run(build([p])).doc)
    assert "Rotated page first line of text. Second line keeps the order. Third line ends the paragraph." in t


# ---------------------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------------------


def test_metadata_title_and_author():
    p, _ = body_page(sentences("M", 3), y=100)
    doc = run(build([p], title="A Proper Title From Metadata", author="Jane Q. Writer")).doc
    assert doc.title == "A Proper Title From Metadata"
    assert doc.byline == "Jane Q. Writer"


def test_junk_metadata_falls_back_to_printed_title_or_hint():
    p = Page()
    p.text(150, 70, "The Printed Big Title", size=20, font="B")
    body_page(sentences("J", 3), y=110, page=p)
    doc = run(build([p], title="Microsoft Word - draft7.docx", author="SE:W:CAR:MP")).doc
    assert doc.title == "The Printed Big Title"
    assert doc.byline == ""
    # nothing usable in the file: the hint (email subject / file name) wins
    q, _ = body_page(sentences("K", 3), y=100)
    doc = run(build([q], title="untitled"), title_hint="Fwd: Quarterly garden report.pdf").doc
    assert doc.title == "Quarterly garden report"


def test_language_from_catalog_and_guess():
    p, _ = body_page(sentences("L", 3), y=100)
    assert run(build([p], lang="fr-FR")).doc.lang == "fr-FR"
    q = Page()
    q.text(72, 100, "Der Bericht ist nicht lang, und die Ergebnisse sind mit den Daten von dem")
    q.text(72, 112, "Projekt auch für die Leser ein gutes Beispiel, das sich auf den Text bezieht.")
    assert run(build([q])).doc.lang == "de"
    assert guess_lang("Это короткий текст на русском языке для проверки") == "ru"
    assert guess_lang("") == "en"


def test_creation_date_becomes_published():
    p, _ = body_page(sentences("C", 2), y=100)
    assert run(build([p], created="D:20240111093000-05'00'")).doc.published == "2024-01-11"
    assert run(build([p])).doc.published == ""


# ---------------------------------------------------------------------------------------
# scans, encryption, caps, failures
# ---------------------------------------------------------------------------------------


def _scan_page() -> Page:
    p = Page()
    w, h = 120, 156
    data = bytes((x * 7 + y * 3) % 256 for y in range(h) for x in range(w))
    p.image(0, 0, 612, 792, w, h, data)
    return p


def test_image_only_pdf_is_reported_as_scanned():
    r = run(build([_scan_page(), _scan_page()]), ocr=False)
    assert r.ok
    assert r.doc.blocks == []
    assert r.doc.notes[0].startswith("Scanned PDF: no text layer")
    assert any("pdf_ocr" in n for n in r.doc.notes)


def test_mixed_document_marks_the_scanned_page():
    p, _ = body_page(sentences("S", 4), y=100)
    doc = run(build([p, _scan_page()]), ocr=False).doc
    assert any("Page 2 is a scanned image" in s for s in texts(doc))
    assert any("1 scanned page had no text layer" in n for n in doc.notes)


def test_password_protected_pdf():
    p, _ = body_page(sentences("E", 2), y=100)
    r = run(build([p], user_password="secret"), title_hint="statement.pdf")
    assert not r.ok and r.kind == "encrypted"
    assert r.doc.blocks == [] and "password-protected" in r.doc.notes[0]
    assert r.doc.title == "statement"


def test_owner_password_only_pdf_opens():
    p, _ = body_page(sentences("O", 2), y=100)
    r = run(build([p], owner_password="owner-only", title="Restricted but readable"))
    assert r.ok and r.doc.title == "Restricted but readable"
    assert "O0 " in plain(r.doc)


def test_page_cap_adds_a_note():
    pages = [body_page(sentences(f"C{i}S", 2), y=100)[0] for i in range(5)]
    doc = run(build(pages), max_pages=2).doc
    assert "Only the first 2 of 5 pages were converted." in doc.notes
    assert "C1S0" in plain(doc) and "C2S0" not in plain(doc)


def test_deadline_truncates_instead_of_failing():
    import time

    pages = [body_page(sentences(f"Q{i}Z", 2), y=100)[0] for i in range(4)]
    doc = engine.convert(build(pages), deadline=time.monotonic() - 1)
    assert "Only the first 1 of 4 pages were converted to stay within the time limit." in doc.notes
    assert "Q0Z0" in plain(doc)


@pytest.mark.parametrize("data, kind", [(b"", "empty"), (b"<html>no</html>", "invalid"),
                                        (b"%PDF-1.7\n garbage garbage", "invalid")])
def test_bad_input_never_raises(data, kind):
    r = convert_pdf(data, title_hint="x.pdf")
    assert not r.ok and r.kind == kind
    assert r.doc.blocks == [] and r.doc.notes and r.doc.title == "x"


# ---------------------------------------------------------------------------------------
# the subprocess wrapper
# ---------------------------------------------------------------------------------------


def test_subprocess_matches_in_process():
    p, _ = body_page(sentences("W", 5), y=100)
    p.text(72, 60, "Worker Round Trip", size=18, font="B")
    data = build([p], author="Ann Author")
    a = pdf_to_document(data, title_hint="t", source_url="https://example.org/a.pdf")
    b = run(data, title_hint="t", source_url="https://example.org/a.pdf").doc
    assert a == b
    assert a.source_url == "https://example.org/a.pdf" and a.byline == "Ann Author"


def test_timeout_returns_a_failure_document():
    p, _ = body_page(sentences("T", 2), y=100)
    r = convert_pdf(build([p]), timeout=0.01, title_hint="slow.pdf")
    assert not r.ok and r.kind == "timeout"
    assert r.doc.blocks == [] and "longer than" in r.doc.notes[0]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS is only enforced on Linux")
def test_memory_cap_returns_a_failure_document():
    p, _ = body_page(sentences("M", 2), y=100)
    r = convert_pdf(build([p]), max_memory_mb=16)
    assert not r.ok and r.kind in ("memory", "crash")
    assert r.doc.blocks == [] and r.doc.notes


async def test_async_wrapper():
    from sendtox3.extract.pdf import pdf_to_document_async

    p, _ = body_page(sentences("A", 2), y=100)
    doc = await pdf_to_document_async(build([p]), title_hint="async")
    assert "A0 " in plain(doc)


def test_worker_dict_round_trip():
    from sendtox3.extract.pdfengine.worker import doc_from_dict, doc_to_dict
    from sendtox3.textmodel import Document, Quote

    d = Document(title="T", blocks=[Heading("H", 3), Paragraph("p"), ListItem("i", True, 4, 1), Quote("q"),
                                    Preformatted("a\nb"), Rule()], byline="B", lang="de", notes=["n"])
    assert doc_from_dict(doc_to_dict(d)) == d


# ---------------------------------------------------------------------------------------
# OCR plumbing that needs no tesseract
# ---------------------------------------------------------------------------------------

HOCR = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class='ocr_page' id='page_1' title='image "p.pgm"; bbox 0 0 2550 3300; ppageno 0'>
 <div class='ocr_carea' id='block_1_1' title="bbox 300 400 1000 520">
  <p class='ocr_par' id='par_1_1' lang='eng' title="bbox 300 400 1000 520">
   <span class='ocr_header' id='line_1_1' title="bbox 300 400 980 470; baseline 0 -14; x_size 70; x_descenders 14; x_ascenders 18">
    <span class='ocrx_word' id='word_1_1' title='bbox 300 400 500 470; x_wconf 95'>Hello</span>
    <span class='ocrx_word' id='word_1_2' title='bbox 520 400 780 470; x_wconf 93'><strong>world,</strong></span>
    <span class='ocrx_word' id='word_1_3' title='bbox 800 400 980 470; x_wconf 91'>again</span>
   </span>
   <span class='ocr_line' id='line_1_2' title="bbox 300 480 710 529; baseline 0 -12; x_size 49; x_descenders 12; x_ascenders 12">
    <span class='ocrx_word' id='word_1_4' title='bbox 300 480 540 529; x_wconf 90'>second</span>
    <span class='ocrx_word' id='word_1_5' title='bbox 560 480 710 529; x_wconf 90'>line &amp; more</span>
   </span>
  </p>
 </div>
 <div class='ocr_carea' id='block_1_2' title="bbox 300 900 330 930">
  <p class='ocr_par' id='par_1_2' lang='eng' title="bbox 300 900 330 930">
   <span class='ocr_line' id='line_1_3' title="bbox 300 900 330 930; baseline 0 0; x_size 30">
    <span class='ocrx_word' id='word_1_6' title='bbox 300 900 330 930; x_wconf 20'>~</span>
   </span>
  </p>
 </div>
</div></body></html>
"""


def test_parse_tesseract_hocr():
    lines = ocrmod.parse_hocr(HOCR, 0, 300)
    assert [ln.text for ln in lines] == ["Hello world, again", "second line & more"]
    a, b = lines
    assert a.x0 == pytest.approx(300 * 72 / 300) and a.y0 == pytest.approx(400 * 72 / 300)
    assert a.block == b.block
    # sizes come from x_size, so a header in capitals still reads as bigger type
    assert a.size > 1.3 * b.size


def test_ocr_without_tesseract_is_reported(monkeypatch):
    monkeypatch.setattr(ocrmod, "tesseract_path", lambda: None)
    r = run(build([_scan_page()]), ocr=True)
    assert r.doc.blocks == []
    assert r.doc.notes[0].startswith("Scanned PDF")
    assert any("tesseract" in n for n in r.doc.notes)


def test_engine_open_errors():
    with pytest.raises(engine.PdfError) as e:
        engine.open_pdf(b"%PDF-1.4 nothing")
    assert not e.value.encrypted
