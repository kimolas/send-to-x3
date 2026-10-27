"""Safe URL fetching, URL cleaning and charset decoding.

:func:`fetch_url` is the only way the app reaches the internet for shared links:

- http/https only, at most :data:`MAX_REDIRECTS` redirects (each hop re-checked),
  a total time budget and a body-size cap (applied to the *decompressed* body).
- SSRF guard: unless ``allow_private`` is set, every destination is checked *after*
  DNS resolution and the connection is made to the checked address (a resolver
  wrapper plus a check inside aiohttp's connector, so DNS rebinding cannot slip past,
  and neither can IP literals, which aiohttp connects to without asking the resolver).
  Hosts are checked in the form aiohttp dials (yarl's IDNA normalisation turns
  full-width digits and ideographic dots into a plain IPv4 literal). Private,
  loopback, link-local, multicast, CGNAT, reserved and unspecified addresses are
  refused, including IPv4 embedded in IPv6 (mapped, 6to4, Teredo, NAT64). Guarded
  fetches use IPv4 only: a device on the home network can have a *global* IPv6
  address, which no address-range check can tell apart from a public server.
- Environment proxies are ignored (``trust_env=False``) so they cannot bypass the guard.
- HTTP errors and bot walls are *not* exceptions: the result carries ``blocked``
  (e.g. ``"Cloudflare challenge"``, ``"HTTP 403"``) so callers can explain what happened.
  :class:`FetchError` is raised for network-level failures with a user-facing reason.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Literal
from collections.abc import Callable, Mapping
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL

from .. import __version__

__all__ = [
    "MAX_REDIRECTS",
    "USER_AGENT",
    "FetchError",
    "FetchResult",
    "clean_url",
    "decode_bytes",
    "decode_html",
    "detect_block",
    "fetch_url",
    "is_blocked_ip",
    "normalize_charset",
    "sniff_kind",
]

_MAJOR_MINOR = ".".join(__version__.split(".")[:2])
USER_AGENT = f"Mozilla/5.0 (compatible; SendToX3/{_MAJOR_MINOR}; +https://github.com/kimolas/send-to-x3)"
MAX_REDIRECTS = 5
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9,*;q=0.5",
}
# Bodies of these types are never downloaded: they cannot become text.
_NO_BODY_PREFIXES = ("image/", "video/", "audio/", "font/")
_CHUNK = 64 * 1024
_MAX_HEADER = 64 * 1024


class FetchError(Exception):
    """A link could not be fetched. ``reason`` is short and user-facing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class FetchResult:
    url: str  # final URL after redirects, cleaned
    status: int
    content_type: str  # lowercased mime without params ("" when missing)
    charset: str | None  # charset parameter of Content-Type, lowercased
    headers: dict[str, str] = field(default_factory=dict)  # lowercased names
    body: bytes = b""
    blocked: str | None = None  # e.g. "Cloudflare challenge", "HTTP 403"

    @property
    def kind(self) -> str:
        """Content kind by header and magic bytes; see :func:`sniff_kind`."""
        return sniff_kind(self.content_type, self.body)


# --------------------------------------------------------------------------------------
# SSRF guard
# --------------------------------------------------------------------------------------

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_blocked_ip(addr: str) -> bool:
    """True if ``addr`` is not a public unicast address (or is not an address at all)."""
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            ip = embedded
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast:
        return True
    if ip.is_reserved or ip.is_unspecified or not ip.is_global:
        return True
    return isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT


class _BlockedAddress(OSError):
    """Raised inside the connector when a host only resolves to blocked addresses."""

    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason or _PRIVATE_REASON


def _guard_blocks(addr: str) -> bool:
    """The guard's verdict on a resolved address: non-public, or IPv6 (see module doc)."""
    return is_blocked_ip(addr) or ":" in addr


