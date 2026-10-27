"""PDF -> pages of positioned text lines, using PDFium through pypdfium2.

PDFium's text page gives one record per character (in content-stream order) with its
Unicode value, a "loose" box (advance width x font ascent/descent), the baseline
origin, the text object it came from and flags for characters PDFium generated itself
(spaces, line breaks) and for line-end hyphens. This module groups those characters
into lines and style runs (spans) - what MuPDF's ``get_text("dict")`` returned for the
prototype - so the layout and structure heuristics run unchanged.
"""

from __future__ import annotations

import ctypes
import math
import re
import time
from ctypes import c_double, c_float, c_int, c_uint, c_ulong, c_void_p

import pypdfium2.raw as R

from . import ligfix, textnorm
from .model import Line, Page, Rect, Span

BOLD_NAME = re.compile(r"(bold|black|heavy|semibold|demibold|demi\b|-medi|cmbx|cmb\d|\bbd\b|-bd|extrabold|,bold)", re.I)
ITALIC_NAME = re.compile(r"(italic|oblique|-it\b|cmti|cmsl|-ital|,italic|-reguital)", re.I)
# names that say what the style is; only fonts without such a word ("F1", "T3") may
# fall back to an explicit descriptor weight
STYLE_NAME = re.compile(r"(regular|roman|book|medium|light|normal|plain|-regu|bold|italic|oblique|black|heavy|semibold|demi|thin)", re.I)

FLAG_ITALIC = 1 << 6
FLAG_FORCE_BOLD = 1 << 18

MARKER = re.compile(r"^\s*(?:[•◦▪●■□➢❖✓►▶‣⁃∙·*\-–—]|\(?[0-9]{1,3}(?:\.[0-9]{1,2}){0,3}[.)]?|\(?[a-zA-Z][.)]|\(?[ivxlcIVXLC]{1,5}[.)])\s*$")

# ----------------------------------------------------------------------------------------
# Direct ctypes prototypes: the char loop makes ~5 calls per character, and passing
# plain addresses instead of typed pointers is ~30% faster.
# ----------------------------------------------------------------------------------------


def _lib():
    try:
        import pypdfium2_raw.bindings as b

        return b._libs["pdfium"]
    except Exception:  # pragma: no cover - layout of a future pypdfium2
        return None


_L = _lib()


def _fn(name, res, *args):
    if _L is None:  # pragma: no cover
        return getattr(R, name)
    f = _L[name]
    f.restype = res
    f.argtypes = list(args)
    return f


GetUnicode = _fn("FPDFText_GetUnicode", c_uint, c_void_p, c_int)
GetLooseCharBox = _fn("FPDFText_GetLooseCharBox", c_int, c_void_p, c_int, c_void_p)
GetCharOrigin = _fn("FPDFText_GetCharOrigin", c_int, c_void_p, c_int, c_void_p, c_void_p)
GetTextObject = _fn("FPDFText_GetTextObject", c_void_p, c_void_p, c_int)
IsGenerated = _fn("FPDFText_IsGenerated", c_int, c_void_p, c_int)
IsHyphen = _fn("FPDFText_IsHyphen", c_int, c_void_p, c_int)
GetFontSize = _fn("FPDFText_GetFontSize", c_double, c_void_p, c_int)
GetMatrix = _fn("FPDFText_GetMatrix", c_int, c_void_p, c_int, c_void_p)
CountChars = _fn("FPDFText_CountChars", c_int, c_void_p)
TextObjGetFont = _fn("FPDFTextObj_GetFont", c_void_p, c_void_p)
FontGetBaseFontName = _fn("FPDFFont_GetBaseFontName", c_ulong, c_void_p, c_void_p, c_ulong)
FontGetFlags = _fn("FPDFFont_GetFlags", c_int, c_void_p)
FontGetWeight = _fn("FPDFFont_GetWeight", c_int, c_void_p)
FontGetItalicAngle = _fn("FPDFFont_GetItalicAngle", c_int, c_void_p, c_void_p)  # (font, int*)
PageCountObjects = _fn("FPDFPage_CountObjects", c_int, c_void_p)
PageGetObject = _fn("FPDFPage_GetObject", c_void_p, c_void_p, c_int)
FormCountObjects = _fn("FPDFFormObj_CountObjects", c_int, c_void_p)
FormGetObject = _fn("FPDFFormObj_GetObject", c_void_p, c_void_p, c_ulong)
ObjGetType = _fn("FPDFPageObj_GetType", c_int, c_void_p)
ObjGetBounds = _fn("FPDFPageObj_GetBounds", c_int, c_void_p, c_void_p, c_void_p, c_void_p, c_void_p)
ObjGetMatrix = _fn("FPDFPageObj_GetMatrix", c_int, c_void_p, c_void_p)
PageGetBoundingBox = _fn("FPDF_GetPageBoundingBox", c_int, c_void_p, c_void_p)


