"""Structure blocks (prototype ``structure.Block``) -> text-model blocks.

Our model has no inline styling: bold/italic are dropped, footnote references become
``[n]`` and point at a final "Notes" section, other superscript digits become Unicode
superscripts (the X3 fonts cover U+00B9/B2/B3 and U+2070-208E).
"""

from __future__ import annotations

import re

from . import textnorm
from .structure import BULLET, join_lines
from ...textmodel import Heading, ListItem, Paragraph, Preformatted, Rule, clean_text

SUP_MAP = str.maketrans("0123456789+-=()−", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾⁻")
SUP_OK = re.compile(r"^[0-9+\-=()−]+$")
NUM_ENUM = re.compile(r"^\s*\(?(\d{1,3})[.)]\s+")
NOTES_TITLE = "Notes"
DINKUS = re.compile(r"^\s*(?:[*•·⁂∗]\s*){3,}$|^\s*⁂\s*$")


def _norm(t: str) -> str:
    return re.sub(r"\W+", "", t.lower())


def segs_text(segs, labels: dict) -> str:
    out = []
    for s in segs:
        t = s.text.replace("­", "")
        if not t:
            continue
        lead = " " if t[:1].isspace() else ""
        trail = " " if t[-1:].isspace() else ""
        if s.note and s.note in labels:
            out.append(f"{lead}[{labels[s.note]}]{trail}")
            continue
        if s.sup:
            core = t.strip()
            out.append(lead + (core.translate(SUP_MAP) if SUP_OK.match(core) else core) + trail)
            continue
        out.append(t)
    text = "".join(out)
    text = re.sub(r"\],\[", "][", text)
    return textnorm.final(re.sub(r"\s+", " ", text))


def _text(blk, builder, labels, strip_marker=None) -> str:
    return segs_text(join_lines(blk.lines, builder.vocab, builder.note_ids, strip_marker), labels)


def _note_labels(blocks, builder) -> tuple[list, dict]:
    order: list = []
    seen: set = set()
    for b in blocks:
        for nid in b.notes:
            if nid in builder.notes and nid not in seen:
                seen.add(nid)
                order.append(nid)
    nums = [str(builder.notes[n][1]).strip() for n in order]
    if len(set(nums)) == len(nums) and all(nums):
        labels = dict(zip(order, nums))
    else:  # markers restart per chapter/opinion or repeat: number them in order
        labels = {nid: str(i) for i, nid in enumerate(order, 1)}
    return order, labels


def to_model(blocks, builder, title: str) -> tuple[list, bool]:
    """Return (model blocks, whether number-heavy "tabular" blocks were flattened)."""
    order, labels = _note_labels(blocks, builder)
    out: list = []
    tabular = False
    ntitle = _norm(title)
    seen_title = False
    for blk in blocks:
        kind = blk.kind
        if kind == "skip":
            continue
        if kind == "notice":
            out.append(Paragraph(clean_text(blk.text)))
            continue
        if kind == "tabular":
            rows = [segs_text(join_lines([ln], builder.vocab, builder.note_ids), labels) for ln in blk.lines]
            rows = [clean_text(r) for r in rows if r.strip()]
            if rows:
                out.append(Preformatted("\n".join(rows)))
                tabular = True
            continue
        if kind in ("heading", "title"):
            text = clean_text(_text(blk, builder, labels))
            if not text:
                continue
            if kind == "title":
                if not seen_title and (_norm(text) == ntitle or ntitle.startswith(_norm(text)) and len(_norm(text)) > 10):
                    seen_title = True
                    continue
                out.append(Heading(text, level=1))
                continue
            out.append(Heading(text, level=max(1, min(3, blk.level or 2))))
            continue
        if kind in ("li", "para", "caption", "meta") and blk.lines and DINKUS.match(blk.lines[0].text):
            out.append(Rule())  # "* * *" section break, possibly glued to the next paragraph
            if len(blk.lines) == 1:
                continue
            blk.lines = blk.lines[1:]
            kind = "para" if kind == "li" else kind
        if kind == "li":
            first = blk.lines[0].text if blk.lines else ""
            if BULLET.match(first):
                text = clean_text(_text(blk, builder, labels, strip_marker=BULLET))
                if text:
                    out.append(ListItem(text, ordered=False))
                continue
            m = NUM_ENUM.match(first)
            if m:
                text = clean_text(_text(blk, builder, labels, strip_marker=NUM_ENUM))
                if text:
                    out.append(ListItem(text, ordered=True, index=int(m.group(1))))
                continue
            text = clean_text(_text(blk, builder, labels))  # "(a) ...", "iv) ...": keep the marker
            if text:
                out.append(Paragraph(text))
            continue
        # para, caption, meta
        text = clean_text(_text(blk, builder, labels))
        if text:
            out.append(Paragraph(text))
    if order:
        notes = []
        for nid in order:
            body = clean_text(segs_text(builder.note_segs(nid), {}))
            if body:
                notes.append(Paragraph(f"[{labels[nid]}] {body}"))
        if notes:
            out.append(Heading(NOTES_TITLE, level=2))
            out.extend(notes)
    return out, tabular