class _GuardedResolver(AbstractResolver):
    """Wraps a real resolver and drops every non-public address it returns."""

    def __init__(self, inner: AbstractResolver, is_blocked: Callable[[str], bool]) -> None:
        self._inner = inner
        self._is_blocked = is_blocked

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        try:
            results = await self._inner.resolve(host, port, family)
        except OSError:
            # An IPv4 lookup of an IPv6-only host fails like a missing host; say why.
            if family == socket.AF_INET and await self._has_public_ipv6(host, port):
                raise _BlockedAddress(f"{host} has only IPv6 addresses", _IPV6_ONLY_REASON) from None
            raise
        allowed = [r for r in results if not self._is_blocked(r["host"])]
        if not allowed:
            if all(":" in r["host"] and not is_blocked_ip(r["host"]) for r in results):
                raise _BlockedAddress(f"{host} has only IPv6 addresses", _IPV6_ONLY_REASON)
            raise _BlockedAddress(f"{host} resolves only to non-public addresses")
        return allowed

    async def _has_public_ipv6(self, host: str, port: int) -> bool:
        """Whether ``host`` has a public IPv6 address (only asked after the IPv4 lookup
        failed, to explain the failure; nothing connects to it)."""
        try:
            async with asyncio.timeout(_IPV6_PROBE_SECONDS):
                if isinstance(self._inner, aiohttp.ThreadedResolver):
                    # Not via the resolver: it passes AI_ADDRCONFIG, which hides IPv6
                    # answers from a container that has no IPv6 address of its own.
                    infos = await asyncio.get_running_loop().getaddrinfo(
                        host, port, family=socket.AF_INET6, type=socket.SOCK_STREAM
                    )
                    addrs = [str(info[4][0]) for info in infos]
                else:
                    addrs = [r["host"] for r in await self._inner.resolve(host, port, socket.AF_INET6)]
        except (OSError, TimeoutError):
            return False
        return any(":" in a and not is_blocked_ip(a) for a in addrs)

    async def close(self) -> None:
        await self._inner.close()


