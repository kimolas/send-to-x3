"""Tiny PDF writer for the PDF tests (standard library only).

Writes PDFs from raw PDF syntax: text in the standard-14 fonts at exact positions,
8-bit gray images, filled rectangles, page rotation, document info, /Lang, separate
show operations (to imitate typesetters that emit soft hyphens on their own), and
RC4 40-bit encryption (standard security handler, revision 2) with user/owner
passwords. Coordinates given to :meth:`Page.text` are distances from the top-left
corner of the unrotated page, ``y`` being the baseline.
"""

from __future__ import annotations

import hashlib
import struct
import zlib

FONTS = {
    "R": "Times-Roman",
    "B": "Times-Bold",
    "I": "Times-Italic",
    "H": "Helvetica",
    "HB": "Helvetica-Bold",
}

PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _esc(s: str) -> bytes:
    raw = s.encode("cp1252")
    out = bytearray()
    for b in raw:
        if b in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([b])
        elif b < 0x20 or b > 0x7E:
            out += b"\\%03o" % b
        else:
            out.append(b)
    return bytes(out)


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for b in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


class Page:
    def __init__(self, width: float = 612, height: float = 792, rotate: int = 0):
        self.width, self.height, self.rotate = width, height, rotate
        self.ops: list[bytes] = []
        self.images: list[tuple[int, int, bytes]] = []

    def text(self, x: float, y: float, s: str, size: float = 10, font: str = "R", rise: float = 0.0) -> "Page":
        """Show ``s`` with its baseline ``y`` points below the top edge."""
        by = self.height - y
        self.ops.append(b"BT /%s %.2f Tf %.2f Ts 1 0 0 1 %.2f %.2f Tm (%s) Tj ET" % (
            font.encode(), size, rise, x, by, _esc(s)))
        return self

    def runs(self, x: float, y: float, parts: list[tuple[str, str, float, float]]) -> "Page":
        """Several show operations on one baseline, each its own text object:
        ``parts`` = [(text, font, size, rise)]. Uses the standard widths to advance."""
        by = self.height - y
        for s, font, size, rise in parts:
            self.ops.append(b"BT /%s %.2f Tf %.2f Ts 1 0 0 1 %.2f %.2f Tm (%s) Tj ET" % (
                font.encode(), size, rise, x, by, _esc(s)))
            x += text_width(s, font, size)
        return self

    def rotated_text(self, x: float, y: float, s: str, size: float = 10, font: str = "R") -> "Page":
        """Text drawn bottom-to-top in PDF space (reads normally on a /Rotate 90 page)."""
        self.ops.append(b"BT /%s %.2f Tf 0 1 -1 0 %.2f %.2f Tm (%s) Tj ET" % (font.encode(), size, x, y, _esc(s)))
        return self

    def rect(self, x: float, y: float, w: float, h: float, gray: float = 0.5) -> "Page":
        self.ops.append(b"q %.2f g %.2f %.2f %.2f %.2f re f Q" % (gray, x, self.height - y - h, w, h))
        return self

    def image(self, x: float, y: float, w: float, h: float, px_w: int, px_h: int, data: bytes) -> "Page":
        """8-bit gray image (``px_w * px_h`` bytes) drawn in the box (x, y)..(x+w, y+h)."""
        assert len(data) == px_w * px_h
        name = b"Im%d" % len(self.images)
        self.images.append((px_w, px_h, data))
        self.ops.append(b"q %.2f 0 0 %.2f %.2f %.2f cm /%s Do Q" % (w, h, x, self.height - y - h, name))
        return self


# Advance widths (1/1000 em) of the characters the tests use, from the standard AFM
# files; everything else falls back to 0.5 em. Only needed by Page.runs().
_W_TIMES = dict(zip(" !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz",
                    [250, 333, 408, 500, 500, 833, 778, 180, 333, 333, 500, 564, 250, 333, 250, 278, 500, 500, 500,
                     500, 500, 500, 500, 500, 500, 500, 278, 278, 564, 564, 564, 444, 921, 722, 667, 667, 722, 611,
                     556, 722, 722, 333, 389, 722, 611, 889, 722, 722, 556, 722, 667, 556, 611, 722, 722, 944, 722,
                     722, 611, 333, 278, 333, 469, 500, 333, 444, 500, 444, 500, 444, 333, 500, 500, 278, 278, 500,
                     278, 778, 500, 500, 500, 500, 333, 389, 278, 500, 500, 722, 500, 500, 444]))


def text_width(s: str, font: str, size: float) -> float:
    return sum(_W_TIMES.get(c, 500) for c in s) * size / 1000.0


