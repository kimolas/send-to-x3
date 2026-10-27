"""Firmware-safe titles (ARCHITECTURE §8).

Every stored item title doubles as a file name on the X3's FAT SD card:

- CrossPoint 2.x catalog downloads write ``<dest_dir>/<title>.epub`` after
  ``StringUtils::sanitizeFilenamePreservingExtension`` and then *refuse* any name that
  contains ``..`` or whose path looks like an 8.3 short alias (``ProtectedPaths.h``).
- CrossPoint 1.6.5 OPDS downloads write ``sanitizeFilename(<title>) + ".epub"``.
- The LAN push worker uploads ``<title>.txt`` to the File Transfer server, which only
  rejects ``/``, ``\\``, ``.`` and ``..``.

:func:`safe_title` produces titles that pass through all of those sanitizers byte for
byte (apart from the appended extension), so the file name, the Library entry, the OPDS
row and the EPUB metadata all show the same text. :func:`unique_title` additionally keeps
titles distinct (case-insensitively, like FAT) so two downloads never overwrite each
other.

The ``fw_*`` helpers are faithful Python ports of the firmware routines (cited per
function). Tests use them, and the firmware harness in ``e2e/fwharness`` checks the
ports against the real C++ code.
"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Iterable, Optional

from ..textmodel import clean_text

__all__ = [
    "DEFAULT_MAX_BYTES",
    "safe_title",
    "unique_title",
    "strip_site_affixes",
    "title_problems",
    "is_firmware_safe",
    "fallback_title",
    "fw_sanitize_filename",
    "fw_sanitize_preserving_extension",
    "fw_is_short_alias",
    "fw_plugin_filename_ok",
]

DEFAULT_MAX_BYTES = 90  # firmware cap is 100 bytes minus the extension

# --------------------------------------------------------------------------------------
# Python ports of the firmware's filename rules
# --------------------------------------------------------------------------------------

_FW_ILLEGAL = frozenset('/\\:*?"<>|')


def _fw_sanitize_range(name: str, content_max_bytes: int) -> str:
    """Port of ``sanitizeFilenameRange`` (2.x ``src/util/StringUtils.cpp:18-57``), which
    is the same algorithm as 1.6.5's ``sanitizeFilename`` (``StringUtils.cpp:7-44``).

    Skips leading spaces/dots, maps ``/ \\ : * ? " < > |`` to ``_``, drops code points
    below 0x20 and 0x7F, stops at the first code point that would exceed the byte budget,
    trims trailing spaces/dots, and returns ``book`` when nothing is left.
    """
    i = 0
    while i < len(name) and name[i] in " .":
        i += 1
    out: list[str] = []
    used = 0
    for ch in name[i:]:
        if ch in _FW_ILLEGAL:
            if used + 1 > content_max_bytes:
                break
            out.append("_")
            used += 1
        elif ord(ch) >= 128 or 32 <= ord(ch) < 127:
            n = len(ch.encode("utf-8", "surrogatepass"))
            if used + n > content_max_bytes:
                break
            out.append(ch)
            used += n
    result = "".join(out).rstrip(" .")
    return result or "book"


def fw_sanitize_filename(name: str, max_bytes: int = 100) -> str:
    """``StringUtils::sanitizeFilename`` (1.6.5 OPDS file names, 2.x fallback path)."""
    return _fw_sanitize_range(name, max_bytes)


def _fw_has_conventional_extension(name_bytes: bytes, dot: int) -> bool:
    ext_bytes = len(name_bytes) - dot
    if dot == 0 or ext_bytes < 2 or ext_bytes > 16:
        return False
    return all(chr(c).isascii() and chr(c).isalnum() for c in name_bytes[dot + 1 :])


def fw_sanitize_preserving_extension(name: str, max_bytes: int = 100) -> str:
    """``StringUtils::sanitizeFilenamePreservingExtension`` (2.x ``StringUtils.cpp:64-74``),
    applied to the substituted ``download.filename`` template before writing a file."""
    raw = name.encode("utf-8", "surrogatepass")
    dot = raw.rfind(b".")
    if dot < 0 or not _fw_has_conventional_extension(raw, dot):
        return fw_sanitize_filename(name, max_bytes)
    ext_bytes = len(raw) - dot
    if ext_bytes >= max_bytes:
        return fw_sanitize_filename(name, max_bytes)
    stem = raw[:dot].decode("utf-8", "surrogatepass")
    return _fw_sanitize_range(stem, max_bytes - ext_bytes) + raw[dot:].decode("ascii")


def fw_is_short_alias(segment: str) -> bool:
    """``protectedpaths::isShortAlias`` (2.x ``src/network/ProtectedPaths.h:20-27``), applied
    to a path segment after SdFat's trimming (leading spaces, trailing dots/spaces)."""
    seg = segment.lstrip(" ").rstrip(". ")
    raw = seg.encode("utf-8", "surrogatepass")
    tilde = raw.find(b"~")
    if tilde < 0 or tilde + 1 >= len(raw) or not (0x30 <= raw[tilde + 1] <= 0x39):
        return False
    dot = raw.find(b".")
    base = dot if dot >= 0 else len(raw)
    ext = len(raw) - dot - 1 if dot >= 0 else 0
    return base <= 8 and ext <= 3