class _GuardedConnector(aiohttp.TCPConnector):
    """A connector that checks every address it is about to connect to.

    aiohttp's ``_resolve_host`` answers IP-literal hosts itself, without calling the
    resolver, so :class:`_GuardedResolver` alone never sees them; this checks the
    literal and the DNS answers alike, right before connecting.
    """

    def __init__(self, *, is_blocked: Callable[[str], bool], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._is_blocked = is_blocked

    async def _resolve_host(self, host: str, port: int, traces: object = None) -> list[ResolveResult]:
        results = await super()._resolve_host(host, port, traces=traces)  # type: ignore[arg-type]
        allowed = [r for r in results if not self._is_blocked(r["host"])]
        if not allowed:
            raise _BlockedAddress(f"{host} is not a public address")
        return allowed


def _make_resolver() -> AbstractResolver:
    """The real DNS resolver (tests replace this to simulate DNS answers)."""
    return aiohttp.ThreadedResolver()


_PRIVATE_REASON = (
    "This link points to a local or private network address, which is blocked (turn on allow_private_urls to allow it)."
)
_IPV6_REASON = (
    "This link points to an IPv6 address. Those are only fetched when allow_private_urls is on, because a device "
    "on your home network can have a public IPv6 address."
)
_IPV6_ONLY_REASON = (
    "This server only has an IPv6 address; Send to X3 fetches over IPv4 only "
    "(unless allow_private_urls is on, because a device on your home network can have a public IPv6 address)."
)
_IPV6_PROBE_SECONDS = 5.0  # the extra lookup that explains a failed IPv4 one


def _check_hop(url: str, allow_private: bool) -> None:
    """Validate scheme and (for IP-literal hosts, which skip DNS) the address.

    The host is taken the way aiohttp will dial it (``yarl.URL(url).raw_host``, IDNA /
    UTS #46 normalised): ``http://１２７.０.０.１/`` and ``http://127。0。0。1/`` are
    plain 127.0.0.1 by then, whatever :func:`urllib.parse.urlsplit` says.
    """
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
        has_host = bool(parts.hostname)
        host = (URL(url).raw_host or "").lower() if has_host else ""
    except (ValueError, TypeError):  # UnicodeError (bad IDNA) is a ValueError
        raise FetchError("The link is not a valid web address.") from None
    if parts.scheme not in ("http", "https"):
        raise FetchError("Only http and https links can be fetched.")
    if not host:
        raise FetchError("The link has no host name.")
    if allow_private:
        return
    name = host.rstrip(".")
    if name == "localhost" or name.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        raise FetchError(_PRIVATE_REASON)
    literal = host.strip("[]")
    if ":" in literal or re.fullmatch(r"[0-9.]+", literal):
        if is_blocked_ip(literal):
            raise FetchError(_PRIVATE_REASON)
        if ":" in literal:
            raise FetchError(_IPV6_REASON)


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------


def _parse_content_type(value: str) -> tuple[str, str | None]:
    mime, _, params = (value or "").partition(";")
    mime = mime.strip().lower()
    m = re.search(r"""charset\s*=\s*["']?([^"';\s]+)""", params, re.I)
    return mime, (m.group(1).lower() if m else None)


def _headers_dict(resp: aiohttp.ClientResponse) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in resp.headers.items():
        k = k.lower()
        out[k] = f"{out[k]}, {v}" if k in out else v
    return out


async def _read_capped(resp: aiohttp.ClientResponse, cap: int) -> bytes:
    if resp.content_length is not None and resp.content_length > cap:
        raise FetchError(f"The file is too large (over {cap // 1_000_000} MB).")
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(_CHUNK):
        buf += chunk
        if len(buf) > cap:
            raise FetchError(f"The file is too large (over {cap // 1_000_000} MB).")
    return bytes(buf)


async def _fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    max_pdf_bytes: int | None,
    allow_private: bool,
    headers: Mapping[str, str] | None,
) -> FetchResult:
    connector: aiohttp.TCPConnector
    if allow_private:
        connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=60)
    else:
        connector = _GuardedConnector(
            is_blocked=lambda addr: _guard_blocks(addr),  # looked up per call (tests patch is_blocked_ip)
            resolver=_GuardedResolver(_make_resolver(), lambda addr: _guard_blocks(addr)),
            family=socket.AF_INET,  # IPv4 only, see the module docstring
            limit=4,
            ttl_dns_cache=60,
        )
    client_timeout = aiohttp.ClientTimeout(total=None, connect=min(10.0, timeout), sock_read=timeout)
    req_headers = dict(_DEFAULT_HEADERS)
    if headers:
        req_headers.update(headers)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=client_timeout,
        headers=req_headers,
        trust_env=False,  # environment proxies would bypass the SSRF guard
        max_line_size=_MAX_HEADER,
        max_field_size=_MAX_HEADER,  # some news sites send > 8 KB CSP headers
    ) as session:
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            _check_hop(current, allow_private)
            async with session.get(current, allow_redirects=False) as resp:
                location = resp.headers.get("Location")
                if resp.status in _REDIRECT_CODES and location:
                    if hop == MAX_REDIRECTS:
                        raise FetchError(f"Too many redirects (more than {MAX_REDIRECTS}).")
                    current = urljoin(str(resp.url), location.strip())
                    continue
                mime, charset = _parse_content_type(resp.headers.get("Content-Type", ""))
                hdrs = _headers_dict(resp)
                if mime.startswith(_NO_BODY_PREFIXES):
                    body = b""
                else:
                    cap = max_pdf_bytes if (max_pdf_bytes and mime == "application/pdf") else max_bytes
                    body = await _read_capped(resp, cap)
                final = clean_url(str(resp.url))
                return FetchResult(
                    url=final,
                    status=resp.status,
                    content_type=mime,
                    charset=charset,
                    headers=hdrs,
                    body=body,
                    blocked=detect_block(resp.status, hdrs, final, body),
                )
    raise FetchError(f"Too many redirects (more than {MAX_REDIRECTS}).")  # pragma: no cover


