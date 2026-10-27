"""Generate send_to_x3/icon.png (128x128) and send_to_x3/logo.png (250x100).

The artwork is drawn from primitives (an e-reader with a text page and an arrow
going into it), so there is nothing to license. The wordmark uses Aileron, the CC0
font bundled with Pillow (``ImageFont.load_default``). Both images sit on their
own coloured tile so they read on light and dark Home Assistant themes.

Needs Pillow >= 10.1 (not a project dependency; use a scratch venv)::

    python3 -m venv /tmp/img && /tmp/img/bin/pip install pillow
    /tmp/img/bin/python e2e/ha/make_icons.py            # writes into send_to_x3/
    /tmp/img/bin/python e2e/ha/make_icons.py --out DIR  # writes elsewhere
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SCALE = 4  # draw at 4x and downsample for clean anti-aliased edges

TILE = (31, 122, 109, 255)  # deep teal: contrasts with white and with near-black
TILE_EDGE = (22, 92, 82, 255)
BODY = (30, 35, 40, 255)  # device bezel
PAPER = (243, 239, 228, 255)  # e-paper screen
INK = (138, 144, 150, 255)  # text lines on the screen
INK_DARK = (70, 76, 82, 255)  # title line
ARROW = (245, 178, 45, 255)  # warm amber
WHITE = (255, 255, 255, 255)
CLEAR = (0, 0, 0, 0)

ROOT = Path(__file__).resolve().parents[2]


def draw_glyph(d: ImageDraw.ImageDraw, ox: float, oy: float, size: float) -> None:
    """Draw the e-reader + arrow glyph into a ``size`` x ``size`` box at (ox, oy).

    Coordinates below are in a 512-unit design grid.
    """
    s = size / 512.0

    def pt(x: float, y: float) -> tuple[int, int]:
        return (round(ox + x * s), round(oy + y * s))

    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[int, int, int, int]:
        return (*pt(x0, y0), *pt(x1, y1))

    def r(v: float) -> int:
        return max(1, round(v * s))

    # Device body, screen, page-turn bar.
    d.rounded_rectangle(box(190, 70, 430, 442), radius=r(34), fill=BODY)
    d.rounded_rectangle(box(214, 96, 406, 384), radius=r(10), fill=PAPER)
    d.rounded_rectangle(box(278, 404, 342, 418), radius=r(7), fill=(74, 82, 90, 255))

    # Text on the page: a title line, then body lines of varying length.
    d.rounded_rectangle(box(236, 124, 360, 140), radius=r(8), fill=INK_DARK)
    y = 168.0
    for length in (148, 132, 148, 104, 148, 140, 118, 148):
        d.rounded_rectangle(box(236, y, 236 + length, y + 10), radius=r(5), fill=INK)
        y += 26.0

    # Arrow pointing into the screen, with a tile-coloured halo so it separates
    # from the bezel it overlaps.
    def arrow(grow: float) -> list[tuple[int, int]]:
        return [
            pt(64 - grow, 226 - grow),
            pt(236, 226 - grow),
            pt(236, 168 - grow * 1.6),
            pt(332 + grow * 1.6, 256),
            pt(236, 344 + grow * 1.6),
            pt(236, 286 + grow),
            pt(64 - grow, 286 + grow),
        ]

    d.polygon(arrow(14), fill=TILE)
    d.polygon(arrow(0), fill=ARROW)


def make_icon(px: int = 128) -> Image.Image:
    big = px * SCALE
    img = Image.new("RGBA", (big, big), CLEAR)
    d = ImageDraw.Draw(img)
    pad = round(big * 0.03)
    d.rounded_rectangle((pad, pad, big - pad, big - pad), radius=round(big * 0.2), fill=TILE_EDGE)
    edge = round(big * 0.012)
    d.rounded_rectangle(
        (pad + edge, pad + edge, big - pad - edge, big - pad - edge), radius=round(big * 0.19), fill=TILE
    )
    glyph = big * 0.86
    draw_glyph(d, (big - glyph) / 2, (big - glyph) / 2, glyph)
    return img.resize((px, px), Image.Resampling.LANCZOS)


def make_logo(width: int = 250, height: int = 100) -> Image.Image:
    w, h = width * SCALE, height * SCALE
    img = Image.new("RGBA", (w, h), CLEAR)
    d = ImageDraw.Draw(img)
    pad = round(h * 0.04)
    d.rounded_rectangle((pad, pad, w - pad, h - pad), radius=round(h * 0.2), fill=TILE_EDGE)
    edge = round(h * 0.012)
    d.rounded_rectangle((pad + edge, pad + edge, w - pad - edge, h - pad - edge), radius=round(h * 0.19), fill=TILE)

    glyph = h * 0.80
    gx = pad + h * 0.05
    draw_glyph(d, gx, (h - glyph) / 2, glyph)

    font = ImageFont.load_default(size=round(h * 0.27))
    small = ImageFont.load_default(size=round(h * 0.13))
    tx = round(gx + glyph + h * 0.04)
    title = "Send to X3"
    sub = "for CrossPoint e-readers"
    t_box = d.textbbox((0, 0), title, font=font, stroke_width=round(h * 0.008))
    s_box = d.textbbox((0, 0), sub, font=small)
    gap = round(h * 0.06)
    block = (t_box[3] - t_box[1]) + gap + (s_box[3] - s_box[1])
    ty = (h - block) / 2 - t_box[1]
    d.text((tx, ty), title, font=font, fill=WHITE, stroke_width=round(h * 0.008), stroke_fill=WHITE)
    sy = ty + t_box[3] + gap - s_box[1]
    d.text((tx, sy), sub, font=small, fill=(220, 238, 234, 255))
    return img.resize((width, height), Image.Resampling.LANCZOS)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "send_to_x3")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    make_icon().save(args.out / "icon.png", optimize=True)
    make_logo().save(args.out / "logo.png", optimize=True)
    print(f"wrote {args.out / 'icon.png'} and {args.out / 'logo.png'}")


if __name__ == "__main__":
    main()