def _addr(obj) -> int:
    raw = getattr(obj, "raw", obj)
    return ctypes.cast(raw, c_void_p).value or 0


# ----------------------------------------------------------------------------------------
# Page geometry: PDF user space -> display space (top-left origin, rotation applied)
# ----------------------------------------------------------------------------------------


class Geom:
    def __init__(self, page):
        box = R.FS_RECTF()
        if PageGetBoundingBox(_addr(page), ctypes.addressof(box)):
            x0, y0, x1, y1 = box.left, box.bottom, box.right, box.top
        else:  # pragma: no cover
            x0, y0, x1, y1 = page.get_cropbox()
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        self.cx0, self.cy0, self.cx1, self.cy1 = x0, y0, x1, y1
        self.rot = (page.get_rotation() or 0) % 360
        w, h = x1 - x0, y1 - y0
        self.width, self.height = (h, w) if self.rot in (90, 270) else (w, h)

    def pt(self, x: float, y: float) -> tuple[float, float]:
        r = self.rot
        if r == 0:
            return x - self.cx0, self.cy1 - y
        if r == 90:
            return y - self.cy0, x - self.cx0
        if r == 180:
            return self.cx1 - x, y - self.cy0
        return self.cy1 - y, self.cx1 - x

    def rect(self, x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        ax, ay = self.pt(x0, y0)
        bx, by = self.pt(x1, y1)
        return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)

    def direction(self, a: float, b: float) -> tuple[float, float]:
        """Text direction (matrix a, b in PDF space) as a display-space vector."""
        r = self.rot
        if r == 0:
            return a, -b
        if r == 90:
            return b, a
        if r == 180:
            return -a, b
        return -b, -a


# ----------------------------------------------------------------------------------------
# Fonts
# ----------------------------------------------------------------------------------------


class _Font:
    __slots__ = ("name", "bold", "italic")

    def __init__(self, name: str, bold: bool, italic: bool):
        self.name, self.bold, self.italic = name, bold, italic


def _font_info(font: int, cache: dict) -> _Font:
    f = cache.get(font)
    if f is not None:
        return f
    name = ""
    flags = weight = 0
    angle = c_int(0)
    if font:
        n = FontGetBaseFontName(font, None, 0)
        if n:
            buf = ctypes.create_string_buffer(n)
            FontGetBaseFontName(font, ctypes.addressof(buf), n)
            name = buf.value.decode("utf-8", "replace")
        flags = FontGetFlags(font)
        weight = FontGetWeight(font)
        FontGetItalicAngle(font, ctypes.addressof(angle))
    name = name.split("+", 1)[1] if re.match(r"^[A-Z]{6}\+", name) else name
    # PDFium's font weight is derived from the descriptor's StemV when /FontWeight is
    # missing, and producers fill StemV carelessly (the Supreme Court's regular Century
    # Expanded reports 740), so only names and the ForceBold flag count. Explicit
    # /FontWeight values of 700+ on fonts whose name has no style word are trusted.
    bold = bool(flags & FLAG_FORCE_BOLD) or bool(BOLD_NAME.search(name))
    if not bold and weight >= 700 and not STYLE_NAME.search(name) and not re.search(r"[a-z]", name):
        bold = True  # "F2", "T1_0" style names: nothing better to go on
    italic = bool(flags & FLAG_ITALIC) or bool(ITALIC_NAME.search(name)) or angle.value < -4
    f = _Font(name, bold, italic)
    cache[font] = f
    return f


# ----------------------------------------------------------------------------------------
# Characters -> lines
# ----------------------------------------------------------------------------------------