def _connector_reason(exc: aiohttp.ClientConnectorError, url: str) -> str:
    host = urlsplit(url).hostname or "the server"
    os_err = exc.os_error
    if isinstance(os_err, _BlockedAddress):
        return os_err.reason
    if isinstance(exc.__cause__, _BlockedAddress):
        return exc.__cause__.reason
    if isinstance(exc, aiohttp.ClientConnectorCertificateError):
        return f"The secure connection to {host} failed (certificate problem)."
    if isinstance(exc, aiohttp.ClientSSLError):
        return f"The secure connection to {host} failed."
    if isinstance(exc, aiohttp.ClientConnectorDNSError):
        return f"Could not find the server {host}."
    return f"Could not connect to {host}."


async def fetch_url(
    url: str,
    *,
    timeout: float = 20,
    max_bytes: int = 8_000_000,
    allow_private: bool = False,
    headers: Mapping[str, str] | None = None,
    max_pdf_bytes: int | None = None,
) -> FetchResult:
    """GET ``url`` safely (see the module docstring). Never follows non-http(s) links.

    ``url`` is cleaned first (:func:`clean_url`), so known redirector wrappers are
    unwrapped without a request. ``timeout`` is the total budget in seconds for all
    hops and the body. ``headers`` are added to the default ones (User-Agent etc.).
    ``max_pdf_bytes`` (optional) raises the size cap for ``application/pdf`` bodies.

    Raises :class:`FetchError` for invalid/blocked links, network failures, timeouts,
    oversize bodies and redirect loops. HTTP error statuses are returned, not raised.
    """
    target = clean_url(url)
    try:
        async with asyncio.timeout(timeout):
            return await _fetch(
                target,
                timeout=timeout,
                max_bytes=max_bytes,
                max_pdf_bytes=max_pdf_bytes,
                allow_private=allow_private,
                headers=headers,
            )
    except FetchError:
        raise
    except TimeoutError:
        raise FetchError(f"The site did not answer within {timeout:g} seconds.") from None
    except aiohttp.ClientConnectorError as exc:
        raise FetchError(_connector_reason(exc, target)) from None
    except (aiohttp.InvalidURL, aiohttp.NonHttpUrlClientError, ValueError, UnicodeError):
        raise FetchError("The link is not a valid web address.") from None
    except aiohttp.ClientPayloadError:
        raise FetchError("The download was cut off or corrupted.") from None
    except aiohttp.ClientResponseError as exc:
        raise FetchError(f"The server sent an invalid response ({exc.message or exc.status}).") from None
    except (aiohttp.ClientError, OSError) as exc:
        raise FetchError(f"The connection failed ({type(exc).__name__}).") from None


# --------------------------------------------------------------------------------------
# Content kind and bot-wall detection
# --------------------------------------------------------------------------------------

Kind = Literal["html", "text", "pdf", "epub", "image", "media", "other"]
_HTML_START_RE = re.compile(
    rb"^\s*(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html|head|body)\b", re.I | re.S
)


def sniff_kind(content_type: str, body: bytes) -> Kind:
    """Classify a response by Content-Type, corrected by magic bytes.

    PDF and EPUB signatures win over any header; HTML served as text/plain or
    octet-stream is recognised by its first tag; an untyped body without NUL bytes is
    treated as text.
    """
    ct = (content_type or "").lower()
    head = body[:1024]
    # PDF readers accept junk before the signature; only trust that when the type allows it
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-") or (
        b"%PDF-" in head
        and ct in ("", "application/pdf", "application/x-pdf", "application/octet-stream", "binary/octet-stream")
    ):
        return "pdf"
    if body.startswith(b"PK\x03\x04") and body[30:58] == b"mimetypeapplication/epub+zip":
        return "epub"
    if ct == "application/epub+zip":
        return "epub"
    if ct.startswith("image/"):
        return "image"
    if ct.startswith(("video/", "audio/")):
        return "media"
    if ct in ("text/html", "application/xhtml+xml") or _HTML_START_RE.match(head):
        return "html"
    if ct == "application/pdf":
        return "pdf"  # no signature in the first KB; the PDF reader will decide
    if ct.startswith("text/") and ct not in ("text/css", "text/javascript", "text/xml", "text/csv", "text/calendar"):
        return "text"
    if ct in ("", "application/octet-stream") and body and b"\x00" not in head:
        return "text"
    return "other"


