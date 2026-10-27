"""Screenshot helpers without third-party dependencies.

The simulator saves SDL surfaces as uncompressed BMP (24 or 32 bpp, BI_RGB or
BI_BITFIELDS, usually bottom-up). These helpers decode that subset, write PNGs for
humans, and compute OCR-free metrics the scenarios assert on: how much of the screen
is inked, and how different two screens are.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Gray:
    """An 8-bit grayscale image, row-major, top row first."""

    width: int
    height: int
    pixels: bytes

    def ink_ratio(self, threshold: int = 128) -> float:
        """Share of pixels darker than ``threshold`` (0.0 blank .. 1.0 black)."""
        if not self.pixels:
            return 0.0
        table = bytes(1 if v < threshold else 0 for v in range(256))
        return sum(self.pixels.translate(table)) / len(self.pixels)

    def region(self, x0: int, y0: int, x1: int, y1: int) -> "Gray":
        x0, x1 = max(0, x0), min(self.width, x1)
        y0, y1 = max(0, y0), min(self.height, y1)
        rows = [self.pixels[y * self.width + x0:y * self.width + x1] for y in range(y0, y1)]
        return Gray(max(0, x1 - x0), max(0, y1 - y0), b"".join(rows))


def read_bmp(path: Path | str) -> Gray:
    """Decode an uncompressed 24/32-bit BMP into grayscale (ITU-R 601 luma)."""
    data = Path(path).read_bytes()
    if data[:2] != b"BM" or len(data) < 54:
        raise ValueError(f"{path}: not a BMP file")
    offset = struct.unpack_from("<I", data, 10)[0]
    header_size = struct.unpack_from("<I", data, 14)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    if bpp not in (24, 32) or compression not in (0, 3):
        raise ValueError(f"{path}: unsupported BMP ({bpp} bpp, compression {compression})")
    # Channel byte positions. BI_RGB is little-endian BGR(A); BI_BITFIELDS gives masks.
    r_i, g_i, b_i = 2, 1, 0
    if compression == 3:
        if header_size >= 52:
            masks = struct.unpack_from("<III", data, 54)
        else:  # BITMAPINFOHEADER followed by three DWORD masks
            masks = struct.unpack_from("<III", data, 14 + header_size)
        r_i, g_i, b_i = (_mask_byte(m) for m in masks)
    top_down = height < 0
    height = abs(height)
    step = bpp // 8
    stride = (width * step + 3) & ~3
    out = bytearray(width * height)
    for y in range(height):
        src_y = y if top_down else height - 1 - y
        row = data[offset + src_y * stride: offset + src_y * stride + width * step]
        if len(row) < width * step:
            raise ValueError(f"{path}: truncated pixel data")
        r = row[r_i::step]
        g = row[g_i::step]
        b = row[b_i::step]
        base = y * width
        for x in range(width):
            out[base + x] = (299 * r[x] + 587 * g[x] + 114 * b[x]) // 1000
    return Gray(width, height, bytes(out))


def _mask_byte(mask: int) -> int:
    for i in range(4):
        if mask == 0xFF << (8 * i):
            return i
    raise ValueError(f"unsupported BMP channel mask {mask:#x}")


def write_png(img: Gray, path: Path | str) -> Path:
    """Write ``img`` as an 8-bit grayscale PNG."""
    raw = b"".join(b"\x00" + img.pixels[y * img.width:(y + 1) * img.width] for y in range(img.height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", img.width, img.height, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path = Path(path)
    path.write_bytes(png)
    return path


def bmp_to_png(bmp: Path | str, png: Path | str | None = None) -> Path:
    bmp = Path(bmp)
    return write_png(read_bmp(bmp), png or bmp.with_suffix(".png"))


def difference(a: Gray, b: Gray, threshold: int = 32) -> float:
    """Share of pixels whose gray level differs by more than ``threshold``."""
    if (a.width, a.height) != (b.width, b.height):
        return 1.0
    n = sum(1 for x, y in zip(a.pixels, b.pixels) if abs(x - y) > threshold)
    return n / max(1, len(a.pixels))
