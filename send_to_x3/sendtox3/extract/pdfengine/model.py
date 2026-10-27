"""Positioned-text model shared by the extraction backend and the layout heuristics.

Coordinates are PDF points in *display* space: origin at the top-left corner of the
visible page (crop box), y growing downwards, page rotation already applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    def contains_point(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


@dataclass
class Span:
    text: str
    size: float
    bold: bool
    italic: bool
    sup: bool
    font: str
    oy: float = 0.0  # baseline (origin y)
    # positions of single "f" glyphs in ``text`` with their ligature-detection key,
    # used by the broken-ligature repair before the text is cleaned
    fix: list | None = None


@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    spans: list
    block: int
    role: str = "body"  # body | furniture | figure | footnote | table
    col: int = 0
    payload: object = None

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def size(self) -> float:  # char-weighted dominant size, ignoring superscripts
        w: dict = {}
        for s in self.spans:
            if s.sup or not s.text.strip():
                continue
            k = round(s.size * 2) / 2
            w[k] = w.get(k, 0) + len(s.text.strip())
        if not w:
            return round(max((s.size for s in self.spans), default=0) * 2) / 2
        return max(w.items(), key=lambda kv: kv[1])[0]

    @property
    def bold(self) -> bool:
        chars = [s for s in self.spans if s.text.strip() and not s.sup]
        n = sum(len(s.text.strip()) for s in chars)
        b = sum(len(s.text.strip()) for s in chars if s.bold)
        return n > 0 and b / n >= 0.8

    @property
    def italic(self) -> bool:
        chars = [s for s in self.spans if s.text.strip() and not s.sup]
        n = sum(len(s.text.strip()) for s in chars)
        i = sum(len(s.text.strip()) for s in chars if s.italic)
        return n > 0 and i / n >= 0.8

    @property
    def font(self) -> str:
        return max(self.spans, key=lambda s: len(s.text)).font if self.spans else ""

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Page:
    number: int
    width: float
    height: float
    lines: list = field(default_factory=list)
    image_rects: list = field(default_factory=list)
    image_cover: float = 0.0
    drawing_clusters: list = field(default_factory=list)
    chars: int = 0
    garbage: float = 0.0
    ocr: bool = False
    ocr_failed: str = ""

    @property
    def needs_ocr(self) -> bool:
        return (self.chars < 25 and self.image_cover > 0.3) or self.garbage > 0.3