# char record: (text, x0, y0, x1, y1, baseline, size, font, ox, fkey)
_T, _X0, _Y0, _X1, _Y1, _B, _S, _F, _OX, _FK = range(10)


class _LineAcc:
    __slots__ = ("chars", "base", "lsize", "x1", "last_ox", "last_base", "nonspace")

    def __init__(self, c):
        self.chars = [c]
        self.base = c[_B]
        self.lsize = c[_S]
        self.x1 = c[_X1]
        self.last_ox = c[_OX]
        self.last_base = c[_B]
        self.nonspace = 1

    def dot_leader(self) -> bool:
        """The line so far ends in a dot leader ("Chapter . . . . ." before a page number)."""
        tail = [c[_T] for c in self.chars[-12:] if c[_T] != " "]
        return len(tail) >= 4 and all(t in ".…·" for t in tail[-4:])

    def is_marker(self) -> bool:
        """Short line start that may be followed by a wide gap: "1", "2.3", "•", "(a)",
        or symbols only ("* *" of a spaced-out "* * *")."""
        if self.nonspace > 6:
            return False
        t = "".join(c[_T] for c in self.chars)
        return bool(MARKER.match(t)) or not any(ch.isalnum() for ch in t)

    def continues(self, c) -> bool:
        size = c[_S]
        ls = self.lsize
        # several characters from one glyph (correctly mapped ligature): same origin
        if abs(c[_OX] - self.last_ox) < 0.01 and abs(c[_B] - self.last_base) < 0.01:
            return True
        ref = max(size, ls, 0.1)
        gap = c[_X0] - self.x1
        if gap < -0.5 * ref:
            return False
        if gap > 1.2 * ref and not (gap <= 4.0 * ref and self.is_marker()) \
                and not (gap <= 10.0 * ref and self.dot_leader()):
            return False
        dy = c[_B] - self.base
        if abs(dy) <= 0.3 * ref:
            return True
        if size < 0.85 * ls and -0.6 * ls <= dy <= 0.45 * ls:
            return True  # super-/subscript inside the line
        if ls < 0.85 * size and 0 < dy <= 0.6 * size and self.nonspace <= 4:
            # the line so far is a raised marker (footnote number): rebase on this char
            self.base, self.lsize = c[_B], size
            return True
        return False

    def add(self, c, space: bool) -> None:
        if space:
            p = self.chars[-1]
            self.chars.append((" ", p[_X1], p[_Y0], p[_X1], p[_Y1], p[_B], p[_S], p[_F], p[_OX], None))
        self.chars.append(c)
        if c[_S] > self.lsize and abs(c[_B] - self.base) <= 0.3 * c[_S]:
            self.lsize = c[_S]
        self.x1 = max(self.x1, c[_X1])
        self.last_ox = c[_OX]
        self.last_base = c[_B]
        self.nonspace += 1


def _build_line(acc: _LineAcc, pno: int, lig: ligfix.Collector | None) -> Line | None:
    """``lig`` collects ligature statistics (first pages only)."""
    chars = acc.chars
    while chars and chars[-1][_T] == " ":
        chars.pop()
    if not chars:
        return None
    ls = acc.lsize
    base = acc.base
    spans: list[Span] = []
    cur_key = None
    buf: list[str] = []
    fix: list = []
    bases: list[float] = []
    cur_font = None
    cur_size = 0.0

    def flush():
        if not buf:
            return
        spans.append(Span(text="".join(buf), size=cur_size, bold=cur_font.bold, italic=cur_font.italic,
                          sup=False, font=cur_font.name, oy=sum(bases) / len(bases), fix=fix[:] or None))

    for c in chars:
        f = c[_F]
        if c[_T] == " " and buf:
            buf.append(" ")
            continue
        dy = c[_B] - base
        cls = 1 if (c[_S] < 0.9 * ls and dy < -0.15 * ls) else (-1 if (c[_S] < 0.9 * ls and dy > 0.15 * ls) else 0)
        key = (id(f), round(c[_S], 1), cls)
        if key != cur_key:
            flush()
            buf, fix, bases = [], [], []
            cur_key, cur_font, cur_size = key, f, c[_S]
        if c[_FK] is not None:
            fix.append((len(buf), c[_FK]))
        buf.append(c[_T])
        bases.append(c[_B])
    flush()
    x0 = min(c[_X0] for c in chars)
    x1 = max(c[_X1] for c in chars)
    y0 = min(c[_Y0] for c in chars)
    y1 = max(c[_Y1] for c in chars)
    if lig is not None:
        text = "".join(c[_T] for c in chars)
        lig.add_text(text)
        for k, c in enumerate(chars):
            if c[_FK] is None:
                continue
            a = k
            while a > 0 and text[a - 1].isalpha():
                a -= 1
            b = k + 1
            while b < len(text) and text[b].isalpha():
                b += 1
            lig.add(c[_FK], text[a:k], text[k + 1:b])
    return Line(pno, x0, y0, x1, y1, spans, 0)