_CHALLENGE_MARKERS: tuple[tuple[re.Pattern[bytes], str], ...] = tuple(
    (re.compile(rx, re.I), why)
    for rx, why in (
        (rb"Attention Required! \| Cloudflare", "Cloudflare block"),
        (rb"<title>\s*Just a moment\.\.\.\s*</title>", "Cloudflare challenge"),
        (rb"challenges\.cloudflare\.com|/cdn-cgi/challenge-platform/|cf-chl-", "Cloudflare challenge"),
        (rb"Checking (?:if the site connection is secure|your browser before accessing)", "Cloudflare challenge"),
        (rb"captcha-delivery\.com|Please enable JS and disable any ad blocker", "DataDome bot check"),
        (rb"_pxCaptcha|px-captcha|perimeterx", "PerimeterX bot check"),
        (rb"<title>Access Denied</title>[\s\S]{0,4000}Reference\s*#", "Akamai access denied"),
        (rb"Incapsula incident ID|_Incapsula_Resource", "Imperva bot check"),
        (rb"<title>\s*Pardon Our Interruption", "bot check"),
    )
)
_CONSENT_HOST_RE = re.compile(
    r"^(?:consent\.(?:google|youtube|yahoo)\.[a-z.]+|guce\.(?:yahoo|aol)\.com|consent\.cmp\.oath\.com|"
    r"myprivacy\.dpgmedia\.[a-z]+|consent\.[a-z0-9-]+\.[a-z.]+)$"
)
_SMALL_PAGE = 60_000