def build(pages: list[Page], *, title: str | None = None, author: str | None = None, lang: str | None = None,
          created: str | None = None, user_password: str | None = None, owner_password: str | None = None,
          compress: bool = True) -> bytes:
    """Serialize ``pages``. With ``user_password`` or ``owner_password`` the file is
    RC4-encrypted (an empty user password opens without prompting)."""
    objs: dict[int, bytes] = {}
    streams: dict[int, tuple[bytes, bytes]] = {}  # num -> (dict without Length, data)
    font_nums = {}
    n = 3
    for key in FONTS:
        font_nums[key] = n
        objs[n] = b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % FONTS[key].encode()
        n += 1
    font_res = b" ".join(b"/%s %d 0 R" % (k.encode(), v) for k, v in font_nums.items())
    kids = []
    for p in pages:
        page_num, content_num = n, n + 1
        n += 2
        xobjs = []
        for i, (w, h, data) in enumerate(p.images):
            streams[n] = (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                          b"/BitsPerComponent 8" % (w, h), data)
            xobjs.append(b"/Im%d %d 0 R" % (i, n))
            n += 1
        res = b"<< /Font << " + font_res + b" >>" + (b" /XObject << " + b" ".join(xobjs) + b" >>" if xobjs else b"") + b" >>"
        rot = b" /Rotate %d" % p.rotate if p.rotate else b""
        objs[page_num] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f]%s /Resources %s /Contents %d 0 R >>" % (
            p.width, p.height, rot, res, content_num)
        streams[content_num] = (b"<<", b"\n".join(p.ops))
        kids.append(b"%d 0 R" % page_num)
    lang_entry = b" /Lang (%s)" % _esc(lang) if lang else b""
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R%s >>" % lang_entry
    objs[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(kids))
    info_num = None
    info_strings: dict[bytes, bytes] = {}
    if title is not None:
        info_strings[b"Title"] = title.encode("cp1252")
    if author is not None:
        info_strings[b"Author"] = author.encode("cp1252")
    if created is not None:
        info_strings[b"CreationDate"] = created.encode("ascii")  # e.g. "D:20240111093000-05'00'"
    if info_strings:
        info_num = n
        n += 1
    file_id = hashlib.md5(b"sendtox3-test-%d" % len(pages)).digest()

    key = None
    encrypt_num = None
    if user_password is not None or owner_password is not None:
        upw = (user_password or "").encode("latin-1")[:32]
        opw = (owner_password or user_password or "").encode("latin-1")[:32]
        okey = hashlib.md5((opw + PAD)[:32]).digest()[:5]
        o_entry = _rc4(okey, (upw + PAD)[:32])
        p_val = -4
        key = hashlib.md5((upw + PAD)[:32] + o_entry + struct.pack("<i", p_val) + file_id).digest()[:5]
        u_entry = _rc4(key, PAD)
        encrypt_num = n
        n += 1
        objs[encrypt_num] = b"<< /Filter /Standard /V 1 /R 2 /O <%s> /U <%s> /P %d >>" % (
            o_entry.hex().encode(), u_entry.hex().encode(), p_val)

    def obj_key(num: int) -> bytes:
        return hashlib.md5(key + struct.pack("<i", num)[:3] + b"\x00\x00").digest()[: len(key) + 5]

    if info_num is not None:
        parts = []
        for k, v in info_strings.items():
            if key is not None:
                v = _rc4(obj_key(info_num), v)
            parts.append(b"/%s <%s>" % (k, v.hex().encode()))
        objs[info_num] = b"<< " + b" ".join(parts) + b" >>"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in range(1, n):
        offsets[num] = len(out)
        if num in streams:
            head, data = streams[num]
            filt = b""
            if compress and not head.startswith(b"<< /Type /XObject"):
                data = zlib.compress(data)
                filt = b" /Filter /FlateDecode"
            if key is not None:
                data = _rc4(obj_key(num), data)
            body = head + filt + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"
        else:
            body = objs[num]
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % n
    for num in range(1, n):
        out += b"%010d 00000 n \n" % offsets[num]
    trailer = b"<< /Size %d /Root 1 0 R" % n
    if info_num is not None:
        trailer += b" /Info %d 0 R" % info_num
    if encrypt_num is not None:
        trailer += b" /Encrypt %d 0 R" % encrypt_num
    trailer += b" /ID [<%s> <%s>] >>" % (file_id.hex().encode(), file_id.hex().encode())
    out += b"trailer\n" + trailer + b"\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def paragraph_lines(page: Page, x: float, y: float, lines: list[str], *, size: float = 10, leading: float = 12,
                    font: str = "R") -> float:
    """Draw ``lines`` downwards from baseline ``y``; returns the next baseline."""
    for ln in lines:
        page.text(x, y, ln, size=size, font=font)
        y += leading
    return y