def _assign_blocks(lines: list) -> None:
    """MuPDF-style text blocks: consecutive (stream-order) lines that touch vertically
    and overlap horizontally, or continue the same visual line."""
    blk = 0
    prev = None
    for ln in lines:
        if prev is not None:
            h = max(prev.y1 - prev.y0, 1.0)
            sz = max(prev.size, ln.size, 1.0)
            gap_v = ln.y0 - prev.y1
            overlap = ln.x0 < prev.x1 + sz and ln.x1 > prev.x0 - sz
            same_row = abs(ln.y0 - prev.y0) < 0.3 * h and 0 <= ln.x0 - prev.x1 < 6 * sz
            if not ((overlap and -0.3 * h < gap_v < 0.75 * h) or same_row):
                blk += 1
        ln.block = blk
        prev = ln


def page_lines(page, pno: int, geom: Geom, lig: ligfix.Collector | None, stats: dict,
               collect: bool = True) -> tuple[list, int, int]:
    """Return (lines, total_chars, bad_chars) for one page. With ``lig``, single 'f'
    glyphs get a ligature-detection key; ``collect`` also feeds their statistics."""
    tp = page.get_textpage()
    try:
        return _page_lines(_addr(tp), pno, geom, lig, stats, collect)
    finally:
        tp.close()


