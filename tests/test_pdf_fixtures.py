"""PDF extraction on real documents (tests/fixtures/pdfs, see NOTICE there).

Checks the measured wins of the prototype on the pypdfium2 port: running headers and
watermarks removed, broken ligatures repaired, paragraphs re-joined across pages and
columns, document-vocabulary dehyphenation, footnotes, heading levels.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

pytest.importorskip("pypdfium2")

from sendtox3.extract.pdf import convert_pdf  # noqa: E402
from sendtox3.textmodel import Heading, Paragraph  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "pdfs"


def _doc(name: str, **kw):
    r = convert_pdf((FIX / name).read_bytes(), isolate=False, **kw)
    assert r.ok, r.doc.notes
    return r.doc


@pytest.fixture(scope="module")
def bert():
    return _doc("acl_bert_2col.pdf")


@pytest.fixture(scope="module")
def scotus():
    return _doc("scotus_601us1r05.pdf")


@pytest.fixture(scope="module")
def mmwr():
    return _doc("mmwr_7301a1.pdf")


@pytest.fixture(scope="module")
def crs():
    return _doc("crs_if13335_2col.pdf")


def text(doc) -> str:
    return "\n\n".join(getattr(b, "text", "") for b in doc.blocks)


def norm(t: str) -> str:
    """Lower-case words only, hyphens removed (so probes ignore dehyphenation style)."""
    t = unicodedata.normalize("NFKC", t).replace("-", "")
    t = re.sub(r"\[\d+\]", "", t)
    return re.sub(r"\s+", " ", re.sub(r"[^\w%]+", " ", t.lower()))


def in_order(doc, probes: list[str]) -> bool:
    n = norm(text(doc))
    pos = [n.find(p) for p in probes]
    return all(p >= 0 for p in pos) and pos == sorted(pos)


def test_bert_metadata_structure_and_order(bert):
    assert bert.title == "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
    assert bert.byline.startswith("Jacob Devlin, Ming-Wei Chang")
    assert bert.lang == "en" and bert.published == "2019-04-29"
    heads = {(b.text, b.level) for b in bert.blocks if isinstance(b, Heading)}
    assert ("1 Introduction", 2) in heads and ("2 Related Work", 2) in heads
    assert ("2.1 Unsupervised Feature-based Approaches", 3) in heads
    assert in_order(bert, ["we introduce a new language representation model called bert",
                           "at the token level tjong kim sang and de meulder 2003",
                           "there are two existing strategies for applying pretrained",
                           "we argue that current techniques restrict"])


def test_bert_dehyphenation(bert):
    t = unicodedata.normalize("NFKC", text(bert))
    for w in ("pre-trained", "fine-tuning", "task-specific", "state-of-the-art", "left-to-right"):
        assert w in t
    for w in ("pretrained", "finetuning", "taskspecific", "lefttoright"):
        assert not re.search(r"\b" + w + r"\b", t)
    assert "representation model" in t and "downstream tasks" in t
    assert len(re.findall(r"[a-z][-­￾]\s*\n\s*[a-z]", t)) <= 1
    # running page numbers (4171, 4172, ...) are gone
    assert not re.search(r"^\s*41\d\d\s*$", t, re.M)


def test_bert_footnotes(bert):
    i = next(k for k, b in enumerate(bert.blocks) if isinstance(b, Heading) and b.text == "Notes")
    notes = [b.text for b in bert.blocks[i + 1:]]
    assert len(notes) >= 10 and all(re.match(r"^\[[^\]]+\] ", n) for n in notes)
    assert any("github.com/tensorflow/tensor2tensor" in n for n in notes)
    body = "\n".join(b.text for b in bert.blocks[:i] if isinstance(b, Paragraph))
    assert re.search(r"\[\d+\]", body)  # in-text references to the notes


def test_scotus_furniture_ligatures_and_page_joins(scotus):
    t = text(scotus)
    assert "Page Proof Pending Publication" not in t
    assert not re.search(r"Cite as: 601 U\. S\.", t)
    # the PDF maps the fi/fl ligatures to a plain "f"
    for bad in (r"\bfled (a|briefs|suit|her)\b", r"\bfnd", r"\bfrst\b", r"specifcally", r"\bfow\b", r"affrm",
                r"refect", r"refiect", r"reffect"):
        assert not re.search(bad, t), bad
    assert "affirmative" in t and "reflect" in t and "specifically" in t
    # a sentence that straddles a page break (with the running head in between)
    assert "the double jeopardy clause prohibits secondguessing the reason" in norm(t)
    # discretionary hyphens are dropped, real ones kept
    assert "repugnant" in t and "so-called" in t


def test_scotus_title_hint(scotus):
    assert scotus.title == "OFFICIAL REPORTS"  # the cover page; no metadata title
    hinted = _doc("scotus_601us1r05.pdf", title_hint="McElrath v. Georgia (Supreme Court)")
    assert hinted.title == "McElrath v. Georgia (Supreme Court)"


def test_mmwr_running_header_and_reading_order(mmwr):
    t = text(mmwr)
    assert "MMWR / January 11, 2024 / Vol. 73 / No. 1" not in t
    assert in_order(mmwr, ["incorrect use of topical antifungals",
                           "superficial fungal skin infections have an estimated",
                           "the emergence and spread of antimicrobialresistant superficial fungal infections",
                           "among boardcertified dermatologists"])
    assert mmwr.byline == "Centers for Disease Control and Prevention"


def test_crs_two_columns(crs):
    assert in_order(crs, ["a federal student loan made under title iv", "in light of these policies",
                          "the first full quarter in which the payment pause was in effect the delinquency rate remained at 0",
                          "figure 1 shows that the share", "additionally the federal reserve bank of boston"])
    assert crs.byline == "Alexandra Hegji, Darryl E. Getter"
    assert "crsreports.congress.gov" not in "\n".join(b.text for b in crs.blocks[:-3] if hasattr(b, "text"))


def test_scan_fixture_without_ocr():
    r = convert_pdf((FIX / "archive_scan_page.pdf").read_bytes(), isolate=False, ocr=False)
    assert r.ok and r.doc.blocks == [] and r.doc.notes[0].startswith("Scanned PDF")