def detect_block(status: int, headers: Mapping[str, str], url: str, body: bytes) -> str | None:
    """Recognise bot walls, consent walls and HTTP errors; ``None`` when the page is usable.

    Every extractor happily "extracts" a challenge page, so this runs before extraction.
    Body markers are only checked on error statuses or small pages (challenge pages are
    tiny; a long article that merely mentions Cloudflare is not a block).
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host and _CONSENT_HOST_RE.match(host):
        return "consent page"
    h = {k.lower(): v for k, v in headers.items()}
    if h.get("cf-mitigated", "").lower() == "challenge":
        return "Cloudflare challenge"
    if status >= 400 and ("x-datadome" in h or "x-datadome-cid" in h):
        return "DataDome bot check"
    if status >= 400 or len(body) < _SMALL_PAGE:
        sample = body[:_SMALL_PAGE]
        for rx, why in _CHALLENGE_MARKERS:
            if rx.search(sample):
                return why
    if status >= 400:
        return f"HTTP {status}"
    return None


# --------------------------------------------------------------------------------------
# URL cleaning
# --------------------------------------------------------------------------------------

_TRACKING_PREFIXES = (
    "utm_", "mc_", "pk_", "mtm_", "matomo_", "piwik_", "hsa_", "_hs", "oly_", "vero_",
    "at_", "itm_", "ns_", "wt.", "fb_action_", "fb_source", "fb_ref", "gad_", "__twitter",
)  # fmt: skip
_TRACKING_EXACT = frozenset(
    """
    fbclid gclid gclsrc dclid gbraid wbraid msclkid yclid ysclid igshid igsh twclid ttclid
    li_fat_id mkt_tok _ga _gl _ke _kx ref_src ref_url s_cid cmpid ncid ocid sr_share spm scm
    _branch_match_id _branch_referrer rb_clickid wickedid __s ito guccounter guce_referrer
    guce_referrer_sig smid smtyp srsltid mbid _openstat cmp icid xtor irclickid irgwc
    triedredirect showwelcome showwelcomeonshare mibextid __cft__ __tn__ share_id rdt
    """.split()
)
# Host suffix -> parameters that are tracking-only on that host. Gift/share tokens that
# unlock articles (nytimes unlocked_article_code, wsj st, bloomberg accessToken, ...)
# are deliberately NOT listed: stripping them would put the article behind the paywall.
_HOST_PARAMS: dict[str, frozenset[str]] = {
    "medium.com": frozenset({"source", "sk"}),
    "substack.com": frozenset({"r", "publication_id", "post_id", "isfreemail", "s"}),
    "x.com": frozenset({"s", "t"}),
    "twitter.com": frozenset({"s", "t"}),
    "youtube.com": frozenset({"si", "feature", "pp", "ab_channel"}),
    "youtu.be": frozenset({"si", "feature"}),
    "spotify.com": frozenset({"si", "context", "nd"}),
    "instagram.com": frozenset({"img_index"}),
    "threads.net": frozenset({"xmt", "slof"}),
    "linkedin.com": frozenset(
        {"trk", "trackingid", "lipi", "refid", "midtoken", "midsig", "trkemail", "eid", "otptoken"}
    ),
    "tiktok.com": frozenset({"is_from_webapp", "sender_device", "_r", "_t", "u_code", "preview_pb", "share_item_id"}),
    "facebook.com": frozenset({"rdid", "share_url"}),
}
_GOOGLE_RE = re.compile(r"(?:^|\.)google\.[a-z]{2,3}(?:\.[a-z]{2})?$")


def _host_match(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _query_param(query: str, *names: str) -> str:
    for k, v in parse_qsl(query, keep_blank_values=True):
        if k in names:
            return v
    return ""


def _proofpoint_v2(u: str) -> str:
    # urldefense.proofpoint.com/v2/url?u=https-3A__example.com_path&d=...
    return re.sub(r"-([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), u.replace("_", "/"))


def _unwrap_once(url: str) -> str | None:
    """Return the wrapped target URL if ``url`` is a known redirector, else ``None``."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    path = p.path
    target = ""
    if _GOOGLE_RE.search(host):
        if path == "/url":
            target = _query_param(p.query, "q", "url")
        elif path.startswith("/amp/s/"):
            target = "https://" + path[len("/amp/s/") :]
        elif path.startswith("/amp/"):
            target = "http://" + path[len("/amp/") :]
    elif host.endswith(".cdn.ampproject.org"):
        m = re.match(r"^/[cv]/(s/)?(.+)$", path)
        if m:
            target = ("https://" if m.group(1) else "http://") + m.group(2)
            if p.query:
                target += "?" + p.query
    elif host in ("l.facebook.com", "lm.facebook.com", "l.instagram.com", "l.messenger.com", "l.threads.net"):
        target = _query_param(p.query, "u")
    elif host.endswith(".safelinks.protection.outlook.com"):
        target = _query_param(p.query, "url")
    elif host == "out.reddit.com":
        target = _query_param(p.query, "url")
    elif _host_match(host, "youtube.com") and path == "/redirect":
        target = _query_param(p.query, "q")
    elif _host_match(host, "duckduckgo.com") and path.startswith("/l/"):
        target = _query_param(p.query, "uddg")
    elif host == "slack-redir.net":
        target = _query_param(p.query, "url")
    elif host == "steamcommunity.com" and path.startswith("/linkfilter"):
        target = _query_param(p.query, "u", "url")
    elif host == "t.umblr.com" and path == "/redirect":
        target = _query_param(p.query, "z")
    elif _host_match(host, "linkedin.com") and path.startswith("/redir/redirect"):
        target = _query_param(p.query, "url")
    elif host in ("vk.com", "m.vk.com") and path == "/away.php":
        target = _query_param(p.query, "to")
    elif host == "href.li":
        target = unquote(p.query)
    elif host == "urldefense.proofpoint.com" and path.startswith("/v2/url"):
        raw = _query_param(p.query, "u")
        target = _proofpoint_v2(raw) if raw else ""
    elif host == "urldefense.com" and path.startswith("/v3/__"):
        m = re.match(r"^/v3/__(.+?)__;", url[url.find("/v3/") :])
        if m and "*" not in m.group(1):
            target = m.group(1)
    target = target.strip()
    if target.lower().startswith(("http://", "https://")) and target != url:
        return target
    return None


