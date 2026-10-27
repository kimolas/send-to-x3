"""Page-level layout heuristics: running headers/footers, watermarks, figure text,
columns / reading order, footnote zones."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

PAGENUM = re.compile(
    r"^\s*(?:[-–—]\s*)?(?:page\s+)?(?:\d{1,4}|[ivxlcdm]{1,7})(?:\s*(?:of|/)\s*\d{1,4})?(?:\s*[-–—])?\s*$", re.I)
CAPTION = re.compile(r"^\s*(fig(?:ure)?\.?|table|chart|exhibit|source:|note:)\s*[\dA-Z]?", re.I)
# "§ 2106. And ..." continues a note (a section sign before a number is a citation)
NOTE_MARK = re.compile(r"^\s*(\d{1,3}|[*†‡¶]{1,3}|§{1,3}(?!\s*\d))(?=\s|[A-Z(“\"'])")


def body_size(pages) -> float:
    c = Counter()
    for p in pages:
        for l in p.lines:
            c[l.size] += len(l.text)
    return c.most_common(1)[0][0] if c else 10.0


def _key(text: str) -> str:
    t = re.sub(r"\d+", "#", text.lower())
    t = re.sub(r"\b[ivxlc]{1,6}\b", "#", t)      # roman page numbers
    return re.sub(r"\s+", " ", t).strip()


TOC_LINE = re.compile(r"(?:\.\s?){4,}\s*[\divxlc]+\s*$|…{2,}\s*[\divxlc]+\s*$", re.I)


def mark_furniture(pages, stats: dict, bsize: float = 10.0) -> None:
    """Running heads/feet and watermarks = text repeated across pages in the margins
    (or identically at the same spot anywhere on the page)."""
    n = len(pages)
    if n == 0:
        return
    margin_cands = defaultdict(set)
    exact = defaultdict(set)
    for p in pages:
        if not p.lines:
            continue
        ys = sorted(p.lines, key=lambda l: l.y0)
        top_ids = {id(l) for l in ys[:3]}
        bot_ids = {id(l) for l in ys[-3:]}
        for l in p.lines:
            zone = None
            if l.y1 < p.height * 0.2 or id(l) in top_ids:
                zone = "top"
            elif l.y0 > p.height * 0.8 or id(l) in bot_ids:
                zone = "bot"
            if zone:
                margin_cands[(zone, _key(l.text))].add(p.number)
            exact[(l.text.strip(), round(l.x0 / 5), round(l.y0 / 5))].add(p.number)
    need = 2 if n <= 4 else max(3, int(0.2 * n)) if n < 40 else max(4, int(0.1 * n))
    rep = {k for k, v in margin_cands.items() if len(v) >= need and k[1]}
    # lines in the first/last two lines of >=2 pages, in body size or smaller (e.g. a
    # section running head like "Syllabus" that only appears on a couple of pages)
    edge = defaultdict(set)
    for p in pages:
        ys = sorted(p.lines, key=lambda l: l.y0)
        for zone, part in (("top", ys[:3]), ("bot", ys[-2:])):
            for l in part:
                if l.size <= bsize * 1.05 and len(l.text.strip()) < 80 and len(re.findall(r"[A-Za-z]", l.text)) >= 4:
                    edge[(zone, _key(l.text))].add(p.number)
    rep |= {k for k, v in edge.items() if len(v) >= 2 and k[1] and n >= 3}
    wm = {k for k, v in exact.items() if len(v) >= max(2, 0.5 * n) and n >= 2}
    removed = 0
    for p in pages:
        if not p.lines:
            continue
        ys = sorted(p.lines, key=lambda l: l.y0)
        top_ids = {id(l) for l in ys[:3]}
        bot_ids = {id(l) for l in ys[-3:]}
        for l in p.lines:
            in_top = l.y1 < p.height * 0.2 or id(l) in top_ids
            in_bot = l.y0 > p.height * 0.8 or id(l) in bot_ids
            k = _key(l.text)
            if (in_top and ("top", k) in rep) or (in_bot and ("bot", k) in rep) \
                    or (l.text.strip(), round(l.x0 / 5), round(l.y0 / 5)) in wm \
                    or ((in_top or in_bot) and PAGENUM.match(l.text) and (l.y1 < p.height * 0.12 or l.y0 > p.height * 0.85)) \
                    or TOC_LINE.search(l.text):
                l.role = "furniture"
                removed += 1
    stats["furniture_lines_removed"] = removed


def mark_figure_text(page, bsize: float, clusters=()) -> int:
    """Text that sits on top of raster images (labels, screenshots) or that looks like
    vector-figure debris (many tiny fragments in small type)."""
    n = 0
    big = [r for r in page.image_rects if 0.02 * page.width * page.height < r.width * r.height < 0.6 * page.width * page.height]
    # (a page-sized image under the text is a scan with an OCR layer, or a background: keep the text)
    for l in page.lines:
        if l.role != "body":
            continue
        cx, cy = l.cx, (l.y0 + l.y1) / 2
        if any(r.x0 - 2 <= cx <= r.x1 + 2 and r.y0 - 2 <= cy <= r.y1 + 2 for r in big) and not CAPTION.match(l.text):
            l.role = "figure"; n += 1
    # vector figures: short text fragments inside a cluster of vector drawings
    # (long lines inside a drawing cluster are kept: shaded call-out boxes, sidebars)
    area = page.width * page.height
    for r in clusters:
        if r.width * r.height < 0.03 * area:
            continue
        inside = [l for l in page.lines if l.role == "body" and r.x0 - 1 <= l.cx <= r.x1 + 1 and r.y0 - 1 <= (l.y0 + l.y1) / 2 <= r.y1 + 1]
        if not inside:
            continue
        short = [l for l in inside if len(l.text.strip()) < 45 and not CAPTION.match(l.text)
                 and not l.text.rstrip().endswith((".", "?", "!", ":")) and not l.text.lstrip()[:1] in "•▪◦●-–"]
        if len(short) >= 3 and len(short) >= 0.5 * len(inside):
            for l in short:
                l.role = "figure"; n += 1
    # vector-figure debris: blocks made of many short small-type fragments
    by_block = defaultdict(list)
    for l in page.lines:
        if l.role == "body":
            by_block[l.block].append(l)
    for blk in by_block.values():
        if len(blk) < 3:
            continue
        avg = sum(len(l.text.strip()) for l in blk) / len(blk)
        small = sum(l.size < bsize * 0.92 for l in blk) / len(blk)
        if avg < 12 and small > 0.6 and not any(CAPTION.match(l.text) for l in blk):
            for l in blk:
                l.role = "figure"; n += 1
    return n


def _gutters(lines, x_lo, x_hi):
    """Empty vertical bands between x_lo..x_hi, ignoring lines wider than 60% of the area."""
    W = x_hi - x_lo
    if W <= 0:
        return []
    occ = [0] * (int(W) + 2)
    used = 0
    for l in lines:
        if l.width > 0.6 * W:
            continue
        used += 1
        for x in range(max(0, int(l.x0 - x_lo)), min(len(occ), int(l.x1 - x_lo) + 1)):
            occ[x] += 1
    if used < 6:
        return []
    # a few centred lines (title, affiliation) may cross the gutter; they become
    # full-width separators in reading_order()
    tol = max(2, int(0.05 * used))
    gutters, start = [], None
    for x in range(int(0.15 * W), int(0.85 * W)):
        if occ[x] <= tol and start is None:
            start = x
        elif occ[x] > tol and start is not None:
            if x - start >= 7:
                gutters.append((x_lo + start, x_lo + x))
            start = None
    if start is not None and int(0.85 * W) - start >= 7:
        gutters.append((x_lo + start, x_lo + int(0.85 * W)))
    # each side must really carry text
    good = []
    for g0, g1 in gutters:
        left = sum(1 for l in lines if l.x1 <= g0 + 1 and 0.2 * W <= l.width <= 0.6 * W)
        right = sum(1 for l in lines if l.x0 >= g1 - 1 and 0.2 * W <= l.width <= 0.6 * W)
        if left >= 4 and right >= 4:
            good.append((g0, g1))
    return good


def _rows(lines) -> list:
    """Row-major order that tolerates jittery boxes (OCR layers, mixed font sizes):
    lines whose vertical extents overlap by >50% are one row, read left to right."""
    rows = []
    for l in sorted(lines, key=lambda l: (l.y0 + l.y1) / 2):
        if rows:
            r = rows[-1]
            top, bot = max(r["y0"], l.y0), min(r["y1"], l.y1)
            if bot - top > 0.5 * min(r["y1"] - r["y0"], l.y1 - l.y0):
                r["items"].append(l); r["y0"] = min(r["y0"], l.y0); r["y1"] = max(r["y1"], l.y1)
                continue
        rows.append({"y0": l.y0, "y1": l.y1, "items": [l]})
    return [l for r in rows for l in sorted(r["items"], key=lambda l: l.x0)]


def _order_cols(lines, gut, x_lo, x_hi, col_base=0) -> list:
    """Order lines given column gutters. Lines crossing a gutter are full-width
    separators; between separators, columns are read left to right."""
    if not gut:
        for l in lines:
            l.col = col_base
        return _rows(lines)
    bounds = [x_lo] + [g for g0, g1 in gut for g in (g0, g1)] + [x_hi]
    cols = [(bounds[i], bounds[i + 1]) for i in range(0, len(bounds), 2)]

    def col_of(l):
        # a line belongs to the column it starts in as long as it does not reach the
        # next column (ragged-right lines may poke into the gutter; MuPDF hid this by
        # counting trailing spaces into line boxes)
        for i, (c0, c1) in enumerate(cols):
            limit = cols[i + 1][0] if i + 1 < len(cols) else x_hi
            if l.x0 >= c0 - 3 and l.x0 < limit and l.x1 <= limit + 1:
                return col_base + i
        return -1  # spans a gutter

    ordered, band = [], []

    def flush():
        for ci in range(len(cols)):
            ordered.extend(_rows([l for l in band if l.col == col_base + ci]))
        band.clear()

    last = None
    for l in sorted(lines, key=lambda l: (l.y0, l.x0)):
        l.col = col_of(l)
        if l.col != -1 and last is not None and last.col == -1 and l.block == last.block \
                and 0 <= l.y0 - last.y1 < 0.8 * max(l.size, 1):
            l.col = -1          # short last line of a full-width caption/paragraph
        last = l
        if l.col == -1:
            flush()
            ordered.append(l)
        else:
            band.append(l)
    flush()
    return ordered


def reading_order(page, bsize: float = 10.0) -> list:
    """Return the page's flow lines in reading order.
    1. try one set of column gutters for the whole page (clean 1/2/3-column pages);
    2. otherwise cut the page into horizontal bands at full-width white space
       (XY-cut style) and look for gutters band by band (tables/figures above
       two-column text, mixed layouts)."""
    lines = [l for l in page.lines if l.role in ("body", "table")]
    if not lines:
        return []
    x_lo = min(l.x0 for l in lines); x_hi = max(l.x1 for l in lines)
    gut = _gutters(lines, x_lo, x_hi)
    if gut:
        return _order_cols(lines, gut, x_lo, x_hi)
    bands, cur, bottom = [], [], None
    for l in sorted(lines, key=lambda l: l.y0):
        if cur and l.y0 > bottom + 1.0 * bsize:
            bands.append(cur); cur = []
        cur.append(l)
        bottom = l.y1 if bottom is None or len(cur) == 1 else max(bottom, l.y1)
    if cur:
        bands.append(cur)
    out = []
    for i, band in enumerate(bands):
        g = _gutters(band, x_lo, x_hi) if len(band) >= 8 else []
        out.extend(_order_cols(band, g, x_lo, x_hi, col_base=10 * i if g else 0))
    return out


def is_note_start(l) -> bool:
    if l.spans and l.spans[0].sup and l.spans[0].text.strip():
        return True
    if len(l.spans) > 1 and re.match(r"^\s*(\d{1,3}|[*†‡§¶]{1,3})\s*$", l.spans[0].text) and l.spans[0].size < 0.9 * l.spans[1].size:
        return True
    return bool(NOTE_MARK.match(l.text))


def mark_footnotes(flow, page, bsize: float) -> None:
    """Footnotes: a run of smaller-than-body lines at the bottom of a column whose first
    line starts with a note marker."""
    by_col = defaultdict(list)
    for l in flow:
        by_col[l.col].append(l)
    for col_lines in by_col.values():
        if len(col_lines) < 2:
            continue
        # walk upward from the bottom while lines are small
        i = len(col_lines)
        while i > 0 and col_lines[i - 1].size < bsize * 0.93 and col_lines[i - 1].role == "body":
            i -= 1
        tail = col_lines[i:]
        if not tail or len(tail) > 0.6 * len(col_lines) or tail[-1].y1 < page.height * 0.5:
            continue
        # zone must start at a marker; drop leading lines until one does
        start = next((k for k, l in enumerate(tail) if is_note_start(l)), None)
        if start is None or start > 2:
            # continuation of a footnote from the previous column/page: small type hugging the bottom
            if tail[0].y0 > page.height * 0.75 and len(tail) <= 4:
                for l in tail:
                    l.role = "footnote"
            continue
        if start > 0 and tail[0].y0 > page.height * 0.7:
            start = 0
        for l in tail[start:]:
            l.role = "footnote"