def fw_plugin_filename_ok(filename: str, dest_dir: str = "/Inbox") -> bool:
    """Would the 2.x catalog download accept this (already sanitized) file name?

    Mirrors ``PluginCatalogActivity.cpp:1136-1150``: the name must be non-empty, contain
    no ``..`` or ``/``, and ``<dest_dir>/<name>`` must pass ``isPluginPath`` (no ``..``;
    no short alias in the first two path levels; not a credential store).
    """
    if not filename or ".." in filename or "/" in filename:
        return False
    folder = dest_dir.rstrip("/")
    dest = f"{folder}/{filename}"
    if len(dest) <= 1 or not dest.startswith("/") or ".." in dest:
        return False
    depth = 0
    for seg in dest.split("/"):
        if not seg:
            continue
        trimmed = seg.lstrip(" ").rstrip(". ")
        if depth < 2 and fw_is_short_alias(trimmed):
            return False
        if trimmed:
            depth += 1
    lowered = dest.lower()
    for sensitive in ("/.crosspoint/wifi.json", "/.crosspoint/opds.json", "/.crosspoint/koreader.json"):
        if lowered.startswith(sensitive):
            return False
    return True


# --------------------------------------------------------------------------------------
# Site affixes
# --------------------------------------------------------------------------------------

_SEP = r"(?:[|\-\u2013\u2014\u00b7\u2022\u00bb:]|::)"
# Greedy head: the tail is the text after the LAST " <sep> ".
_SUFFIX_RE = re.compile(rf"^(?P<head>.*\S)\s+{_SEP}\s+(?P<tail>\S.*?)\s*$")
# Lazy head: the head is the text before the FIRST " <sep> " (or "Site: ").
_PREFIX_RES = (
    re.compile(rf"^\s*(?P<head>.*?\S)\s+{_SEP}\s+(?P<tail>\S.*)$"),
    re.compile(r"^\s*(?P<head>[^:]*?\S):\s+(?P<tail>\S.*)$"),
)
_GENERIC_SUFFIXES = (
    re.compile(
        r"\s+[-\u2013\u2014|:]\s+(?:wikipedia|wikip\u00e9dia|wikipedia,[^|\-\u2013\u2014]{1,48}|"
        r"\u0432\u0438\u043a\u0438\u043f\u0435\u0434\u0438\u044f)\s*$",
        re.I,
    ),
    re.compile(r"\s+[\u00b7|\-\u2013\u2014]\s+github\s*$", re.I),
    re.compile(r"\s+[-\u2013\u2014|]\s+youtube\s*$", re.I),
    re.compile(r"\s+\|\s+(?:by\s+[^|]{1,80}\s+\|\s+)?medium\s*$", re.I),
)
_GENERIC_PREFIXES = (re.compile(r"^\s*github\s+[-\u2013\u2014]\s+(?=\S)", re.I),)
_MIN_REMAINDER = 3