def _page_lines(tp: int, pno: int, geom: Geom, lig, stats, collect: bool = True) -> tuple[list, int, int]:
    n = CountChars(tp)
    if n <= 0:
        return [], 0, 0
    rect = R.FS_RECTF()
    prect = ctypes.addressof(rect)
    ox, oy = c_double(), c_double()
    pox, poy = ctypes.addressof(ox), ctypes.addressof(oy)
    mat = R.FS_MATRIX()
    pmat = ctypes.addressof(mat)
    fonts: dict = {}  # per page: PDFium may free a font when its last page closes
    W, H = geom.width, geom.height
    rot0 = geom.rot == 0
    cx0, cy1 = geom.cx0, geom.cy1

    out: list = []
    acc: _LineAcc | None = None
    pend_space = pend_break = False
    total = bad = 0
    cur_obj = -1
    font = None
    size = 0.0
    upright = True
    prev_f_same = False
    n_chars = n_objs = 0
    for i in range(n):
        u = GetUnicode(tp, i)
        if u == 0x20 or u == 0xA0 or u == 0x3000:
            pend_space = True
            continue
        if u == 0x0D or u == 0x0A:
            if IsGenerated(tp, i):
                pend_break = True
            else:
                total += 1
                bad += 1
            continue
        hyph = False
        if u == 2 and IsHyphen(tp, i):
            ch = "-"
            hyph = True
        elif u < 0x20:
            if u == 9:
                pend_space = True
            else:  # raw char code from a font without a Unicode map
                total += 1
                bad += 1
            continue
        else:
            ch = chr(u) if u < 0x110000 else "�"
        total += 1
        if u == 0xFFFD or 0xE000 <= u <= 0xF8FF:
            bad += 1
        obj = GetTextObject(tp, i) or 0
        n_chars += 1
        if obj != cur_obj:
            cur_obj = obj
            n_objs += 1
            fs = GetFontSize(tp, i)
            GetMatrix(tp, i, pmat)
            a, b, c, d = mat.a, mat.b, mat.c, mat.d
            det = abs(a * d - b * c)
            size = fs * math.sqrt(det) if det > 0 else fs
            dx, dy = geom.direction(a, b)
            norm = math.hypot(dx, dy) or 1.0
            upright = dx > 0 and abs(dy) / norm <= 0.05
            font = _font_info(TextObjGetFont(obj) if obj else 0, fonts)
        if not upright or size <= 0.5:
            continue
        if hyph and n_chars >= 3 * n_objs and _lone_object(tp, i, n, obj):
            # a line-end hyphen drawn by its own show operation, inside text that is
            # otherwise set in longer runs, is a typesetter's discretionary (soft)
            # hyphen; PDFium reports soft and hard line-end hyphens alike
            ch = "\u00ad"
        GetLooseCharBox(tp, i, prect)
        GetCharOrigin(tp, i, pox, poy)
        if rot0:
            x0, x1 = rect.left - cx0, rect.right - cx0
            y0, y1 = cy1 - rect.top, cy1 - rect.bottom
            bx, by = ox.value - cx0, cy1 - oy.value
        else:
            x0, y0, x1, y1 = geom.rect(rect.left, rect.bottom, rect.right, rect.top)
            bx, by = geom.pt(ox.value, oy.value)
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        if mx < -2 or mx > W + 2 or my < -2 or my > H + 2:
            continue  # outside the visible page (TEXT_MEDIABOX_CLIP)
        fkey = None
        if ch == "f" and lig is not None:
            # a correctly mapped ligature yields several chars at one origin; skip those
            same_next = i + 1 < n and _same_origin(tp, i + 1, ox.value, oy.value)
            if not same_next and not prev_f_same:
                fkey = ligfix.width_key(font.name, x1 - x0, size)
            prev_f_same = same_next
        else:
            prev_f_same = False
        rec = (ch, x0, y0, x1, y1, by, size, font, bx, fkey)
        if acc is not None and acc.continues(rec):
            gap = x0 - acc.x1
            space = pend_space or (pend_break and gap > 0.1 * size) or gap > 0.3 * size
            if acc.chars[-1][_T] in ("-", "\u00ad") and pend_break and gap < 0.3 * size:
                space = False
            acc.add(rec, space and not (abs(bx - acc.last_ox) < 0.01))
        else:
            if acc is not None:
                ln = _build_line(acc, pno, lig if collect else None)
                if ln is not None:
                    out.append(ln)
            acc = _LineAcc(rec)
        pend_space = pend_break = False
    if acc is not None:
        ln = _build_line(acc, pno, lig if collect else None)
        if ln is not None:
            out.append(ln)
    return out, total, bad


_ox2, _oy2 = c_double(), c_double()


def _lone_object(tp: int, i: int, n: int, obj: int) -> bool:
    """Is char ``i`` the only real character of its text object?"""
    for j in (i - 1, i + 1):
        while 0 <= j < n and IsGenerated(tp, j):
            j += -1 if j < i else 1
        if 0 <= j < n and (GetTextObject(tp, j) or 0) == obj:
            return False
    return bool(obj)


def _same_origin(tp: int, i: int, x: float, y: float) -> bool:
    GetCharOrigin(tp, i, ctypes.addressof(_ox2), ctypes.addressof(_oy2))
    return abs(_ox2.value - x) < 0.01 and abs(_oy2.value - y) < 0.01


# ----------------------------------------------------------------------------------------
# Images and vector drawings
# ----------------------------------------------------------------------------------------


