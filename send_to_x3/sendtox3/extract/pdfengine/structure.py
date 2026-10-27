"""Ordered lines -> semantic blocks (headings, paragraphs, list items, captions,
footnotes, tables) with paragraph re-flow and dehyphenation."""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

from .layout import NOTE_MARK, is_note_start

BULLET = re.compile(r"^\s*([•◦▪▫●○■□➢❖✓►▶‣⁃∙·]|[-–—*](?=\s))\s*")
ENUM = re.compile(r"^\s*(\(?\d{1,2}[.)]|\(?[a-z][.)]|\([ivx]{1,4}\))\s+(?=\S)")
TERMINAL = tuple(".!?:;\"”’)»")
ROMAN_HEAD = re.compile(r"^\s*(?:[IVXL]{1,6}|[A-Z]|\d{1,2})\.?\s*$")
NUMBERED_HEAD = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+[A-Z(“\"]")
KEEP_PREFIX = {"self", "non", "well", "ill", "all", "ex", "quasi", "half", "high", "low", "long", "short", "cross"}
WORD = re.compile(r"[A-Za-z][A-Za-z']*(?:-[A-Za-z][A-Za-z']*)*")


@dataclass
class Seg:
    text: str
    bold: bool = False
    italic: bool = False
    sup: bool = False
    note: str | None = None       # footnote id when this is a linked marker


@dataclass
class Block:
    kind: str                      # para | li | caption | heading | table | notice | title
    lines: list = field(default_factory=list)
    page: int = 0
    col: int = 0
    size: float = 0.0
    bold: bool = False
    level: int = 0
    rows: list | None = None
    first_in_col: bool = False
    indented: bool = False
    after_float: bool = False
    notes: list = field(default_factory=list)   # footnotes attached for rendering
    text_override: str | None = None
    ordered: bool = False

    @property
    def text(self) -> str:
        return self.text_override if self.text_override is not None else " ".join(l.text.strip() for l in self.lines)

    @property
    def last_char(self) -> str:
        t = self.text.rstrip()
        return t[-1] if t else ""

    @property
    def complete(self) -> bool:
        return self.last_char in TERMINAL


NUMTOK = re.compile(r"^[(\[]?[-+±]?[\d.,/%]+[)\]%*]?$|^[-–—]$")


def _tabular(text: str) -> bool:
    toks = text.split()
    return len(toks) >= 3 and sum(bool(NUMTOK.match(t)) for t in toks) >= 0.4 * len(toks)


class Vocab:
    """Words seen *inside* lines; used to decide whether a line-end hyphen is real."""

    def __init__(self, pages):
        self.plain, self.hyph = Counter(), Counter()
        for p in pages:
            for l in p.lines:
                toks = WORD.findall(l.text)
                for t in toks[:-1] if l.text.rstrip().endswith(("-", "\u00ad")) else toks:
                    t = t.lower()
                    (self.hyph if "-" in t else self.plain)[t] += 1

    def join(self, a: str, b: str, soft: bool) -> str:
        """a ends with a hyphen (removed already), b is the next line. Return joined text."""
        if soft:
            return a + b
        m1 = re.search(r"([A-Za-z]+)$", a)
        m2 = re.match(r"([A-Za-z]+)", b)
        if not m1 or not m2:
            return a + "-" + b
        f1, f2 = m1.group(1), m2.group(1)
        if not f2[0].islower():
            return a + "-" + b                      # COVID-19, Jean-Pierre
        m3 = re.search(r"([A-Za-z]+(?:-[A-Za-z]+)+)$", a)
        if m3:                                      # "state-of-the-" + "art": a compound goes on
            return a + "-" + b
        hy, pl = f"{f1}-{f2}".lower(), f"{f1}{f2}".lower()
        if self.hyph[hy] > self.plain[pl]:
            return a + "-" + b
        if self.plain[pl] > 0:
            return a + b
        if f1.lower() in KEEP_PREFIX:
            return a + "-" + b
        return a + b


def line_segments(line, note_ids: dict) -> list:
    segs = []
    for s in line.spans:
        t = s.text
        if s.sup and t.strip():
            if t[:1].isspace() and segs:
                segs.append(Seg(" "))
            parts = [x for x in re.split(r"(,)", t.strip()) if x]
            for part in parts:
                nid = note_ids.get((line.page, part.strip())) if part != "," else None
                segs.append(Seg(part, sup=True, note=nid))
            if t[-1:].isspace():
                segs.append(Seg(" "))  # "dataset,15 and": keep the space after the marker
        else:
            segs.append(Seg(t, s.bold, s.italic))
    return segs


def join_lines(lines, vocab: Vocab, note_ids: dict, strip_marker: re.Pattern | None = None) -> list:
    """Concatenate lines into a list of Segs, de-hyphenating at line ends."""
    out: list = []
    for i, l in enumerate(lines):
        segs = line_segments(l, note_ids)
        if i == 0 and strip_marker is not None and segs:
            segs[0].text = strip_marker.sub("", segs[0].text, count=1)
        # trim
        while segs and not segs[-1].text.strip():
            segs.pop()
        if segs:
            segs[-1].text = segs[-1].text.rstrip()
        if not segs:
            continue
        if not out:
            segs[0].text = segs[0].text.lstrip()
            out.extend(segs)
            continue
        prev = out[-1]
        nxt = segs[0]
        nxt.text = nxt.text.lstrip()
        if prev.text.endswith("-") and not prev.sup and nxt.text[:1].isdigit():
            pass                                   # "COVID-" + "19" -> "COVID-19"
        elif prev.text.endswith(("-", "\u00ad")) and not prev.sup and nxt.text[:1].isalpha():
            soft = prev.text.endswith("\u00ad")
            head = prev.text[:-1]
            joined = vocab.join(head, nxt.text, soft)
            # keep style of the first fragment for the joined word
            prev.text = joined[: len(head) + (len(joined) - len(head) - len(nxt.text))]
            nxt.text = joined[len(prev.text):]
            if prev.bold == nxt.bold and prev.italic == nxt.italic and not nxt.sup:
                prev.text += nxt.text
                segs = segs[1:]
        elif prev.text.endswith(("—", "–", "/")):
            pass
        else:
            prev.text += " "
        out.extend(segs)
    for s in out:
        s.text = s.text.replace("\u00ad", "")
    return out


class Builder:
    def __init__(self, pages, bsize: float, slides: bool = False, ocr_like: bool = False):
        self.ocr_like = ocr_like      # OCR text layer: sizes/bold are noise, be lenient
        self.pages = {p.number: p for p in pages}
        self.bsize = bsize
        self.slides = slides
        self.vocab = Vocab(pages)
        gaps = []
        for p in pages:
            ls = [l for l in p.lines if l.role == "body" and abs(l.size - bsize) < 0.6]
            for a, b in zip(ls, ls[1:]):
                if a.block == b.block and b.y0 > a.y0:
                    gaps.append(b.y0 - a.y1)
        self.gap = statistics.median(gaps) if gaps else 0.25 * bsize
        self.note_ids: dict = {}
        self.notes: dict = {}        # id -> (number, segs, page)

    # ----------------------------------------------------------- footnotes
    def collect_footnotes(self, flow) -> None:
        cur = None
        for l in flow:
            if l.role != "footnote":
                continue
            m = NOTE_MARK.match(l.text)
            sep_marker = is_note_start(l) and not m
            if cur is None and not (m or sep_marker) and self.notes:
                cur = list(self.notes.values())[-1]       # note continued from previous page/column
                cur[2].append(l)
                continue
            if m or sep_marker or cur is None:
                num = l.spans[0].text.strip() if sep_marker else (m.group(1) if m else "*")
                if sep_marker:
                    l.spans[0].sup = True
                nid = f"fn-p{l.page + 1}-{re.sub(r'[^0-9A-Za-z]', 'x', num)}"
                while nid in self.notes:
                    nid += "b"
                cur = [nid, num, [l]]
                self.notes[nid] = cur
                self.note_ids[(l.page, num)] = nid
            else:
                cur[2].append(l)

    def note_segs(self, nid):
        _, num, lines = self.notes[nid]
        first = lines[0]
        strip = re.compile(r"^\s*" + re.escape(num) + r"\s*") if not (first.spans and first.spans[0].sup) else None
        if first.spans and first.spans[0].sup:
            lines = [type(first)(first.page, first.x0, first.y0, first.x1, first.y1, first.spans[1:], first.block)] + lines[1:]
        return join_lines(lines, self.vocab, {}, strip)

    # ----------------------------------------------------------- page -> raw blocks
    def raw_blocks(self, flow, page) -> list:
        blocks: list = []
        body = [l for l in flow if l.role in ("body", "table")]
        # per-column geometry
        colinfo = {}
        for c in {l.col for l in body}:
            cl = [l for l in body if l.col == c and l.role == "body"]
            if not cl:
                continue
            lefts = Counter(round(l.x0) for l in cl)
            left = min(x for x, n in lefts.items() if n >= max(1, 0.2 * lefts.most_common(1)[0][1]))
            right = sorted(l.x1 for l in cl)[int(0.9 * (len(cl) - 1))]
            just = sum(abs(l.x1 - right) < 1.5 for l in cl) / len(cl)
            colinfo[c] = (left, right, just > 0.45)
        prev = None
        cur = None
        seen_cols = set()
        for l in body:
            if l.role == "table":
                cur = Block("table", [l], l.page, l.col, rows=l.payload)
                blocks.append(cur); prev = None; cur = None
                continue
            left, right, justified = colinfo.get(l.col, (l.x0, l.x1, False))
            sz = max(l.size, 1)
            text = l.text
            dash = bool(re.match(r"^\s*[-–—*]\s", text))
            is_bullet = (bool(BULLET.match(text)) and not dash) or ((bool(ENUM.match(text)) or dash) and (
                prev is None or prev.text.rstrip().endswith((".", ":", ";", "?", "!")) or l.y0 - prev.y1 > self.gap + 0.5 * sz))
            is_caption = bool(re.match(r"^\s*(fig(?:ure)?\.?|table|chart|exhibit)\s*[A-Z]?\d+(?:\.\d+)?\s*[.:—–|-]", text, re.I))
            indented = (l.x0 - left) > 0.8 * sz
            new = False
            if cur is not None and prev is not None and prev.col == l.col and abs(l.y0 - prev.y0) < 0.3 * sz and l.x0 >= prev.x1 - 1:
                cur.lines.append(l); prev = l      # same visual line (cells, spaced names)
                continue
            if cur is None or prev is None or prev.col != l.col or l.y0 < prev.y0 - 2:
                new = True
            elif is_bullet or is_caption:
                new = True
            elif not self.ocr_like and (abs(l.size - prev.size) > 0.6 or l.bold != prev.bold and (len(text.strip()) > 3)):
                new = True
            elif self.ocr_like and abs(l.size - prev.size) > 0.3 * sz:
                new = True
            else:
                gap = l.y0 - prev.y1
                if gap > self.gap + 0.5 * sz:
                    new = True
                elif cur.kind == "li" and indented:
                    new = False            # hanging indent continuation
                elif indented and (prev.x0 - left) < 0.5 * sz and (prev.text.rstrip().endswith(TERMINAL) or prev.x1 < right - 2 * sz):
                    new = True
                else:
                    slack = right - prev.x1
                    limit = 1.5 * sz if justified else max(8 * sz, 0.3 * (right - left))
                    if slack > limit and prev.text.rstrip().endswith(TERMINAL):
                        new = True
                    elif slack > limit and justified and not text[:1].islower():
                        new = True
            if new and cur is not None and prev is not None and prev.col == l.col and l.y0 >= prev.y0 - 2 \
                    and prev.text.rstrip().endswith(("-", "­")) and text.lstrip()[:1].islower() \
                    and not is_bullet and not is_caption and abs(l.size - prev.size) <= 0.6:
                new = False                # "As-" / "suming ...": a word broken at the line end
            if new:
                kind = "li" if is_bullet else "caption" if is_caption else "para"
                switched = prev is None or prev.col != l.col or l.y0 < prev.y0 - 2
                cur = Block(kind, [l], l.page, l.col, size=l.size, bold=l.bold,
                            first_in_col=switched, indented=indented)
                cur.ordered = bool(ENUM.match(text)) and not BULLET.match(text)
                seen_cols.add(l.col)
                blocks.append(cur)
            else:
                cur.lines.append(l)
            prev = l
        # number-heavy blocks (tables without rules detection) keep their line breaks
        for b in blocks:
            if b.kind == "para" and len(b.lines) >= 2:
                tab = sum(1 for l in b.lines if _tabular(l.text))
                if tab >= 0.5 * len(b.lines):
                    b.kind = "tabular"
            elif b.kind == "para" and len(b.lines) == 1 and _tabular(b.text) and len(b.text.split()) >= 5:
                b.kind = "tabular"
        # heading candidates
        for b in blocks:
            if b.kind == "li" and b.ordered and (b.size >= self.bsize * 1.12 and len(b.lines) <= 2
                                                 or b.bold and len(b.lines) == 1 and len(b.text.split()) <= 10) \
                    and len(b.text) < 120 and not b.text.rstrip().endswith((".", ",", ";")):
                b.kind = "para"          # "1. Introduction" set in a heading font
            if b.kind != "para":
                continue
            t = b.text.strip()
            if len(b.lines) > 3 or len(t) > 160 or not t:
                continue
            if len(re.findall(r"[A-Za-z]", t)) < 3 and not ROMAN_HEAD.match(t):
                continue
            if t.endswith((".", ",", ";")) and not NUMBERED_HEAD.match(t):
                continue
            words = len(t.split())
            big = b.size >= self.bsize * (1.5 if self.ocr_like else 1.12)
            boldish = b.bold and b.size >= self.bsize * 0.88 and words <= 14 and not self.ocr_like
            l0 = b.lines[0]
            left, right, _ = colinfo.get(b.col, (l0.x0, l0.x1, False))
            centered = abs(l0.cx - (left + right) / 2) < 0.06 * (right - left) and l0.width < 0.4 * (right - left)
            roman = centered and ROMAN_HEAD.match(t) and len(b.lines) == 1
            if big or boldish or roman:
                b.kind = "heading"
                b.level = -1 if roman else 0
        return blocks

    # ----------------------------------------------------------- stitching
    def is_float(self, b, ref) -> bool:
        if b.kind in ("caption", "table"):
            return True
        if b.kind in ("para", "li") and ref is not None and abs(b.size - ref.size) > 0.6:
            return True
        return False

    @staticmethod
    def cut_midway(ref) -> bool:
        """Did this paragraph stop at a column/page end (last line runs to the margin)?"""
        if len(ref.lines) < 2:
            return ref.last_char in "-\u00ad,"
        right = max(l.x1 for l in ref.lines)
        left = min(l.x0 for l in ref.lines)
        last = ref.lines[-1]
        return last.x1 >= right - 0.25 * (right - left) or ref.last_char in "-\u00ad,"

    def stitch(self, raw: list) -> list:
        """Re-join paragraphs split by column/page breaks.  Captions, tables and text in a
        different type size that sit between the two halves ("floats": figure captions,
        side boxes) are kept, but emitted after the re-joined paragraph."""
        out: list = []
        open_i = None          # index in `out` of the paragraph that may continue
        prev_raw = None
        held = 0
        for b in raw:
            ref = out[open_i] if open_i is not None else None
            if ref is not None and b.kind == "para" and ref.kind == "li" and b.first_in_col \
                    and b.text[:1].islower() and abs(b.size - ref.size) <= 0.6:
                ref.lines.extend(b.lines); prev_raw = b
                continue
            if ref is not None and b.kind == "para" and not b.indented \
                    and abs(b.size - ref.size) <= 0.6 and b.bold == ref.bold \
                    and (b.first_in_col or (prev_raw is not None and self.is_float(prev_raw, ref))) \
                    and (b.text[:1].islower() or (not ref.complete and self.cut_midway(ref))):
                ref.lines.extend(b.lines)
                prev_raw = b
                continue
            out.append(b)
            if b.kind in ("para", "li"):
                # a different-size block only "floats" past an *unfinished* paragraph,
                # and only for a few blocks (side box, caption, footnote-like text)
                if ref is None or ref.complete or not self.is_float(b, ref) or held >= 6:
                    open_i, held = len(out) - 1, 0
                else:
                    held += 1
            elif b.kind in ("heading", "title"):
                open_i = None
            prev_raw = b
        return out

    # ----------------------------------------------------------- headings
    def assign_levels(self, blocks: list) -> None:
        heads = [b for b in blocks if b.kind == "heading"]
        if not heads:
            return
        # a "heading style" used for most of the document is body text, not a heading
        keys = Counter((round(b.size * 2) / 2, b.bold) for b in heads)
        total = sum(1 for b in blocks if b.kind in ("para", "li", "heading"))
        for b in heads:
            k = (round(b.size * 2) / 2, b.bold)
            if keys[k] > 0.45 * total and total > 8 and b.size < self.bsize * 1.12:
                b.kind = "para"
        heads = [b for b in heads if b.kind == "heading"]
        p0 = [b for b in heads if b.page == min(x.page for x in blocks)]
        if p0:
            top = max(p0, key=lambda b: b.size)
            if top.size >= self.bsize * 1.3 and top.lines[0].y0 < self.pages[top.page].height * 0.45:
                top.kind, top.level = "title", 1
                heads = [b for b in heads if b is not top]
        styles = sorted({(round(b.size * 2) / 2, b.bold) for b in heads if b.level == 0}, key=lambda k: (-k[0], not k[1]))
        rank = {k: i for i, k in enumerate(styles)}
        for b in heads:
            if b.level == -1:
                b.level = 4 if re.match(r"^\s*[A-Z]\.?\s*$", b.text) and not re.match(r"^\s*[IVX]+\.?\s*$", b.text) else 3
                continue
            m = NUMBERED_HEAD.match(b.text)
            if m:
                b.level = min(4, 1 + m.group(1).count(".") + 1)
            else:
                b.level = min(4, 2 + rank[(round(b.size * 2) / 2, b.bold)])

    SECTION_WORDS = re.compile(r"^\s*(abstract|summary|introduction|contents|table of contents|preface|foreword|overview|background|executive summary|syllabus|opinion)\b", re.I)

    def front_matter(self, blocks: list) -> None:
        """Bold/large lines between the title and the first real section/paragraph are
        authors, affiliations, dates: render them as plain 'meta' lines, not headings."""
        ti = next((i for i, b in enumerate(blocks) if b.kind == "title"), None)
        if ti is None:
            return
        for b in blocks[:ti]:
            if b.kind == "heading":
                b.kind = "meta"
        for b in blocks[ti + 1:]:
            if b.kind == "heading" and (self.SECTION_WORDS.match(b.text) or NUMBERED_HEAD.match(b.text)):
                break
            if b.kind == "para" and len(b.text) > 200:
                break
            if b.page != blocks[ti].page:
                break
            if b.kind == "heading":
                b.kind = "meta"

    # ----------------------------------------------------------- entry point
    def build(self, flows: dict) -> list:
        raw = []
        for pno in sorted(flows):
            flow = flows[pno]
            self.collect_footnotes(flow)
            pb = self.raw_blocks(flow, self.pages[pno])
            if self.slides and pb:
                cand = [b for b in pb if b.kind in ("para", "heading") and len(b.text) < 120]
                if cand:
                    h = max(cand[:3], key=lambda b: b.size)
                    if h.size >= self.bsize:
                        h.kind, h.level = "heading", 2
                        h.first_in_col = False
            raw.extend(pb)
            p = self.pages[pno]
            if p.needs_ocr and not p.ocr:
                raw.append(Block("notice", [], pno, text_override=f"[Page {pno + 1} is a scanned image with no text layer; it was not converted.]"))
        blocks = self.stitch(raw)
        if not self.slides:
            self.assign_levels(blocks)
            self.front_matter(blocks)
        # attach footnotes to the block that references them (or the last block of their page)
        used = set()
        last_on_page = {}
        for b in blocks:
            last_on_page[b.page] = b
        for b in blocks:
            for l in b.lines:
                for s in l.spans:
                    if s.sup:
                        for part in s.text.split(","):
                            nid = self.note_ids.get((l.page, part.strip()))
                            if nid and nid not in used:
                                b.notes.append(nid); used.add(nid)
        for nid, (_, num, lines) in self.notes.items():
            if nid not in used:
                host = last_on_page.get(lines[0].page) or (blocks[-1] if blocks else None)
                if host is not None:
                    host.notes.append(nid)
        return blocks