def _norm_key(s: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s).casefold())


def _site_keys(site: str) -> set[str]:
    site = (site or "").strip()
    if not site:
        return set()
    keys = {_norm_key(site)}
    host = site.lower()
    if " " not in host and "." in host:
        host = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", host).split("/")[0]
        host = re.sub(r"^(?:www\d*|m|amp|mobile)\.", "", host)
        keys.add(_norm_key(host))
        first = host.split(".")[0]
        if len(first) >= 3:
            keys.add(_norm_key(first))
    keys.discard("")
    return keys


def strip_site_affixes(title: str, site: str = "") -> str:
    """Drop well-known site decorations (`` - Wikipedia``, ``GitHub - ``, `` | Medium``)
    and, when ``site`` is given, a leading/trailing segment equal to the site name
    (``Story | The Guardian`` with site ``The Guardian`` or ``theguardian.com``)."""
    t = title
    keys = _site_keys(site)
    for _ in range(4):  # a handful of passes handles "X | Section | Site"
        before = t
        for rx in _GENERIC_SUFFIXES:
            m = rx.search(t)
            if m and len(t[: m.start()].strip()) >= _MIN_REMAINDER:
                t = t[: m.start()].rstrip()
        for rx in _GENERIC_PREFIXES:
            m = rx.match(t)
            if m and len(t[m.end() :].strip()) >= _MIN_REMAINDER:
                t = t[m.end() :]
        if keys:
            m = _SUFFIX_RE.match(t)
            if m and _norm_key(m.group("tail")) in keys and len(m.group("head")) >= _MIN_REMAINDER:
                t = m.group("head")
            for rx in _PREFIX_RES:
                m = rx.match(t)
                if m and _norm_key(m.group("head")) in keys and len(m.group("tail")) >= _MIN_REMAINDER:
                    t = m.group("tail")
                    break
        if t == before:
            break
    return t.strip()


# --------------------------------------------------------------------------------------
# safe_title
# --------------------------------------------------------------------------------------

_SCHEME_RE = re.compile(r"^\s*(?:https?|ftp)://", re.I)
# Arrows (clean_text already maps the Unicode ones to ASCII) read best as a dash once
# "<" and ">" are gone; "<=" / ">=" are left to the plain removal below.
_ARROW_RE = re.compile(r"\s*(?:<=>|<-+>|=>|<-+|-+>)\s*")
_DOTS_RE = re.compile(r"[.\u2026]{2,}")
_ALIAS_RE = re.compile(r"~(?=[0-9])")
_TIME_COLON_RE = re.compile(r"(?<=[0-9]):(?=[0-9])")
_SPACES_RE = re.compile(r" {2,}")
_DOUBLE_SEP_RE = re.compile(r"\s+-(?:\s+-)+(?=\s)")
_LEAD_SEP_RE = re.compile(r"^(?:[-\u2013\u2014]\s+)+")
_TRAIL_SEP_RE = re.compile(r"(?:\s+[-\u2013\u2014,;\u00b7])+$")
_TRANSLATE = {
    ord("/"): "-",
    ord("\\"): "-",
    ord("|"): "-",
    ord(":"): " - ",
    ord("*"): None,
    ord("?"): None,
    ord('"'): "'",
    ord("<"): None,
    ord(">"): None,
    ord("{"): None,
    ord("}"): None,
}
# DOS device names: invalid as the part before the first dot on Windows (the SD card is
# also read on PCs). Superscript digits count too ("COM¹").
_RESERVED_RE = re.compile(r"^(?:con|prn|aux|nul|clock\$|conin\$|conout\$|(?:com|lpt)[0-9\u00b9\u00b2\u00b3])$", re.I)
_ELLIPSIS = "\u2026"


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _drop_unsafe_chars(s: str) -> str:
    """Code points that are invisible, invalid, or outside the BMP (SD-card LFNs are
    UTF-16, the device fonts are BMP-only) are removed."""
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn") or ord(ch) > 0xFFFF:
            continue
        out.append(ch)
    return "".join(out)