def _mul(m1, m2):
    """m1 then m2 (PDF row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def page_objects(page, geom: Geom, max_paths: int = 4000) -> tuple[list, list]:
    """(image rects, vector path rects) in display space."""
    images: list = []
    paths: list = []
    l, b, r, t = c_float(), c_float(), c_float(), c_float()
    pl, pb, pr, pt = (ctypes.addressof(v) for v in (l, b, r, t))
    mat = R.FS_MATRIX()
    pmat = ctypes.addressof(mat)
    W, H = geom.width, geom.height
    budget = [20000]

    def visit(count, get, parent, m, depth):
        for i in range(count(parent)):
            if budget[0] <= 0:
                return
            budget[0] -= 1
            obj = get(parent, i)
            if not obj:
                continue
            typ = ObjGetType(obj)
            if typ == R.FPDF_PAGEOBJ_FORM:
                if depth < 5 and ObjGetMatrix(obj, pmat):
                    fm = (mat.a, mat.b, mat.c, mat.d, mat.e, mat.f)
                    visit(FormCountObjects, FormGetObject, obj, _mul(fm, m) if m else fm, depth + 1)
                continue
            if typ not in (R.FPDF_PAGEOBJ_IMAGE, R.FPDF_PAGEOBJ_PATH, R.FPDF_PAGEOBJ_SHADING):
                continue
            if typ != R.FPDF_PAGEOBJ_IMAGE and len(paths) >= max_paths:
                continue
            if not ObjGetBounds(obj, pl, pb, pr, pt):
                continue
            x0, y0, x1, y1 = l.value, b.value, r.value, t.value
            if m is not None:
                pts = [_apply(m, x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
                x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
                y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
            dx0, dy0, dx1, dy1 = geom.rect(x0, y0, x1, y1)
            dx0, dy0, dx1, dy1 = max(dx0, 0), max(dy0, 0), min(dx1, W), min(dy1, H)
            if dx1 <= dx0 or dy1 <= dy0:
                if typ == R.FPDF_PAGEOBJ_PATH and (dx1 >= dx0 and dy1 >= dy0):
                    paths.append(Rect(dx0, dy0, dx1, dy1))  # hairline rule
                continue
            rc = Rect(dx0, dy0, dx1, dy1)
            if typ == R.FPDF_PAGEOBJ_IMAGE:
                if rc.width > 4 and rc.height > 4:
                    images.append(rc)
            else:
                paths.append(rc)

    visit(PageCountObjects, PageGetObject, _addr(page), None, 0)
    return images, paths


def cluster_drawings(paths: list, W: float, H: float, tol: float = 3.0) -> list:
    """Group vector paths that touch (within ``tol``) into figure regions, like
    PyMuPDF's ``Page.cluster_drawings``. Page-sized backgrounds are ignored."""
    area = W * H
    rects = [Rect(p.x0, p.y0, p.x1, p.y1) for p in paths if p.area < 0.6 * area]
    if not rects:
        return []
    rects.sort(key=lambda r: (r.y0, r.x0))
    clusters: list = []
    for r in rects:
        merged = None
        for c in clusters:
            if r.x0 <= c.x1 + tol and r.x1 >= c.x0 - tol and r.y0 <= c.y1 + tol and r.y1 >= c.y0 - tol:
                c.x0, c.y0, c.x1, c.y1 = min(c.x0, r.x0), min(c.y0, r.y0), max(c.x1, r.x1), max(c.y1, r.y1)
                merged = c
                break
        if merged is None:
            clusters.append(Rect(r.x0, r.y0, r.x1, r.y1))
    # merge clusters that grew into each other
    changed = True
    while changed and len(clusters) > 1:
        changed = False
        out: list = []
        for c in clusters:
            for o in out:
                if c.x0 <= o.x1 + tol and c.x1 >= o.x0 - tol and c.y0 <= o.y1 + tol and c.y1 >= o.y0 - tol:
                    o.x0, o.y0, o.x1, o.y1 = min(c.x0, o.x0), min(c.y0, o.y0), max(c.x1, o.x1), max(c.y1, o.y1)
                    changed = True
                    break
            else:
                out.append(c)
        clusters = out
    return [c for c in clusters if c.width > tol and c.height > tol]


def _union_cover(rects, W: float, H: float) -> float:
    """Rough share of the page covered by images (sampling grid, avoids polygon maths)."""
    if not rects or W <= 0 or H <= 0:
        return 0.0
    n, hit = 24, 0
    for i in range(n):
        for j in range(n):
            x, y = (i + 0.5) * W / n, (j + 0.5) * H / n
            if any(r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1 for r in rects):
                hit += 1
    return hit / (n * n)


# ----------------------------------------------------------------------------------------
# Post-processing shared with the OCR path
# ----------------------------------------------------------------------------------------

SUPTEXT = re.compile(r"^[\d*†‡§¶,\-–]{1,7}$")


def mark_superscripts(spans) -> None:
    """Treat small, raised digit/symbol spans as superscripts (footnote markers like
    'library.1')."""
    main = max(spans, key=lambda s: len(s.text.strip()))
    for s in spans:
        if s.sup or s is main:
            continue
        if SUPTEXT.match(s.text.strip()) and s.size < 0.85 * main.size and s.oy < main.oy - 0.15 * main.size:
            s.sup = True