def _is_tracking_param(key: str, host: str) -> bool:
    k = unquote(key.replace("+", " ")).strip().lower()
    if k.startswith("amp;"):  # "&amp;utm_source=" copied out of HTML
        k = k[4:]
    if k.startswith(_TRACKING_PREFIXES) or k in _TRACKING_EXACT:
        return True
    for suffix, names in _HOST_PARAMS.items():
        if _host_match(host, suffix) and k in names:
            return True
    return False


def _clean_fragment(fragment: str) -> str:
    frag = fragment.split(":~:", 1)[0]  # text fragments (#:~:text=...) are browser-only
    if re.match(r"(?i)^(?:xtor=|utm_|echobox=|at_medium=|ref=)", frag):
        return ""
    return frag


def clean_url(url: str) -> str:
    """Normalise a shared URL without fetching it.

    Unwraps known redirectors (Google /url and AMP, Facebook/Instagram l.php, Outlook
    SafeLinks, Proofpoint, out.reddit.com, DuckDuckGo, Slack, ...), removes tracking
    parameters (utm_*, fbclid, gclid, per-host ones such as x.com ``s``/``t``) while
    keeping every other parameter byte-for-byte and in order, drops text fragments,
    lowercases scheme and host and drops default ports. ``ref`` is never removed
    (GitHub uses it for branches). Bare ``www.``/``host.tld/...`` input gets https://.
    Non-http(s) input is returned stripped but otherwise unchanged.
    """
    u = (url or "").strip().strip("<>\"'`").strip()
    if not u:
        return ""
    if not re.match(r"(?i)^[a-z][a-z0-9+.-]*:", u):
        if re.match(r"(?i)^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?(?:[/?#]|$)", u):
            u = "https://" + u
        else:
            return u
    elif re.match(r"(?i)^[a-z0-9-]+(?:\.[a-z0-9-]+)+:\d+(?:[/?#]|$)", u):
        u = "https://" + u  # "example.com:8080/x" parses as scheme "example.com"
    for _ in range(5):
        try:
            inner = _unwrap_once(u)
        except ValueError:
            break
        if inner is None:
            break
        u = inner
    try:
        p = urlsplit(u)
        port = p.port
    except ValueError:
        return u
    scheme = p.scheme.lower()
    if scheme not in ("http", "https"):
        return u
    host = (p.hostname or "").lower()
    if not host:
        return u
    netloc = host if ":" not in host else f"[{host}]"
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    if p.username is not None:
        cred = p.username + (f":{p.password}" if p.password is not None else "")
        netloc = f"{cred}@{netloc}"
    pieces = [piece for piece in p.query.split("&") if piece]
    kept = [piece for piece in pieces if not _is_tracking_param(piece.split("=", 1)[0], host)]
    query = "&".join(kept)
    path = p.path
    if re.search(r"[^\x21-\x7e]", path):
        path = quote(path, safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit((scheme, netloc, path, query, _clean_fragment(p.fragment)))


# --------------------------------------------------------------------------------------
# Charset decoding
# --------------------------------------------------------------------------------------

# WHATWG Encoding Standard labels whose browser decoder differs from Python's codec of
# the same name, mapped to the Python codec that matches what browsers do.
_LABEL_MAP = {
    "iso-8859-1": "cp1252", "iso8859-1": "cp1252", "latin1": "cp1252", "latin-1": "cp1252",
    "l1": "cp1252", "us-ascii": "cp1252", "ascii": "cp1252", "ansi_x3.4-1968": "cp1252",
    "iso-8859-9": "cp1254", "latin5": "cp1254", "iso-8859-11": "cp874", "tis-620": "cp874",
    "shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932",
    "ms_kanji": "cp932", "windows-31j": "cp932", "csshiftjis": "cp932",
    "gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "gb_2312-80": "gb18030", "chinese": "gb18030",
    "big5": "big5hkscs", "big5-hkscs": "big5hkscs", "x-x-big5": "big5hkscs",
    "euc-kr": "cp949", "ks_c_5601-1987": "cp949", "korean": "cp949", "windows-949": "cp949",
    "utf-16": "utf-8", "utf-16le": "utf-8", "utf-16be": "utf-8", "unicode": "utf-8",
    "ucs-2": "utf-8", "unicode-1-1-utf-8": "utf-8", "utf8": "utf-8",
    "x-user-defined": "cp1252", "koi8-ru": "koi8_u", "iso-8859-8-i": "iso8859-8",
    "x-mac-roman": "mac_roman", "macintosh": "mac_roman", "x-mac-cyrillic": "mac_cyrillic",
    "x-cp1251": "cp1251", "x-cp1250": "cp1250",
}  # fmt: skip
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+?charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)
_XML_DECL_RE = re.compile(rb"""^\s*<\?xml[^>]+encoding\s*=\s*["']([A-Za-z0-9_.:-]+)["']""", re.I)
_MULTIBYTE_RE = re.compile(rb"[\xc2-\xf4][\x80-\xbf]")
_CP1252_UNDEFINED_RE = re.compile(rb"[\x81\x8d\x8f\x90\x9d]")
_BOMS = ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))
_SHORT_INPUT = 4096