def _replace_illegal(s: str) -> str:
    s = _SCHEME_RE.sub("", s)
    s = _ARROW_RE.sub(" - ", s)
    s = _TIME_COLON_RE.sub(".", s)  # "10:30" -> "10.30", "John 3:16" -> "John 3.16"
    s = s.translate(_TRANSLATE)
    s = _ALIAS_RE.sub("~ ", s)  # break the 8.3 alias pattern: "~5 min" -> "~ 5 min"
    s = _DOTS_RE.sub(_ELLIPSIS, s)  # ".." is refused by the 2.x downloader
    s = _SPACES_RE.sub(" ", s)
    s = _DOUBLE_SEP_RE.sub(" -", s)
    return s


def _trim(s: str) -> str:
    s = s.strip(" ")
    while s and unicodedata.combining(s[0]):  # a mark with no base character
        s = s[1:].lstrip(" ")
    s = s.lstrip(" .")  # firmware skips leading spaces/dots; dot-files are hidden
    s = _LEAD_SEP_RE.sub("", s)
    s = s.rstrip(" .")  # firmware trims trailing spaces/dots
    s = _TRAIL_SEP_RE.sub("", s)
    return s.rstrip(" .")


def _avoid_reserved(s: str) -> str:
    stem, dot, rest = s.partition(".")
    if _RESERVED_RE.match(stem.rstrip(" ")):
        return stem.rstrip(" ") + "_" + dot + rest
    return s


def _cut_index(s: str, budget: int) -> int:
    """Largest index i such that s[:i] fits in ``budget`` UTF-8 bytes and does not end
    between a base character and its combining marks."""
    used = 0
    i = 0
    for ch in s:
        n = _utf8_len(ch)
        if used + n > budget:
            break
        used += n
        i += 1
    while 0 < i < len(s) and unicodedata.combining(s[i]):
        i -= 1
    return i


def _cap_bytes(s: str, max_bytes: int) -> str:
    """Cut to ``max_bytes`` on a character boundary, preferring a word boundary, and mark
    the cut with an ellipsis when there is room for one."""
    if _utf8_len(s) <= max_bytes:
        return s
    mark = _ELLIPSIS if max_bytes >= 8 else ""
    budget = max_bytes - _utf8_len(mark)
    i = _cut_index(s, budget)
    head = s[:i]
    space = head.rfind(" ")
    if space >= max(1, int(len(head) * 0.6)):
        head = head[:space]
    head = _trim(head)
    if not head:
        return ""
    return head + mark


def _clean_once(s: str, max_bytes: int, site: str = "") -> str:
    # Replacing characters can create a new affix ("Foo / Wikipedia" -> "Foo - Wikipedia")
    # or bring a base letter next to a combining mark, so every pass strips affixes and
    # re-normalizes (keeps the result NFC and the function idempotent).
    s = strip_site_affixes(s, site)
    s = unicodedata.normalize("NFC", _replace_illegal(s))
    s = _trim(s)
    s = _avoid_reserved(s)
    s = _cap_bytes(s, max_bytes)
    return _trim(s)


def fallback_title(now: Optional[float] = None) -> str:
    """``Note YYYY-MM-DD HH.MM`` in local time (``:`` is not allowed in file names)."""
    t = time.localtime(time.time() if now is None else now)
    return time.strftime("Note %Y-%m-%d %H.%M", t)