def _apply_fixes(ln: Line, lig: ligfix.Collector) -> None:
    """Replace broken ligature glyphs ('f' with a ligature's advance) in place."""
    if not any(s.fix for s in ln.spans):
        return
    text = "".join(s.text for s in ln.spans)
    starts, off = [], 0
    for s in ln.spans:
        starts.append(off)
        off += len(s.text)
    for s, start in zip(ln.spans, starts):
        if not s.fix:
            continue
        t = s.text
        for pos, key in sorted(s.fix, reverse=True):
            if t[pos:pos + 1] != "f":
                continue
            k = start + pos
            a = k
            while a > 0 and text[a - 1].isalpha():
                a -= 1
            b = k + 1
            while b < len(text) and text[b].isalpha():
                b += 1
            exp = lig.choose(key, text[a:k], text[k + 1:b])
            if exp:
                t = t[:pos] + exp + t[pos + 1:]
        s.text = t


def finish_lines(lines: list, lig: ligfix.Collector | None = None) -> list:
    """Apply ligature repairs, clean span text, drop empty spans/lines, mark
    superscripts."""
    out = []
    for ln in lines:
        if lig is not None and lig.decided:
            _apply_fixes(ln, lig)
        spans = []
        for s in ln.spans:
            s.fix = None
            s.text = textnorm.clean(s.text)
            if s.text:
                spans.append(s)
        if not spans or not "".join(s.text for s in spans).strip():
            continue
        if len(spans) > 1:
            mark_superscripts(spans)
        ln.spans = spans
        out.append(ln)
    _assign_blocks(out)
    return out


# ----------------------------------------------------------------------------------------
# Document
# ----------------------------------------------------------------------------------------


def extract(doc, *, max_pages: int, stats: dict, log: list, deadline: float | None = None) -> list:
    """Extract every page (up to ``max_pages``) into :class:`Page` records. With a
    ``deadline`` (time.monotonic), stops early so the rest of the pipeline still fits:
    the layout and structure passes take about a third of the extraction time."""
    lig = ligfix.Collector()
    pages = []
    n = min(len(doc), max_pages)
    started = time.monotonic()
    for pno in range(n):
        if deadline is not None and pno and time.monotonic() + 0.4 * (time.monotonic() - started) + 1.0 > deadline:
            stats["stopped_for_time"] = True
            break
        page = doc[pno]
        try:
            geom = Geom(page)
            pg = Page(pno, geom.width, geom.height)
            try:
                lines, total, bad = page_lines(page, pno, geom, lig, stats, collect=pno < lig.max_pages)
            except Exception as e:  # one broken page must not lose the document
                log.append(f"page {pno + 1}: text extraction failed: {type(e).__name__}")
                lines, total, bad = [], 0, 0
            try:
                images, paths = page_objects(page, geom)
            except Exception as e:
                log.append(f"page {pno + 1}: object scan failed: {type(e).__name__}")
                images, paths = [], []
            pg.lines = lines
            pg.image_rects = images
            pg.image_cover = _union_cover(images, geom.width, geom.height)
            pg.drawing_clusters = cluster_drawings(paths, geom.width, geom.height)
            pg.garbage = bad / total if total else 0.0
            pages.append(pg)
        finally:
            page.close()
    gidmap = lig.decide()
    if gidmap:
        stats["ligature_repair"] = sorted({c for _, cands in gidmap.values() for c in cands})
    for pg in pages:
        pg.lines = finish_lines(pg.lines, lig)
        pg.chars = len("".join(ln.text for ln in pg.lines).replace(" ", ""))
    _sanity_bold(pages)
    return pages


def _sanity_bold(pages) -> None:
    """If most of the text claims to be bold, the flag carries no information (a font
    with a misleading weight): clear it so short lines do not all become headings."""
    total = bold = 0
    for pg in pages:
        for ln in pg.lines:
            for s in ln.spans:
                n = len(s.text.strip())
                total += n
                bold += n if s.bold else 0
    if total and bold > 0.6 * total:
        for pg in pages:
            for ln in pg.lines:
                for s in ln.spans:
                    s.bold = False