def normalize_charset(label: str | None) -> str | None:
    """Map a charset label to the Python codec name browsers would effectively use."""
    if not label:
        return None
    n = label.strip().strip("\"'").lower()
    n = _LABEL_MAP.get(n, n)
    try:
        return codecs.lookup(n).name
    except LookupError:
        return None


def _try_decode(raw: bytes, enc: str) -> str | None:
    try:
        return raw.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return None


def _decode_mostly(raw: bytes, enc: str) -> str | None:
    """Decode with replacement if only a few bytes are invalid (what a browser shows)."""
    try:
        text = raw.decode(enc, "replace")
    except LookupError:
        return None
    if text.count("\ufffd") > max(2, len(text) // 1000):
        return None  # far too many errors: the label is wrong
    if enc == "cp1252":
        # the 5 bytes cp1252 leaves undefined are invisible C1 controls in browsers
        text = text.replace("\ufffd", "")
    return text


def decode_bytes(body: bytes, charset_hint: str | None = None, *, html: bool = True) -> str:
    """Decode a fetched body the way a browser would, with fixes for lying labels.

    Order: BOM; HTTP charset; ``<meta charset>``/``<?xml encoding?>`` in the first
    8 KB (HTML only); strict UTF-8; cp1252 for short Western input (statistical
    detectors misread it); charset-normalizer; cp1252 with replacement. A single-byte
    label on bytes that are valid multi-byte UTF-8 is overridden by UTF-8.
    """
    if not body:
        return ""
    for bom, enc in _BOMS:
        if body.startswith(bom):
            return body[len(bom) :].decode(enc, "replace")
    candidates: list[str] = []
    hint = normalize_charset(charset_hint)
    if hint:
        candidates.append(hint)
    if html:
        head = body[:8192]
        m = _META_CHARSET_RE.search(head) or _XML_DECL_RE.search(head)
        if m:
            meta = normalize_charset(m.group(1).decode("ascii", "ignore"))
            if meta and meta not in candidates:
                candidates.append(meta)
    utf8 = _try_decode(body, "utf-8")
    for enc in candidates:
        if enc == "utf-8" and utf8 is not None:
            return utf8
        if enc != "utf-8" and utf8 is not None and _MULTIBYTE_RE.search(body):
            return utf8  # lying legacy label on real UTF-8 bytes
        text = _try_decode(body, enc)
        if text is None:
            text = _decode_mostly(body, enc)  # a few broken bytes under a correct label
        if text is not None:
            return text
    if utf8 is not None:
        return utf8
    if len(body) < _SHORT_INPUT and not _CP1252_UNDEFINED_RE.search(body):
        return body.decode("cp1252")
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(body).best()
    except Exception:  # pragma: no cover - detector failure is not fatal
        best = None
    if best is not None:
        return str(best)
    return body.decode("cp1252", "replace")


def decode_html(body: bytes, charset_hint: str | None) -> str:
    """Decode an HTML body (see :func:`decode_bytes`)."""
    return decode_bytes(body, charset_hint, html=True)