def safe_title(
    title: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    site: str = "",
    now: Optional[float] = None,
) -> str:
    """Make ``title`` survive every firmware file-name sanitizer unchanged (see module doc).

    ``site`` (optional) enables stripping a ``| Site Name`` affix equal to the site.
    ``now`` (unix seconds, optional) fixes the time used by the empty-title fallback.
    The result is never empty, at most ``max_bytes`` UTF-8 bytes, and idempotent:
    ``safe_title(safe_title(x)) == safe_title(x)``.
    """
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    s = _drop_unsafe_chars(clean_text(title or ""))
    for _ in range(8):  # steps can expose new work (e.g. "a.?.b" -> "a..b"); iterate to a fixed point
        nxt = _clean_once(s, max_bytes, site)
        if nxt == s:
            break
        s = nxt
    if not s or title_problems(s, max_bytes=max_bytes):
        s = _clean_once(fallback_title(now), max_bytes)
    return s


def _title_key(title: str) -> str:
    """FAT compares long names case-insensitively; casefold is a safe superset."""
    return unicodedata.normalize("NFC", title).casefold()


_COUNTER_RE = re.compile(r"^(?P<stem>.*\S) \((?P<n>[0-9]{1,6})\)$")


def unique_title(
    title: str,
    taken: Iterable[str],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    site: str = "",
    now: Optional[float] = None,
) -> str:
    """:func:`safe_title`, then `` (2)``, `` (3)``, ... until it differs (case-insensitively)
    from every title in ``taken``. The suffix always fits inside ``max_bytes``. ``site``
    and ``now`` are passed to :func:`safe_title`."""
    base = safe_title(title, max_bytes=max_bytes, site=site, now=now)
    keys = {_title_key(t) for t in taken}
    if _title_key(base) not in keys:
        return base
    m = _COUNTER_RE.match(base)
    stem, start = (m.group("stem"), int(m.group("n")) + 1) if m else (base, 2)
    for n in range(start, start + 100_000):
        suffix = f" ({n})"
        room = max_bytes - _utf8_len(suffix)
        head = _trim(_cap_bytes(stem, room)) if room > 0 else ""
        cand = (head + suffix) if head else suffix.strip()
        if _title_key(cand) not in keys and not title_problems(cand, max_bytes=max_bytes):
            return cand
    raise RuntimeError("could not find a unique title")  # pragma: no cover - 100k collisions


# --------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------


def title_problems(title: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> list[str]:
    """Reasons ``title`` would not survive the firmware as a file name (empty = safe)."""
    problems: list[str] = []
    if not title:
        return ["empty"]
    try:
        size = _utf8_len(title)
    except UnicodeEncodeError:
        return ["not encodable as UTF-8"]
    if size > max_bytes:
        problems.append(f"{size} bytes > {max_bytes}")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") or ord(c) > 0xFFFF for c in title):
        problems.append("invisible, control or non-BMP character")
    if "{" in title or "}" in title:
        problems.append("template brace")
    if title[0] in " ." or title[-1] in " .":
        problems.append("leading/trailing space or dot")
    if ".." in title:
        problems.append("'..'")
    if fw_sanitize_filename(title) != title:
        problems.append("changed by 1.6.5 sanitizeFilename")
    for ext in (".epub", ".txt"):
        name = title + ext
        if fw_sanitize_preserving_extension(name) != name:
            problems.append(f"changed by 2.x sanitizeFilenamePreservingExtension ({ext})")
        if not fw_plugin_filename_ok(name, "/Inbox") or not fw_plugin_filename_ok(name, "/"):
            problems.append(f"refused by 2.x downloader ({ext})")
    if _RESERVED_RE.match(title.partition(".")[0].rstrip(" ")):
        problems.append("Windows reserved device name")
    return problems


def is_firmware_safe(title: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    return not title_problems(title, max_bytes=max_bytes)
