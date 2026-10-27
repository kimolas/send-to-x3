"""RFC 822 email -> IngestRequests, plus the acceptance filter (ARCHITECTURE §9, §14).

``parse_email`` turns one raw message into zero or more :class:`IngestRequest`:

- the body, as ``text`` (plain part) or ``html`` (HTML part), after removing forward
  banners and their header blocks, signatures, mobile "Sent from" lines and newsletter
  chrome ("View in browser", "Unsubscribe", hidden preheaders, tracking images; text
  chrome only in html.py's strict forms and near either end of the body);
- a body that is just links (one to ten URLs plus at most 30 other words) is a link
  share, as for a text-only HTTP share: each link becomes ``url``, or, when the sender
  added a few words, ``auto`` text "<words> <link>" (the Processor fetches the article
  and keeps the words as a comment). The "Send to X3" Shortcut's ``[x3]`` emails carry
  its url/text/title and are handed on as ``auto`` with an explicit ``url``, exactly
  like its HTTP request;
- each PDF attachment becomes ``pdf``; ``.txt``/``.md`` attachments ``text``; ``.html``
  attachments ``html``; ``.epub`` attachments pass through as files (``kind="auto"``
  with ``data``); messages forwarded as attachments (``message/rfc822``) are
  parsed recursively. Images, signatures (S/MIME, PGP), calendars, vCards and other
  binaries are skipped, and identical parts are only delivered once.

``accept_email`` is the security gate the IMAP worker applies before anything is
queued: size cap, secret plus-tag, sender allowlist (at least one of the two must be
set) and (when ``require_dmarc``) the topmost ``Authentication-Results`` header.

Only the stdlib ``email`` package (``policy.default``), lxml and html.py's chrome
rules (:func:`sendtox3.extract.html.is_mail_chrome`) are used. Text is not
cleaned for the device here (the text/HTML extractors do that); this module only fixes
what is specific to email: charsets, transfer encodings, format=flowed, CRLF.
"""

from __future__ import annotations

import codecs
import email
import email.header
import email.policy
import email.utils
import hashlib
import html as _html
import logging
import re
from dataclasses import dataclass, field
from email.message import Message
from email.utils import getaddresses
from pathlib import PurePosixPath, PureWindowsPath
from collections.abc import Iterable
from typing import Optional

import lxml.html
from lxml import etree

from ..config import Options
from ..models import IngestRequest
from ..textmodel import clean_text
from .html import MAIL_CHROME_EDGE, is_mail_chrome, is_mail_chrome_link

log = logging.getLogger(__name__)

__all__ = [
    "MAX_EMAIL_BYTES",
    "ParsedEmail",
    "AuthResult",
    "AuthResults",
    "parse_email",
    "accept_email",
    "parse_auth_results",
    "addressed_to_tag",
    "normalize_plus_tag",
    "sender_allowed",
    "has_sender_filter",
    "sender_filter_problem",
    "watches_personal_inbox",
    "NO_SENDER_FILTER",
    "WILDCARD_IN_INBOX",
    "trusted_authserv_ids",
    "mailbox",
    "clean_subject",
    "unflow",
    "decode_text_bytes",
    "organizational_domain",
    "domains_aligned",
]

MAX_EMAIL_BYTES = 25 * 1024 * 1024  # §9 size cap (raw RFC 822 size)
# Links plus a comment of at most this many words / characters are a link share, not a
# note: the same limits as ingest.COMMENT_MAX_WORDS / COMMENT_MAX_CHARS for HTTP shares.
URL_ONLY_MAX_WORDS = 30
URL_ONLY_MAX_CHARS = 300
MAX_URLS_PER_EMAIL = 10  # a body listing more links than this is treated as text
MAX_NESTING = 3  # message/rfc822 inside message/rfc822 ...
SHORT_NOTE_WORDS = 12  # a preamble this short above a forward ("FYI", "see below") is dropped
MAX_REQUESTS_PER_EMAIL = 50  # an email with hundreds of attachments must not flood the queue

# Recipient headers that carry the envelope address (Bcc and plus-addressing show up here).
_ENVELOPE_HEADERS = ("delivered-to", "x-original-to", "x-delivered-to", "envelope-to", "x-envelope-to")


# ======================================================================================
# Public data
# ======================================================================================


@dataclass
class AuthResult:
    """One ``method=result`` clause of an Authentication-Results header (RFC 8601)."""

    method: str  # "dmarc", "dkim", "spf", ...
    result: str  # "pass", "fail", "none", ...
    props: dict[str, str] = field(default_factory=dict)  # "header.from" -> "example.com"


@dataclass
class AuthResults:
    authserv_id: str = ""  # lowercased; "" when the header has none (some MTAs omit it)
    results: list[AuthResult] = field(default_factory=list)


@dataclass
class ParsedEmail:
    subject: str = ""  # cleaned: Re:/Fwd:/[tag] prefixes removed, single line
    from_addr: str = ""  # lowercased address of the first From mailbox
    to_addrs: list[str] = field(default_factory=list)  # To + Cc, lowercased
    message_id: str = ""
    auth_results: str = ""  # topmost Authentication-Results value, unfolded
    dmarc_pass: bool = False  # topmost A-R: dmarc=pass for the From domain
    dkim_aligned_pass: bool = False  # topmost A-R: dkim=pass with d= aligned to From
    requests: list[IngestRequest] = field(default_factory=list)
    # ---- extras (not part of the §14 contract, used by accept_email and for logs)
    raw_subject: str = ""
    from_name: str = ""
    from_count: int = 0  # mailboxes in From: DMARC requires exactly one
    delivered_to: list[str] = field(default_factory=list)  # Delivered-To, X-Original-To, ...
    authserv_id: str = ""
    dmarc_result: str = ""  # "pass" / "fail" / "none" / ... or "" when not reported
    auth_summary: str = ""  # short human summary for logs
    size: int = 0  # raw message size in bytes
    headers_only: bool = False  # parsed with headers_only=True (no requests computed)
    notes: list[str] = field(default_factory=list)  # skipped parts etc., for logs

    @property
    def from_domain(self) -> str:
        return self.from_addr.rpartition("@")[2]

    @property
    def sender(self) -> str:
        """The From mailbox as used in ``IngestRequest.sender``."""
        return mailbox(self.from_name, self.from_addr)


# ======================================================================================
# Entry points
# ======================================================================================


def parse_email(raw: bytes, *, headers_only: bool = False) -> ParsedEmail:
    """Parse one RFC 822 message. Never raises for malformed input.

    With ``headers_only`` (or when the message exceeds :data:`MAX_EMAIL_BYTES`) only
    the headers are looked at and ``requests`` stays empty; the IMAP worker uses this
    to reject unwanted mail before downloading bodies.
    """
    raw = raw or b""
    p = ParsedEmail(size=len(raw))
    too_big = len(raw) > MAX_EMAIL_BYTES
    p.headers_only = headers_only or too_big
    try:
        if p.headers_only:
            msg = email.message_from_bytes(_header_block(raw), policy=email.policy.default)
        else:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception as e:  # the stdlib parser is lenient; this is belt and braces
        log.warning("email: unparseable message (%s)", e)
        p.notes.append(f"unparseable message: {e}")
        return p
    _fill_headers(p, msg)
    if p.headers_only:
        if too_big:
            p.notes.append(f"message is {len(raw) / 1_000_000:.1f} MB, over the {MAX_EMAIL_BYTES // 2**20} MB cap")
        return p
    try:
        reqs = _message_requests(msg, depth=0, notes=p.notes)
    except Exception as e:  # never let one odd message stop the worker
        log.exception("email: failed to extract content from %s", p.message_id or "message")
        p.notes.append(f"content extraction failed: {e}")
        reqs = []
    p.requests = _dedupe(reqs)
    if len(p.requests) > MAX_REQUESTS_PER_EMAIL:
        p.notes.append(f"kept the first {MAX_REQUESTS_PER_EMAIL} of {len(p.requests)} parts")
        p.requests = p.requests[:MAX_REQUESTS_PER_EMAIL]
    return p


def accept_email(p: ParsedEmail, opts: Options) -> tuple[bool, str]:
    """Security gate (§9). Returns ``(ok, reason)``; the reason is meant for logs.

    Checks, in order: size cap, secret plus-tag, sender allowlist, DMARC/aligned DKIM
    from the topmost Authentication-Results (when ``require_dmarc``), then whether
    anything readable was found. A ``headers_only`` parse skips the content check.
    Fails closed: with neither a plus-tag nor ``allowed_senders`` nothing is accepted
    (``allowed_senders: ["*"]`` accepts any sender that passes the other checks, but only
    with a plus-tag or an ``imap_folder`` other than INBOX: see
    :func:`sender_filter_problem`).
    """
    who = p.from_addr or "(no From address)"
    if p.size > MAX_EMAIL_BYTES:
        return False, f"rejected: message from {who} is {p.size / 1_000_000:.1f} MB (cap {MAX_EMAIL_BYTES // 2**20} MB)"

    tag = normalize_plus_tag(opts.imap_plus_tag)
    if tag and not _has_plus_tag(p, tag):
        return False, f"rejected: message from {who} is not addressed to the secret +tag address"

    if not sender_allowed(p, opts):
        if problem := sender_filter_problem(opts):
            return False, f"rejected: message from {who}: {problem}"
        return False, f"rejected: sender {who} is not in allowed_senders"

    auth_note = ""
    if opts.require_dmarc:
        ok, auth_note = _auth_ok(p, opts)
        if not ok:
            return False, f"rejected: message from {who}: {auth_note}"

    if not p.headers_only and not p.requests:
        return False, f"rejected: message from {who} has no readable content (text, link, PDF, .txt or .html)"

    detail = f" ({auth_note})" if auth_note else ""
    return True, f"accepted: message from {who}{detail}"


# ======================================================================================
# Headers
# ======================================================================================


def _header_block(raw: bytes) -> bytes:
    """The header section of a raw message (up to and including the blank line)."""
    m = re.search(rb"\r?\n\r?\n", raw)
    return raw[: m.end()] if m else raw


def _fix_surrogates(s: str) -> str:
    """Repair raw 8-bit header bytes that the parser kept as surrogate escapes."""
    if not any("\udc80" <= ch <= "\udcff" for ch in s):
        return s
    b = s.encode("utf-8", "surrogateescape")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp1252", "replace")


def _header_values(msg: Message, name: str) -> list[str]:
    """All decoded values of header ``name`` (top first). Never raises."""
    out: list[str] = []
    try:
        values = msg.get_all(name) or []
        for v in values:
            out.append(_fix_surrogates(str(v)))
        return out
    except Exception:
        out = []
    # Fallback: undecoded raw values, RFC 2047 decoded by hand.
    for k, v in msg.raw_items():
        if k.lower() != name.lower():
            continue
        v = re.sub(r"\r?\n(?=[ \t])", "", str(v))
        try:
            v = str(email.header.make_header(email.header.decode_header(v)))
        except Exception:
            pass
        out.append(_fix_surrogates(v))
    return out


def _header(msg: Message, name: str) -> str:
    vals = _header_values(msg, name)
    return vals[0] if vals else ""


def _unfold(v: str) -> str:
    return re.sub(r"\s+", " ", v.replace("\r", " ").replace("\n", " ")).strip()


def _addresses(values: Iterable[str]) -> list[tuple[str, str]]:
    """(display name, lowercased address) pairs; malformed entries are dropped."""
    out: list[tuple[str, str]] = []
    for v in values:
        try:
            pairs = getaddresses([_unfold(v)])
        except Exception:
            pairs = []
        for name, addr in pairs:
            addr = (addr or "").strip().strip("<>").lower()
            if "@" not in addr:
                continue
            out.append((_fix_surrogates(name or "").strip().strip('"').strip(), addr))
    return out


def _fill_headers(p: ParsedEmail, msg: Message) -> None:
    p.raw_subject = _unfold(_header(msg, "subject"))
    p.subject = clean_subject(p.raw_subject)
    froms = _addresses(_header_values(msg, "from"))
    p.from_count = len(froms)
    if froms:
        name, addr = froms[0]
        p.from_addr = addr
        p.from_name = clean_text(name) if name and name.lower() != addr else ""
    p.to_addrs = _dedupe_strs(a for _, a in _addresses(_header_values(msg, "to") + _header_values(msg, "cc")))
    env: list[str] = []
    for h in _ENVELOPE_HEADERS:
        env.extend(a for _, a in _addresses(_header_values(msg, h)))
    p.delivered_to = _dedupe_strs(env)
    p.message_id = _unfold(_header(msg, "message-id"))

    ar_values = _header_values(msg, "authentication-results")
    if ar_values:
        p.auth_results = _unfold(ar_values[0])  # topmost only: lower ones can be forged by the sender
        ar = parse_auth_results(p.auth_results)
        p.authserv_id = ar.authserv_id
        _evaluate_auth(p, ar)
    else:
        p.auth_summary = "no Authentication-Results"


def _dedupe_strs(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


# ======================================================================================
# Subject
# ======================================================================================

# Reply/forward prefixes in common mail clients and languages (Re, Fwd, AW/WG German,
# SV/VS Nordic, TR French, RV/ENC/RES Spanish/Portuguese, ODP/PD Polish, Antw Dutch).
_SUBJ_PREFIX_RE = re.compile(
    r"^\s*(?:re|fwd?|fw|aw|wg|sv|vs|tr|rv|enc|res|odp|pd|antw|antwort|vb|doorst)"
    r"\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*",
    re.I,
)
_SUBJ_TAG_RE = re.compile(r"^\s*\[[^\[\]\n]{1,40}\]\s*")
_SUBJ_FWD_SUFFIX_RE = re.compile(r"\s*\((?:fwd|fw)\)\s*$", re.I)


def clean_subject(subject: str) -> str:
    """'Fwd: RE: [x3] Title' -> 'Title'. Removes reply/forward prefixes and leading
    bracketed tags (the Shortcut's ``[x3]``, mailing-list tags) in any order."""
    s = _unfold(subject or "")
    for _ in range(12):
        before = s
        s = _SUBJ_PREFIX_RE.sub("", s)
        s = _SUBJ_TAG_RE.sub("", s)
        s = _SUBJ_FWD_SUFFIX_RE.sub("", s)
        if s == before:
            break
    return clean_text(s)


# ======================================================================================
# Authentication-Results (RFC 8601), alignment
# ======================================================================================


def _strip_comments(s: str) -> str:
    """Remove RFC 5322 comments ``( ... )`` (nested, with quoted-pairs), keep quoted strings."""
    out: list[str] = []
    depth = 0
    in_quote = False
    i = 0
    while i < len(s):
        ch = s[i]
        if in_quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(s):
                out.append(s[i + 1])
                i += 2
                continue
            if ch == '"':
                in_quote = False
        elif depth:
            if ch == "\\":
                i += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        elif ch == '"':
            in_quote = True
            out.append(ch)
        elif ch == "(":
            depth = 1
            out.append(" ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _split_semicolons(s: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    in_quote = False
    for ch in s:
        if ch == '"':
            in_quote = not in_quote
        if ch == ";" and not in_quote:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts]


_KV_RE = re.compile(r'([A-Za-z0-9_.\-/]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s;]+)')


def parse_auth_results(value: str) -> AuthResults:
    """Parse one Authentication-Results header value."""
    s = _strip_comments(_unfold(value or ""))
    parts = _split_semicolons(s)
    if not parts:
        return AuthResults()
    first = parts[0]
    ar = AuthResults()
    if "=" in first.split(" ", 1)[0]:
        # No authserv-id (some MTAs, e.g. Microsoft 365, start straight with a result).
        clauses = parts
    else:
        ar.authserv_id = (first.split() or [""])[0].lower()
        clauses = parts[1:]
    for clause in clauses:
        pairs = _KV_RE.findall(clause)
        if not pairs:
            continue  # "none" or garbage
        method, result = pairs[0]
        method = method.split("/", 1)[0].lower()  # "dkim/1" -> "dkim"
        res = AuthResult(method=method, result=result.strip('"').lower())
        for k, v in pairs[1:]:
            res.props[k.lower()] = v.strip('"')
        ar.results.append(res)
    return ar


def _dkim_domain(r: AuthResult) -> str:
    d = r.props.get("header.d", "")
    if not d:
        i = r.props.get("header.i", "")
        d = i.rpartition("@")[2] if i else ""
    return d.strip().strip(".").lower()


def _evaluate_auth(p: ParsedEmail, ar: AuthResults) -> None:
    from_domain = p.from_domain
    summary: list[str] = []
    for r in ar.results:
        if r.method == "dmarc":
            hf = r.props.get("header.from", "").strip().lower()
            if not p.dmarc_result or r.result == "pass":
                p.dmarc_result = r.result
            if r.result == "pass" and from_domain and (not hf or hf == from_domain):
                p.dmarc_pass = True
            summary.append(f"dmarc={r.result}" + (f"({hf})" if hf else ""))
        elif r.method == "dkim":
            d = _dkim_domain(r)
            if r.result == "pass" and from_domain and d and domains_aligned(d, from_domain):
                p.dkim_aligned_pass = True
            summary.append(f"dkim={r.result}" + (f"({d})" if d else ""))
        elif r.method == "spf":
            summary.append(f"spf={r.result}")
    who = ar.authserv_id or "unnamed server"
    p.auth_summary = (" ".join(summary) or "no results") + f" by {who}"


# Multi-label public suffixes common in mail (a compact stand-in for the Public Suffix
# List) plus shared-hosting suffixes where every subdomain belongs to someone else.
_MULTI_LABEL_SUFFIXES = frozenset(
    """
    co.uk org.uk me.uk ltd.uk plc.uk ac.uk gov.uk nhs.uk sch.uk net.uk
    com.au net.au org.au edu.au gov.au asn.au id.au co.nz org.nz net.nz ac.nz govt.nz
    co.jp ne.jp or.jp ac.jp go.jp ad.jp ed.jp gr.jp lg.jp co.kr or.kr ne.kr ac.kr go.kr
    com.cn net.cn org.cn gov.cn edu.cn com.hk org.hk net.hk edu.hk com.tw org.tw net.tw edu.tw
    com.sg org.sg net.sg edu.sg gov.sg com.my net.my org.my co.th in.th ac.th co.id or.id ac.id
    co.in net.in org.in firm.in gen.in ind.in ac.in edu.in gov.in res.in com.pk com.bd com.vn com.ph
    com.br net.br org.br gov.br edu.br com.ar com.mx org.mx gob.mx com.co com.pe com.ve com.uy com.ec
    co.za org.za net.za gov.za ac.za co.ke co.ug co.tz com.ng com.eg com.tr org.tr net.tr gen.tr
    co.il org.il ac.il gov.il com.sa com.ua kiev.ua com.ru org.ru net.ru msk.ru spb.ru com.pl net.pl org.pl
    co.at or.at ac.at gv.at com.es org.es nom.es com.pt com.gr com.cy com.mt co.hu
    github.io gitlab.io herokuapp.com blogspot.com netlify.app vercel.app pages.dev workers.dev
    web.app firebaseapp.com appspot.com azurewebsites.net cloudfront.net wordpress.com
    """.split()
)
_GENERIC_SECOND_LEVEL = frozenset("co com net org gov edu ac ne or go gob mil nic ltd plc sch".split())


def organizational_domain(domain: str) -> str:
    """Approximate DMARC organizational domain ('mail.example.co.uk' -> 'example.co.uk')."""
    labels = [x for x in (domain or "").strip().strip(".").lower().split(".") if x]
    if len(labels) <= 2:
        return ".".join(labels)
    last2 = ".".join(labels[-2:])
    if last2 in _MULTI_LABEL_SUFFIXES or (len(labels[-1]) == 2 and labels[-2] in _GENERIC_SECOND_LEVEL):
        return ".".join(labels[-3:])
    return last2


def domains_aligned(signing_domain: str, from_domain: str) -> bool:
    """DMARC relaxed alignment: same organizational domain (or one is a parent of the other)."""
    a = (signing_domain or "").strip().strip(".").lower()
    b = (from_domain or "").strip().strip(".").lower()
    if not a or not b:
        return False
    if a == b or a.endswith("." + b) or b.endswith("." + a):
        return True
    oa, ob = organizational_domain(a), organizational_domain(b)
    return bool(oa) and oa == ob and "." in oa


# authserv-ids we know for a mailbox provider, keyed by the IMAP host's organizational
# domain. When known, the topmost Authentication-Results must come from that server.
# An entry matches the id itself or any subdomain ("messagingengine.com" matches
# "mx3.messagingengine.com"); "" means the provider writes its header without an
# authserv-id (Microsoft 365 / Outlook.com: "spf=pass ...; dmarc=pass ...").
# Not listed: iCloud (one header per method, so the topmost one may carry no DMARC
# result) and self-hosted servers, whose own header is the topmost one only when the
# server adds one to every message.
_KNOWN_AUTHSERV_IDS: dict[str, frozenset[str]] = {
    "gmail.com": frozenset({"mx.google.com"}),
    "googlemail.com": frozenset({"mx.google.com"}),
    "fastmail.com": frozenset({"messagingengine.com"}),
    "yahoo.com": frozenset({"yahoo.com"}),
    "office365.com": frozenset({""}),
    "outlook.com": frozenset({""}),
}


def trusted_authserv_ids(opts: Options) -> Optional[frozenset[str]]:
    """authserv-ids the topmost Authentication-Results must carry for ``imap_host``, or
    None when the provider is not known (then the topmost header is taken as it is)."""
    return _KNOWN_AUTHSERV_IDS.get(organizational_domain(opts.imap_host or ""))


def _authserv_trusted(authserv_id: str, trusted: frozenset[str]) -> bool:
    a = (authserv_id or "").strip().strip(".").lower()
    for t in trusted:
        if not t:
            if not a:
                return True
        elif a == t or a.endswith("." + t):
            return True
    return False


def _auth_ok(p: ParsedEmail, opts: Options) -> tuple[bool, str]:
    if p.from_count != 1:
        return False, f"{p.from_count} From addresses (DMARC needs exactly one)"
    if not p.auth_results:
        return False, "no Authentication-Results header (require_dmarc is on)"
    expected = trusted_authserv_ids(opts)
    if expected is not None and not _authserv_trusted(p.authserv_id, expected):
        want = ", ".join(sorted(t or "a header without authserv-id" for t in expected))
        return False, (
            f"topmost Authentication-Results is from '{p.authserv_id or '(no authserv-id)'}', expected {want}"
        )
    if p.dmarc_result == "fail" and not p.dmarc_pass:
        return False, f"DMARC failed for {p.from_domain} ({p.auth_summary})"
    if p.dmarc_pass:
        return True, f"dmarc=pass by {p.authserv_id or 'unnamed server'}"
    if p.dkim_aligned_pass:
        return True, f"aligned dkim=pass by {p.authserv_id or 'unnamed server'}"
    return False, f"no DMARC pass or aligned DKIM pass for {p.from_domain or '?'} ({p.auth_summary})"


# ======================================================================================
# Acceptance helpers
# ======================================================================================


def normalize_plus_tag(tag: str) -> str:
    """The ``imap_plus_tag`` option as compared ("" = no tag filter)."""
    t = (tag or "").strip().lower()
    if "@" in t:  # someone pasted the whole address: keep the part between '+' and '@'
        local = t.rpartition("@")[0]
        t = local.partition("+")[2] if "+" in local else local
    return t.lstrip("+").strip()


def addressed_to_tag(p: ParsedEmail, tag: str) -> bool:
    """True if a recipient (To, Cc, Delivered-To, X-Original-To, ...) is ``<user>+<tag>@...``."""
    t = normalize_plus_tag(tag)
    return bool(t) and _has_plus_tag(p, t)


def _allowed_entries(opts: Options) -> list[str]:
    senders = opts.allowed_senders or []
    if isinstance(senders, str):
        senders = senders.split(",")
    return [s for s in (_normalize_allow_entry(a) for a in senders) if s]


#: Why nothing is accepted while neither filter is set (fail closed: otherwise anyone
#: whose mail passes DMARC, which any free mail account does, could fill the queue).
NO_SENDER_FILTER = (
    "allowed_senders and imap_plus_tag are both empty, so no mail is accepted: list the addresses you send "
    "from in allowed_senders ('*' accepts any sender) and/or set imap_plus_tag"
)


#: Why ``allowed_senders: ['*']`` accepts nothing in the user's own INBOX without a tag
#: (every message there that passes DMARC would become an item and be marked read).
WILDCARD_IN_INBOX = (
    "allowed_senders is '*' (any sender) but the app watches INBOX without imap_plus_tag, so every message "
    "there that passes DMARC would become an item and be marked read; no mail is accepted: set "
    "imap_plus_tag, set imap_folder to a folder or label that only your shares go to, or list the "
    "addresses you send from in allowed_senders instead of '*'"
)


def has_sender_filter(opts: Options) -> bool:
    """True when ``allowed_senders`` (``*`` included) or ``imap_plus_tag`` is set.
    Without either, :func:`accept_email` accepts nothing (see also
    :func:`sender_filter_problem`)."""
    return bool(_allowed_entries(opts) or normalize_plus_tag(opts.imap_plus_tag))


def watches_personal_inbox(opts: Options) -> bool:
    """``imap_folder`` is INBOX (any case: RFC 3501) and no ``imap_plus_tag`` is set: the
    user's own mailbox, not a folder, label or +tag address only shares go to."""
    folder = (opts.imap_folder or "").strip() or "INBOX"
    return folder.upper() == "INBOX" and not normalize_plus_tag(opts.imap_plus_tag)


def sender_filter_problem(opts: Options) -> str:
    """Why the sender settings accept no mail at all, or ``""`` when they are usable:
    neither ``allowed_senders`` nor ``imap_plus_tag`` set (:data:`NO_SENDER_FILTER`), or
    ``allowed_senders: ['*']`` in an untagged INBOX (:data:`WILDCARD_IN_INBOX`). The IMAP
    worker does not start then, and :func:`accept_email` rejects everything."""
    if not has_sender_filter(opts):
        return NO_SENDER_FILTER
    if "*" in _allowed_entries(opts) and watches_personal_inbox(opts):
        return WILDCARD_IN_INBOX
    return ""


def sender_allowed(p: ParsedEmail, opts: Options) -> bool:
    """The sender part of :func:`accept_email`: From is in ``allowed_senders`` (``*`` =
    anyone, but only with a plus-tag or a dedicated folder). With an empty list the
    secret plus-tag is the gate; with neither set, nobody is allowed."""
    allowed = _allowed_entries(opts)
    if "*" in allowed:
        return not watches_personal_inbox(opts)
    if not allowed:
        return bool(normalize_plus_tag(opts.imap_plus_tag))
    return _sender_allowed(p.from_addr, allowed)


def _has_plus_tag(p: ParsedEmail, tag: str) -> bool:
    for addr in list(p.to_addrs) + list(p.delivered_to):
        local = addr.rpartition("@")[0]
        user, plus, sub = local.partition("+")
        if plus and user and tag in sub.lower().split("+"):
            return True
    return False


def _normalize_allow_entry(entry: str) -> str:
    e = (entry or "").strip().strip("<>").strip().lower()
    if e == "*":
        return e  # any sender
    if e and "@" not in e:
        e = "@" + e  # a bare domain means the whole domain
    return e


def _sender_allowed(addr: str, allowed: list[str]) -> bool:
    addr = (addr or "").lower()
    if "@" not in addr:
        return False
    domain = addr.rpartition("@")[2]
    for e in allowed:
        if e.startswith("@"):
            if domain == e[1:]:
                return True
        elif addr == e:
            return True
    return False


# ======================================================================================
# Charset decoding
# ======================================================================================

_CHARSET_ALIASES = {
    # Browsers decode these labels as cp1252; mail clients mislabel the same way.
    "us-ascii": "cp1252",
    "ascii": "cp1252",
    "ansi_x3.4-1968": "cp1252",
    "iso-8859-1": "cp1252",
    "iso8859-1": "cp1252",
    "iso_8859-1": "cp1252",
    "latin1": "cp1252",
    "latin-1": "cp1252",
    "l1": "cp1252",
    "windows-1252": "cp1252",
    "x-user-defined": "cp1252",
    "shift_jis": "cp932",
    "shift-jis": "cp932",
    "sjis": "cp932",
    "x-sjis": "cp932",
    "ms_kanji": "cp932",
    "windows-31j": "cp932",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "x-gbk": "gb18030",
    "big5": "big5hkscs",
    "euc-kr": "cp949",
    "ks_c_5601-1987": "cp949",
    "iso-8859-8-i": "iso-8859-8",
    "windows-874": "cp874",
    "tis-620": "cp874",
    "x-mac-roman": "mac_roman",
    "macintosh": "mac_roman",
    "utf8": "utf-8",
}
_UNKNOWN_CHARSETS = frozenset(
    {"unknown-8bit", "x-unknown", "unknown", "default", "binary", "8bit", "7bit", "none", "charset", "x-binaryenc"}
)
# Labels where "valid UTF-8 wins" must not be applied (stateful or non-ASCII-compatible).
_NO_UTF8_FIRST_RE = re.compile(r"^(?:iso-?2022|utf-?7|utf-?16|utf-?32|ucs|hz)", re.I)
_BOMS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _norm_charset(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    n = str(label).strip().strip("\"'").lower()
    if not n or n in _UNKNOWN_CHARSETS:
        return None
    n = _CHARSET_ALIASES.get(n, n)
    try:
        codecs.lookup(n)
    except LookupError:
        return None
    return n


def decode_text_bytes(raw: bytes, declared: Optional[str] = None) -> str:
    """Decode a text payload robustly.

    BOM > valid UTF-8 (fixes the common "iso-8859-1 label on UTF-8 bytes" lie and
    ``unknown-8bit``) > declared charset > cp1252 for short input > charset-normalizer.
    """
    if not raw:
        return ""
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            return raw[len(bom) :].decode(enc, "replace")
    label = _norm_charset(declared)
    if label and _NO_UTF8_FIRST_RE.match(label):
        try:
            return raw.decode(label)
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if label and label != "utf-8":
        try:
            return raw.decode(label)
        except (UnicodeDecodeError, LookupError):
            pass
    if label == "utf-8":
        # Declared UTF-8 with a few bad bytes: keep UTF-8 unless it is mostly garbage.
        text = raw.decode("utf-8", "replace")
        non_ascii = sum(1 for b in raw if b >= 0x80) or 1
        if text.count("�") * 3 < non_ascii:
            return text
    # charset-normalizer misreads short Western text ("Café crème" -> cp1250): cp1252 first.
    if len(raw) < 4096:
        return raw.decode("cp1252", "replace")
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best)
    except Exception:  # pragma: no cover - detector failure is not fatal
        pass
    return raw.decode("cp1252", "replace")


# "cafÃ©" / "â€”": UTF-8 bytes that were decoded as cp1252 somewhere upstream.
_MOJIBAKE_RE = re.compile("[ÂÃÅÆÇÐÑÒÓÔÕÖØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóô][\u0080-¿ŒœŠšŸŽžƒˆ˜–—‘-„†-•…‰‹›€™]")
_CP1252_REVERSE = {}
for _b in range(0x80, 0xA0):
    try:
        _CP1252_REVERSE[bytes([_b]).decode("cp1252")] = _b
    except UnicodeDecodeError:
        pass


def _repair_mojibake(s: str) -> str:
    """Undo one level of UTF-8-read-as-cp1252, only when that clearly helps."""
    hits = len(_MOJIBAKE_RE.findall(s))
    if hits < 1:
        return s
    out = bytearray()
    for ch in s:
        cp = ord(ch)
        if cp < 0x100:
            out.append(cp)
        elif ch in _CP1252_REVERSE:
            out.append(_CP1252_REVERSE[ch])
        else:
            return s  # a character cp1252 cannot have produced: not mojibake
    try:
        fixed = out.decode("utf-8")
    except UnicodeDecodeError:
        return s
    return fixed if len(_MOJIBAKE_RE.findall(fixed)) < hits else s


_CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _normalize_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _tidy_text(s: str) -> str:
    """CRLF -> LF, no control characters, no trailing spaces, at most one blank line."""
    s = _CTRL_RE.sub("", _normalize_newlines(s)).replace("\t", "    ")
    lines = [ln.rstrip() for ln in s.split("\n")]
    s = "\n".join(lines)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip("\n").strip()


def _part_bytes(part: Message) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if isinstance(payload, bytes):
        return payload
    p = part.get_payload()
    if isinstance(p, str):
        return p.encode("utf-8", "surrogateescape")
    return b""


def _part_charset(part: Message) -> Optional[str]:
    try:
        return part.get_content_charset()
    except Exception:
        return None


def _part_text(part: Message) -> str:
    """Decoded text of a text/* leaf: transfer encoding, charset, flowed, newlines."""
    raw = _part_bytes(part)
    charset = _part_charset(part)
    if part.get_content_subtype() == "html" and not _norm_charset(charset):
        charset = _meta_charset(raw) or charset
    text = _normalize_newlines(decode_text_bytes(raw, charset))
    text = _repair_mojibake(text)
    if part.get_content_type() == "text/plain" and str(part.get_param("format") or "").lower() == "flowed":
        text = unflow(text, delsp=str(part.get_param("delsp") or "").lower() == "yes")
    return text


_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:\-]+)""", re.I)
_XML_ENC_RE = re.compile(rb"""<\?xml[^>]+encoding\s*=\s*["']([A-Za-z0-9_.:\-]+)""", re.I)


def _meta_charset(raw: bytes) -> Optional[str]:
    head = raw[:4096]
    m = _META_CHARSET_RE.search(head) or _XML_ENC_RE.search(head[:256])
    return m.group(1).decode("ascii", "ignore") if m else None


# ======================================================================================
# format=flowed (RFC 3676)
# ======================================================================================


def unflow(text: str, delsp: bool = False) -> str:
    """Decode RFC 3676 format=flowed text.

    A line ending in a space continues on the next line (the space is removed when
    ``delsp``). Quote depth (leading '>') is kept as a "> " prefix; a change of depth
    ends a paragraph; one leading space after the quote marks is space-stuffing; the
    signature separator "-- " is never flowed. A flowed paragraph that runs into a
    blank line is ended there and the blank line kept (lenient: many senders do this).
    """
    out: list[str] = []
    para: list[str] = []
    para_depth = 0

    def prefix(depth: int) -> str:
        return (">" * depth + " ") if depth else ""

    def flush() -> None:
        if para:
            out.append((prefix(para_depth) + "".join(para)).rstrip())
            para.clear()

    for line in _normalize_newlines(text).split("\n"):
        depth = len(line) - len(line.lstrip(">"))
        content = line[depth:]
        if content.startswith(" "):
            content = content[1:]  # space-stuffing (or the space after quote marks)
        if para and depth != para_depth:
            flush()  # quote depth changed: improperly flowed, end the paragraph
        if content == "-- ":
            flush()
            out.append(prefix(depth) + "-- ")
            continue
        if content.endswith(" "):
            if not para:
                para_depth = depth
            para.append(content[:-1] if delsp else content)
            continue
        if para:
            if content == "":
                flush()
                out.append(prefix(depth).rstrip())
            else:
                para.append(content)
                flush()
        else:
            out.append((prefix(depth) + content).rstrip() if content else prefix(depth).rstrip())
    flush()
    return "\n".join(out)


# ======================================================================================
# Plain-text body cleanup: forwards, signatures, newsletter chrome
# ======================================================================================

_FWD_BANNER_RE = re.compile(
    r"""^[ \t]*(?:
        -{2,}\s*(?:Forwarded|Original)\s+(?:Message|E-?mail)\s*-{2,}
      | -{2,}\s*(?:Weitergeleitete\s+Nachricht|Urspr[üu]ngliche\s+Nachricht|Message\s+transf[ée]r[ée]
               |Message\s+d'origine|Mensaje\s+reenviado|Mensaje\s+original|Messaggio\s+inoltrato
               |Messaggio\s+originale|Doorgestuurd\s+bericht|Oorspronkelijk\s+bericht
               |Mensagem\s+encaminhada|Vidarebefordrat\s+meddelande)\s*-{2,}
      | Begin\s+forwarded\s+message\s*:
      | Anfang\s+der\s+weitergeleiteten\s+(?:Nachricht|E-?Mail)\s*:
      | D[ée]but\s+du\s+message\s+r[ée]exp[ée]di[ée]\s*:
      | Inicio\s+del\s+mensaje\s+reenviado\s*:
      | Inizio\s+messaggio\s+inoltrato\s*:
      | In[íi]cio\s+da\s+mensagem\s+(?:re)?encaminhada\s*:
      | Doorgestuurd\s+bericht\s*:
    )[ \t]*$""",
    re.I | re.X,
)
# Outlook draws a rule above the header block of a forward/reply (no banner text).
_RULE_LINE_RE = re.compile(r"^[ \t]*(?:_{10,}|-{10,}|={10,})[ \t]*$")

_HDR_KEYS = {
    "from": "from", "von": "from", "de": "from", "da": "from", "van": "from", "från": "from", "od": "from",
    "date": "date", "sent": "date", "datum": "date", "gesendet": "date", "envoyé": "date", "envoye": "date",
    "fecha": "date", "enviado": "date", "enviada": "date", "data": "date", "inviato": "date",
    "verzonden": "date", "skickat": "date",
    "subject": "subject", "betreff": "subject", "objet": "subject", "asunto": "subject",
    "oggetto": "subject", "assunto": "subject", "onderwerp": "subject", "ämne": "subject",
    "to": "to", "an": "to", "à": "to", "a": "to", "para": "to", "aan": "to", "till": "to", "do": "to",
    "cc": "cc", "kopie": "cc", "bcc": "bcc", "reply-to": "reply-to", "antwort an": "reply-to",
}  # fmt: skip
_HDR_LINE_RE = re.compile(
    r"^[ \t]*\*?(?P<key>[^\W\d_][^\W\d_ -]{0,10}(?:[ -][^\W\d_]{2,6})?)\*?[ \t]*:[ \t]*\*?[ \t]*(?P<val>.*)$"
)

_MOBILE_SIG_RE = re.compile(
    r"""^[ \t]*(?:
        Sent\s+from\s+my\s+\S.{0,40}
      | Sent\s+from\s+(?:Mail|Outlook|Yahoo\s+Mail|Gmail|AOL|Proton\s?Mail|my\s+mobile|Samsung)\b.{0,60}
      | Get\s+Outlook\s+for\s+(?:iOS|Android|Mac|Windows)\b.{0,80}
      | Sent\s+(?:via|with|using)\s+\S.{0,60}
      | Envoy[ée]\s+(?:de|depuis)\s+mon\s+\S.{0,40}
      | Von\s+meinem\s+\S.{0,40}\s+gesendet\.?
      | Gesendet\s+(?:von|mit)\s+(?:meinem\s+)?\S.{0,40}
      | Enviado\s+(?:desde|do|de)\s+(?:mi|meu|o\s+meu)\s+\S.{0,40}
      | Inviato\s+da\s+\S.{0,40}
      | Verstuurd\s+(?:vanaf|met)\s+(?:mijn\s+)?\S.{0,40}
      | Skickat\s+fr[åa]n\s+(?:min\s+)?\S.{0,40}
    )[ \t]*$""",
    re.I | re.X,
)
_SIG_SEP_RE = re.compile(r"^-- ?$")

# Newsletter chrome ("View in browser", "Unsubscribe | Manage preferences", "You are
# receiving this email because ..."): only html.py's strict forms (see
# :func:`sendtox3.extract.html.is_mail_chrome`), so that a sentence which merely mentions
# a subscription, receiving or a browser is never removed ("Please update your
# subscription by Friday"), and only near either end of the body or of a forwarded
# message: in the first _CHROME_HEAD / last _CHROME_TAIL blocks (HTML) or paragraphs
# (plain text), a whole block, or one of its first/last lines.
_CHROME_HEAD, _CHROME_TAIL = MAIL_CHROME_EDGE
_CHROME_MAX_CHARS = 300  # a longer block (or the rest of one from a footer line on) is kept
_CHROME_MAX_WORDS = 60
_CHROME_LINE_MAX_CHARS = 250
# What else a short footer block holding an "Unsubscribe"-style link says ("You received
# this because you subscribed. <Unsubscribe>"); checked only next to such a link.
_FOOTER_LEAD_RE = re.compile(
    r"""(?ix)^[^\w]{0,10}(?:
        you(?:'re|’re|\s+are)?\s+(?:receiv(?:ed|ing)|getting)\s+this\b
      | this\s+(?:e-?mail|message|newsletter)\s+(?:was|is)\s+sent\b
      | if\s+you\b[^.?!]{0,40}\b(?:receiv|subscri|e-?mails?\b)
      | to\s+stop\s+receiving\b
      | (?:want\s+to\s+change|(?:no\s+longer|not)\s+interested|why\s+did\s+I\s+get)\b
      | (?:copyright\b|©)
    )"""
)
_STUB_PLAIN_RE = re.compile(
    r"(?i)(html[- ]capable|does(?:n't| not) support html|enable html|"
    r"view\s+(?:this|it)\s+(?:e-?mail|message)\s+in\s+(?:a|your)\s+(?:web\s*)?browser|"
    r"this is a multi-part message in mime format|requires? an? html)"
)

_URL_RE = re.compile(r"""(?i)\b(?:https?://|www\.)[^\s<>"'`{}|\\^\[\]]+""")
_ANGLE_URL_RE = re.compile(r"(?i)<\s*(?:URL:)?\s*(https?://[^<>]{1,2000}?)\s*>")
_LINK_ANNOTATION_RE = re.compile(
    r"(?i)(?<=\S)[ \t]*(?:<\s*https?://[^\s<>]+\s*>|\[\s*https?://[^\s\]]+\s*\]|\(\s*https?://[^\s)]+\s*\))"
)
_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


@dataclass
class _ForwardInfo:
    from_name: str = ""
    from_addr: str = ""
    subject: str = ""


def _forward_info(headers: dict[str, str]) -> _ForwardInfo:
    info = _ForwardInfo(subject=clean_subject(headers.get("subject", "")))
    frm = headers.get("from", "")
    if frm:
        pairs = _addresses([frm])
        if pairs:
            info.from_name = clean_text(pairs[0][0])
            info.from_addr = pairs[0][1]
        else:  # "Alice Example" without an address, or "Alice [mailto:...]"
            info.from_name = clean_text(re.sub(r"\[mailto:[^\]]*\]|<[^>]*>", "", frm))
    return info


def _parse_header_lines(lines: list[str], start: int) -> tuple[dict[str, str], int]:
    """Parse a forwarded-message header block starting at ``start``; returns (headers, end)."""
    headers: dict[str, str] = {}
    i = start
    last_key = ""
    while i < len(lines):
        line = lines[i]
        m = _HDR_LINE_RE.match(line)
        key = _HDR_KEYS.get(m.group("key").strip().lower()) if m else None
        if m and key:
            val = m.group("val").strip().strip("*").strip()
            headers.setdefault(key, val)
            last_key = key
            i += 1
            continue
        if last_key and line[:1] in (" ", "\t") and line.strip():
            headers[last_key] = (headers[last_key] + " " + line.strip()).strip()  # wrapped value
            i += 1
            continue
        break
    return headers, i


def _is_forward_header(headers: dict[str, str], has_banner: bool) -> bool:
    if has_banner:
        return True
    others = sum(1 for k in ("date", "to", "subject", "cc") if k in headers)
    return "from" in headers and others >= 2


def _split_forwards_text(text: str) -> tuple[list[str], list[_ForwardInfo]]:
    """Split at forward banners / header blocks: ([preamble, fwd1, fwd2...], [info1, ...])."""
    lines = text.split("\n")
    segments: list[list[str]] = [[]]
    infos: list[_ForwardInfo] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        banner = bool(_FWD_BANNER_RE.match(line))
        rule = not banner and bool(_RULE_LINE_RE.match(line))
        prev_blank = not segments[-1] or not segments[-1][-1].strip()
        if banner or rule or (prev_blank and _HDR_LINE_RE.match(line)):
            j = i + 1 if (banner or rule) else i
            blanks = 0
            while j < len(lines) and not lines[j].strip() and blanks < 2:
                j += 1
                blanks += 1
            headers, end = _parse_header_lines(lines, j)
            if _is_forward_header(headers, banner):
                infos.append(_forward_info(headers))
                segments.append([])
                i = end
                continue
        segments[-1].append(line)
        i += 1
    return ["\n".join(s) for s in segments], infos


def _unquote_segment(seg: str) -> str:
    """A forwarded segment quoted with '>' on (nearly) every line loses one quote level."""
    lines = [ln for ln in seg.split("\n") if ln.strip()]
    if lines and sum(1 for ln in lines if ln.lstrip().startswith(">")) >= 0.9 * len(lines):
        return "\n".join(re.sub(r"^\s*> ?", "", ln) for ln in seg.split("\n"))
    return seg


def _strip_signature(seg: str) -> str:
    """Cut an RFC 3676 '-- ' signature or a trailing mobile "Sent from my ..." line."""
    lines = seg.split("\n")
    for i, line in enumerate(lines):
        if _SIG_SEP_RE.match(line):
            tail = [ln for ln in lines[i + 1 :] if ln.strip()]
            if len(tail) <= 15:
                lines = lines[:i]
                break
    nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
    for i in nonblank[-4:]:
        if _MOBILE_SIG_RE.match(lines[i]):
            lines = lines[:i]
            break
    # Mailing-list footer (Mailman, Google Groups): a rule, then a few lines with list info.
    nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
    for i in nonblank[-7:]:
        if _RULE_LINE_RE.match(lines[i]):
            footer = "\n".join(lines[i + 1 :])
            if re.search(r"(?i)mailing list|listinfo|mailman|unsubscribe|google groups|https?://", footer):
                lines = lines[:i]
            break
    return "\n".join(lines)


def _is_chrome(text: str) -> bool:
    return is_mail_chrome(text, max_words=_CHROME_MAX_WORDS, max_chars=_CHROME_MAX_CHARS)


def _chrome_lines(lines: list[str], *, head: bool, tail: bool) -> set[int]:
    """Which of the (non-empty) ``lines`` of one block near the start (``head``) and/or
    end (``tail``) of the body are chrome: all of them when the block as a whole is;
    otherwise single lines among its first/last few (with the lead-in question just
    before one: "Want to change how you receive these emails?" / "You can unsubscribe"),
    and in a block near the end everything from a "You are receiving this email
    because ..." line on."""
    if not lines:
        return set()
    if _is_chrome(" ".join(lines)):
        return set(range(len(lines)))
    eligible = [
        j for j in range(len(lines)) if (head and j < _CHROME_HEAD) or (tail and j >= len(lines) - _CHROME_TAIL)
    ]
    drop: set[int] = set()
    for j in eligible:
        if j in drop or len(lines[j]) > _CHROME_LINE_MAX_CHARS:
            continue
        if tail and _is_chrome(" ".join(lines[j:])):
            drop.update(range(j, len(lines)))
            break
        if _is_chrome(lines[j]):
            drop.add(j)
            if j - 1 in eligible and j - 1 not in drop and _is_chrome(f"{lines[j - 1]} {lines[j]}"):
                drop.add(j - 1)
    return drop


def _strip_chrome_lines(seg: str) -> str:
    """Plain text: chrome paragraphs and lines (see :func:`_chrome_lines`) in the first
    :data:`_CHROME_HEAD` / last :data:`_CHROME_TAIL` paragraphs of a body or forward."""
    lines = seg.split("\n")
    paras: list[list[int]] = []  # each paragraph: the indexes of its lines
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if paras and paras[-1][-1] == i - 1:
            paras[-1].append(i)
        else:
            paras.append([i])
    drop: set[int] = set()
    for n, para in enumerate(paras):
        head, tail = n < _CHROME_HEAD, n >= len(paras) - _CHROME_TAIL
        if head or tail:
            texts = [" ".join(lines[i].split()) for i in para]
            drop.update(para[j] for j in _chrome_lines(texts, head=head, tail=tail))
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


def _word_count(s: str) -> int:
    return len(_WORD_RE.findall(s))


def _rejoin_angle_urls(s: str) -> str:
    """'<https://a.example/very\\n/long>' (RFC 3986 App. C wrapping) -> one URL."""
    return _ANGLE_URL_RE.sub(lambda m: "<" + re.sub(r"\s+", "", m.group(1)) + ">", s)


def _clean_plain_body(text: str) -> tuple[str, list[_ForwardInfo]]:
    text = _rejoin_angle_urls(_normalize_newlines(text))
    segments, infos = _split_forwards_text(text)
    cleaned: list[str] = []
    for n, seg in enumerate(segments):
        if n:
            seg = _unquote_segment(seg)
        seg = _strip_chrome_lines(_strip_signature(seg))
        cleaned.append(_tidy_text(seg))
    if infos and cleaned and _word_count(cleaned[0]) <= SHORT_NOTE_WORDS:
        cleaned[0] = ""  # "FYI" / "see below" above a forward
    body = "\n\n".join(c for c in cleaned if c)
    return _tidy_text(body), infos


def _strip_link_annotations(text: str) -> str:
    """'word <https://...>' / 'word [https://...]' (HTML links flattened by the sender's
    client) -> 'word'. Bare URLs on their own are kept."""
    return _LINK_ANNOTATION_RE.sub("", text)


# ======================================================================================
# HTML body cleanup
# ======================================================================================

_BLOCK_TAGS = frozenset(
    "address article aside blockquote br center dd div dl dt fieldset figcaption figure footer form "
    "h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tbody td tfoot th thead tr ul".split()
)
_DROP_TAGS = frozenset(
    "script style head meta link base img picture source svg video audio iframe object embed "
    "canvas map area noscript template button input select textarea".split()
)
_HIDDEN_STYLE_RE = re.compile(
    r"(?i)(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all|"
    r"max-height\s*:\s*0(?:px)?\s*(?:!important)?\s*(?:;|$)|opacity\s*:\s*0(?:\.0+)?\s*(?:!important)?\s*(?:;|$))"
)
_SIGNATURE_XPATH = (
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' gmail_signature ')"
    " or @data-smartmail='gmail_signature'"
    " or @id='AppleMailSignature' or @id='Signature' or @id='signature'"
    " or @id='ms-outlook-mobile-signature' or @id='ymail_android_signature'"
    " or contains(concat(' ', normalize-space(@class), ' '), ' moz-signature ')"
    " or contains(concat(' ', normalize-space(@class), ' '), ' protonmail_signature_block ')]"
)


def _remove_keep_tail(el: etree._Element) -> None:
    """Remove an element (or comment) but keep its tail text in the tree."""
    parent = el.getparent()
    if parent is None:
        return
    tail = el.tail
    prev = el.getprevious()
    if tail:
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(el)


def _el_text(el: etree._Element) -> str:
    """Visible text of an element with line breaks at <br> and block boundaries."""
    parts: list[str] = []
    for event, node in etree.iterwalk(el, events=("start", "end")):
        tag = node.tag if isinstance(node.tag, str) else ""
        tag = tag.lower()
        if event == "start":
            if tag in _BLOCK_TAGS:
                parts.append("\n")
            if tag and node.text:
                parts.append(node.text)
        else:
            if tag in _BLOCK_TAGS:
                parts.append("\n")
            if node is not el and node.tail:
                parts.append(node.tail)
    text = "".join(parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v​‌‍﻿]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


# huge_tree: libxml2 otherwise stops at 256 levels of nesting and silently drops the rest
# (long Outlook reply chains nest that deep).
_HTML_PARSER = lxml.html.HTMLParser(huge_tree=True, remove_comments=False)


def _parse_html(html: str) -> Optional[lxml.html.HtmlElement]:
    html = re.sub(r"^\s*<\?xml[^>]*>", "", html or "")
    if not html.strip():
        return None
    try:
        return lxml.html.document_fromstring(html, parser=_HTML_PARSER)
    except (etree.ParserError, ValueError):
        return None


def _crude_html_text(html: str) -> str:
    """Last resort when lxml cannot parse a body: tags out, entities decoded."""
    s = re.sub(r"(?is)<(script|style|head)\b.*?</\1\s*>", " ", html or "")
    s = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6])\s*>", "\n", s)
    s = _html.unescape(re.sub(r"<[^>]*>", " ", s))
    return _tidy_text(re.sub(r"[ \t\xa0]+", " ", s))


def _body_of(doc: lxml.html.HtmlElement) -> lxml.html.HtmlElement:
    body = doc.find("body")
    return body if body is not None else doc


def _strip_html_junk(doc: lxml.html.HtmlElement) -> None:
    for c in list(doc.iter(etree.Comment, etree.ProcessingInstruction)):
        _remove_keep_tail(c)
    for el in list(doc.iter()):
        if not isinstance(el.tag, str) or el.getparent() is None:
            continue
        tag = el.tag.lower()
        if tag in _DROP_TAGS or (tag == "title" and el.getparent().tag != "head"):
            _remove_keep_tail(el)
        elif el.get("hidden") is not None or _HIDDEN_STYLE_RE.search(el.get("style") or ""):
            _remove_keep_tail(el)  # preheaders, mobile-only duplicates
    for el in list(doc.xpath(_SIGNATURE_XPATH)):
        if el.getparent() is not None:
            _remove_keep_tail(el)


def _block_ancestor(el: etree._Element, stop: etree._Element) -> etree._Element:
    node = el
    while node is not None and node is not stop:
        if isinstance(node.tag, str) and node.tag.lower() in _BLOCK_TAGS:
            return node
        node = node.getparent()
    return el


def _is_attached(el: etree._Element, root: etree._Element) -> bool:
    node = el
    while node is not None:
        if node is root:
            return True
        node = node.getparent()
    return False


def _html_units(body: etree._Element) -> list[etree._Element]:
    """The blocks holding the body's text, in document order: for each text, the block
    :func:`_block_ancestor` gives it (one pass, not a walk up the tree per text)."""
    units: list[etree._Element] = []
    seen: set[etree._Element] = set()
    stack: list[etree._Element] = []  # open block elements below ``body``

    def add(unit: Optional[etree._Element]) -> None:
        if unit is not None and unit not in seen:
            seen.add(unit)
            units.append(unit)

    for event, node in etree.iterwalk(body, events=("start", "end")):
        tag = node.tag.lower() if isinstance(node.tag, str) else ""
        is_block = node is not body and tag in _BLOCK_TAGS
        if event == "start":
            if is_block:
                stack.append(node)
            if tag and node.text and node.text.strip():
                add(stack[-1] if stack else node)
        else:
            if is_block:
                stack.pop()
            if node is not body and node.tail and node.tail.strip():
                add(stack[-1] if stack else node.getparent())
    return units


_LINE_SPACE_RE = re.compile(r"[ \t\r\n\f\v\xa0​‌‍﻿]+")


def _has_block_inside(el: etree._Element) -> bool:
    return any(
        isinstance(d.tag, str) and d.tag.lower() in _BLOCK_TAGS and d.tag.lower() != "br" for d in el.iterdescendants()
    )


def _unit_lines(unit: etree._Element) -> list[tuple[str, list[tuple[str, etree._Element]]]]:
    """The lines of ``unit``'s own text (split at ``<br>`` and nested blocks, which are
    units of their own): (text, parts), a part being ("text", unit) for ``unit.text``,
    ("el", child) for an inline child with its tail, ("tail", child) for the text after
    a ``<br>`` or nested block."""
    lines: list[list[tuple[str, etree._Element]]] = [[]]
    if unit.text:
        lines[-1].append(("text", unit))
    for ch in unit:
        tag = ch.tag.lower() if isinstance(ch.tag, str) else ""
        if tag in _BLOCK_TAGS or (tag and _has_block_inside(ch)):
            lines.append([])
            if ch.tail:
                lines[-1].append(("tail", ch))
        else:
            lines[-1].append(("el", ch))
    out = []
    for parts in lines:
        pieces = []
        for kind, el in parts:
            if kind == "text":
                pieces.append(el.text or "")
            elif kind == "tail":
                pieces.append(el.tail or "")
            else:
                pieces.append((el.text_content() if isinstance(el.tag, str) else "") + (el.tail or ""))
        text = _LINE_SPACE_RE.sub(" ", "".join(pieces)).strip()
        if text:
            out.append((text, parts))
    return out


def _remove_line(unit: etree._Element, parts: list[tuple[str, etree._Element]]) -> None:
    for kind, el in parts:
        if kind == "text":
            unit.text = None
        elif kind == "tail":
            el.tail = None
        elif el.getparent() is unit:
            unit.remove(el)  # with its tail, which is on the same line


def _text_without(el: etree._Element, skip: list[etree._Element]) -> str:
    """Visible text of ``el`` without the subtrees in ``skip`` (their tails are kept)."""
    parts: list[str] = []
    skipping: Optional[etree._Element] = None
    for event, node in etree.iterwalk(el, events=("start", "end")):
        if skipping is not None:
            if event == "end" and node is skipping:
                skipping = None
                if node.tail:
                    parts.append(node.tail)
            continue
        if event == "start":
            if any(node is s for s in skip):
                skipping = node
            elif isinstance(node.tag, str) and node.text:
                parts.append(node.text)
        elif node is not el and node.tail:
            parts.append(node.tail)
    return _LINE_SPACE_RE.sub(" ", "".join(parts)).strip()


def _strip_html_chrome(body: etree._Element) -> None:
    """Remove "View in browser", "Unsubscribe", "You are receiving this email because..."
    in the first :data:`_CHROME_HEAD` / last :data:`_CHROME_TAIL` blocks of the body:
    a block that is chrome as a whole (see :func:`_chrome_lines`), else its chrome lines;
    and a short block whose "Unsubscribe"-style link comes with nothing but a footer
    sentence ("You received this because you subscribed.")."""
    units = _html_units(body)
    window: dict[etree._Element, tuple[bool, bool]] = {}
    for n, unit in enumerate(units):
        head, tail = n < _CHROME_HEAD, n >= len(units) - _CHROME_TAIL
        if head or tail:
            window[unit] = (head, tail)
    targets: list[etree._Element] = []
    for unit, (head, tail) in window.items():
        text = _el_text(unit)
        if unit is not body and len(text) <= _CHROME_MAX_CHARS and _is_chrome(text):
            targets.append(unit)
            continue
        lines = _unit_lines(unit)
        for j in sorted(_chrome_lines([t for t, _ in lines], head=head, tail=tail)):
            _remove_line(unit, lines[j][1])
    by_unit: dict[etree._Element, list[etree._Element]] = {}
    for a in body.iter("a"):
        if is_mail_chrome_link(a.text_content() or ""):
            blk = _block_ancestor(a, body)
            if blk in window and blk is not body:
                by_unit.setdefault(blk, []).append(a)
    for blk, anchors in by_unit.items():
        if not _is_attached(blk, body) or len(_el_text(blk)) > _CHROME_MAX_CHARS:
            continue
        rest = _text_without(blk, anchors)
        if not re.search(r"\w", rest) or _FOOTER_LEAD_RE.match(rest):
            targets.append(blk)
    for el in targets:
        if el is not body and _is_attached(el, body):
            _remove_keep_tail(el)


def _strip_html_mobile_sig(body: etree._Element) -> None:
    for t in body.xpath(".//text()"):
        if len(t) > 120 or not _MOBILE_SIG_RE.match(t.strip()):
            continue
        owner = t.getparent()
        if owner is not None and t.is_tail:
            owner = owner.getparent()
        if owner is None:
            continue
        blk = _block_ancestor(owner, body)
        if blk is not body and _is_attached(blk, body) and len(_el_text(blk)) <= 120:
            _remove_keep_tail(blk)


def _text_after(body: etree._Element, owner: etree._Element, is_tail: bool) -> str:
    """Visible text after a marker that is ``owner.text`` (or ``owner.tail``)."""
    parts: list[str] = []
    started = False
    for event, node in etree.iterwalk(body, events=("start", "end")):
        tag = node.tag.lower() if isinstance(node.tag, str) else ""
        if not started:
            if node is owner and event == ("end" if is_tail else "start"):
                started = True
            continue
        if event == "start":
            if tag in _BLOCK_TAGS:
                parts.append("\n")
            if node.text:
                parts.append(node.text)
        else:
            if tag in _BLOCK_TAGS:
                parts.append("\n")
            if node.tail:
                parts.append(node.tail)
    return "".join(parts).strip()


def _truncate_from(body: etree._Element, owner: etree._Element, is_tail: bool) -> None:
    """Remove the marker text and everything after it in document order."""
    if is_tail:
        owner.tail = None
    else:
        owner.text = None
        for ch in list(owner):
            owner.remove(ch)
    node = owner
    while node is not None and node is not body:
        parent = node.getparent()
        if parent is None:
            break
        for sib in list(node.itersiblings()):
            parent.remove(sib)
        node.tail = None
        node = parent


def _strip_html_sig_separator(body: etree._Element) -> None:
    """RFC 3676 "-- " signature written in HTML (Thunderbird, Apple, hand-typed)."""
    for t in body.xpath(".//text()"):
        if t.strip() != "--":
            continue
        owner = t.getparent()
        if owner is None:
            continue
        after = _text_after(body, owner, t.is_tail)
        if len(after) <= 500 and len([ln for ln in after.split("\n") if ln.strip()]) <= 15:
            _truncate_from(body, owner, t.is_tail)
            return


def _header_only(text: str) -> Optional[tuple[dict[str, str], bool]]:
    """If ``text`` is nothing but a forward banner and/or header lines: (headers, banner)."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None
    banner = False
    if _FWD_BANNER_RE.match(lines[0]):
        banner = True
        lines = lines[1:]
    headers, end = _parse_header_lines(lines, 0)
    if end != len(lines):
        return None
    return headers, banner


_HTML_FWD_HINT_RE = re.compile(
    r"(?i)forward|weitergeleitet|r[ée]exp[ée]di|transf[ée]r[ée]|reenviado|inoltrat|doorgestuurd|"
    r"encaminhad|vidarebefordrat|original message|^\s*\*?\s*(?:from|von|de|da|van|från|od)\s*\*?\s*:"
)


def _find_html_forward(body: etree._Element) -> Optional[tuple[list[etree._Element], dict[str, str]]]:
    """First forward header block in document order: (elements to remove, headers)."""
    for el in body.xpath(".//*[@id='divRplyFwdMsg']"):
        res = _header_only(_el_text(el))
        if res:
            return [el], res[0]
    for t in body.xpath(".//text()"):
        if not _HTML_FWD_HINT_RE.search(t):
            continue
        owner = t.getparent()
        if owner is None:
            continue
        if t.is_tail:
            owner = owner.getparent()
        best: Optional[etree._Element] = None
        best_res: Optional[tuple[dict[str, str], bool]] = None
        node = owner
        while node is not None and node is not body:
            text = _el_text(node)
            if len(text) > 3000:
                break
            res = _header_only(text)
            if res is None:
                break
            best, best_res = node, res
            node = node.getparent()
        if best is None or best_res is None:
            continue
        group = [best]
        headers = dict(best_res[0])
        banner = best_res[1]
        # Apple Mail: the banner and each header line are separate sibling <div>s.
        sib = best.getnext()
        while sib is not None and not (group[-1].tail or "").strip():
            stext = _el_text(sib)
            if not stext.strip():
                group.append(sib)
            else:
                sres = _header_only(stext)
                if sres is None or sres[1]:
                    break
                for k, v in sres[0].items():
                    headers.setdefault(k, v)
                group.append(sib)
            sib = sib.getnext()
        if _is_forward_header(headers, banner):
            # Outlook puts an <hr> right above the header block.
            prev = best.getprevious()
            if (
                prev is not None
                and isinstance(prev.tag, str)
                and prev.tag.lower() == "hr"
                and not (prev.tail or "").strip()
            ):
                group.insert(0, prev)
            return group, headers
    return None


def _text_before(body: etree._Element, target: etree._Element) -> str:
    parts: list[str] = []
    for event, node in etree.iterwalk(body, events=("start", "end")):
        if node is target:
            break
        if event == "start" and isinstance(node.tag, str):
            if node.text:
                parts.append(node.text)
            if node.tag.lower() in _BLOCK_TAGS:
                parts.append(" ")
        elif event == "end" and node is not body and node.tail:
            parts.append(node.tail)
    return " ".join(parts)


def _drop_preceding(body: etree._Element, target: etree._Element) -> None:
    node = target
    while node is not None and node is not body:
        parent = node.getparent()
        if parent is None:
            break
        parent.text = None
        for sib in list(node.itersiblings(preceding=True)):
            parent.remove(sib)  # removes the sibling's tail too: it is also preceding text
        node = parent


def _strip_html_forwards(body: etree._Element) -> list[_ForwardInfo]:
    infos: list[_ForwardInfo] = []
    for _ in range(5):
        found = _find_html_forward(body)
        if not found:
            break
        group, headers = found
        infos.append(_forward_info(headers))
        first = group[0]
        # Apple Mail wraps forwarded content in <blockquote type="cite">: not a quote.
        anc = first.getparent()
        while anc is not None and anc is not body:
            nxt = anc.getparent()
            if (
                isinstance(anc.tag, str)
                and anc.tag.lower() == "blockquote"
                and (anc.get("type") or "").lower() == "cite"
            ):
                anc.drop_tag()
            anc = nxt
        if _word_count(_text_before(body, first)) <= SHORT_NOTE_WORDS:
            _drop_preceding(body, first)
        for el in group:
            if _is_attached(el, body):
                _remove_keep_tail(el)
    return infos


@dataclass
class _HtmlBody:
    html: str
    text: str  # visible text after cleanup
    links: list[tuple[str, str]]  # (href, anchor text) for http(s) anchors
    forwards: list[_ForwardInfo]


def _clean_html_body(html: str, title: str = "") -> Optional[_HtmlBody]:
    doc = _parse_html(html)
    if doc is None:
        return None
    _strip_html_junk(doc)
    body = _body_of(doc)
    forwards = _strip_html_forwards(body)
    _strip_html_sig_separator(body)
    _strip_html_chrome(body)
    _strip_html_mobile_sig(body)
    links = _links_of(body)
    text = _el_text(body)
    # Rebuild a minimal document: title + cleaned body (no head junk).
    head = doc.find("head")
    if head is not None:
        for ch in list(head):
            head.remove(ch)
    else:
        head = etree.Element("head")
        doc.insert(0, head)
    if title:
        t = etree.SubElement(head, "title")
        t.text = title
    out = lxml.html.tostring(doc, encoding="unicode", method="html")
    return _HtmlBody(html=out, text=text, links=links, forwards=forwards)


def _links_of(el: etree._Element) -> list[tuple[str, str]]:
    """(href, anchor text) of the http(s) anchors under ``el``."""
    links = []
    for a in el.iter("a"):
        href = (a.get("href") or "").strip()
        if re.match(r"(?i)https?://", href):
            # An anchor wrapping other anchors (iOS rich-link cards) gives no title text.
            nested = next(a.iterdescendants("a"), None) is not None
            links.append((href, "" if nested else clean_text(a.text_content() or "")))
    return links


def _html_links(html: str) -> list[tuple[str, str]]:
    doc = _parse_html(html)
    if doc is None:
        return []
    _strip_html_junk(doc)
    return _links_of(_body_of(doc))


def _html_visible_text(html: str) -> str:
    doc = _parse_html(html)
    if doc is None:
        return ""
    _strip_html_junk(doc)
    return _el_text(_body_of(doc))


def _plain_to_html(text: str) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "".join("<p>" + _html.escape(p).replace("\n", "<br>") + "</p>" for p in paras)


# ======================================================================================
# MIME traversal
# ======================================================================================


@dataclass
class _Unit:
    """One displayable body unit: a single text part or a multipart/alternative group."""

    plain: Optional[str] = None
    html: Optional[str] = None


@dataclass
class _Walk:
    units: list[_Unit] = field(default_factory=list)
    attachments: list[Message] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)  # message/rfc822 parts


_SKIP_TYPES = frozenset(
    {
        "application/pkcs7-signature",
        "application/x-pkcs7-signature",
        "application/pgp-signature",
        "application/pgp-keys",
        "application/pgp-encrypted",
        "application/ms-tnef",
        "application/vnd.ms-tnef",
        "text/calendar",
        "text/vcard",
        "text/x-vcard",
        "text/directory",
        "text/rfc822-headers",
        "message/delivery-status",
        "message/disposition-notification",
        "message/feedback-report",
    }
)


def _filename(part: Message) -> str:
    try:
        name = part.get_filename() or part.get_param("name") or ""
    except Exception:
        name = ""
    if isinstance(name, tuple):  # RFC 2231 tuple from get_param
        try:
            name = email.utils.collapse_rfc2231_value(name)
        except Exception:
            name = ""
    name = _fix_surrogates(str(name))
    # Never trust paths in attachment names.
    name = PureWindowsPath(PurePosixPath(name).name).name
    return clean_text(name)


def _disposition(part: Message) -> str:
    try:
        return (part.get_content_disposition() or "").lower()
    except Exception:
        return ""


def _is_body_text(part: Message) -> bool:
    if part.get_content_type() not in ("text/plain", "text/html"):
        return False
    if _disposition(part) == "attachment":
        return False
    return not _filename(part)


def _walk(part: Message, w: _Walk) -> None:
    ctype = part.get_content_type()
    if part.get_content_maintype() == "message" and part.get_content_subtype() in ("rfc822", "global"):
        w.messages.append(part)
        return
    if part.is_multipart():
        sub = part.get_content_subtype()
        children = list(part.get_payload() or [])
        if sub == "alternative":
            _walk_alternative(children, w)
        elif sub == "related":
            root = _related_root(part, children)
            if root is not None:
                _walk(root, w)
            for c in children:
                # Inline resources of the HTML root: images are dropped, anything else
                # (a PDF someone embedded) is still an attachment.
                if c is not root and not c.is_multipart() and c.get_content_maintype() not in ("image", "text"):
                    w.attachments.append(c)
        elif sub == "signed":
            if children:
                _walk(children[0], w)
        elif sub == "encrypted":
            pass  # cannot be read without the recipient's key
        else:  # mixed, report, digest, parallel, unknown subtypes
            for c in children:
                _walk(c, w)
        return
    if ctype in _SKIP_TYPES:
        return
    if _is_body_text(part):
        text = _part_text(part)
        w.units.append(_Unit(plain=text) if ctype == "text/plain" else _Unit(html=text))
        return
    w.attachments.append(part)


def _related_root(part: Message, children: list[Message]) -> Optional[Message]:
    start = part.get_param("start")
    if start:
        for c in children:
            if str(c.get("content-id", "")).strip() == str(start).strip():
                return c
    return children[0] if children else None


def _walk_alternative(children: list[Message], w: _Walk) -> None:
    plain: Optional[str] = None
    html: Optional[str] = None
    for c in children:  # RFC 2046: least to most faithful; first plain, last HTML win
        sub = _Walk()
        _walk(c, sub)
        w.attachments.extend(sub.attachments)
        w.messages.extend(sub.messages)
        if not sub.units:
            continue
        p_parts = [u.plain for u in sub.units if u.plain is not None]
        if p_parts and plain is None:
            plain = "\n\n".join(p_parts)
        if any(u.html is not None for u in sub.units):
            # Apple Mail splits HTML around inline attachments (mixed[html, pdf, html]).
            html = "".join(u.html if u.html is not None else _plain_to_html(u.plain or "") for u in sub.units)
    if plain is not None or html is not None:
        w.units.append(_Unit(plain=plain, html=html))


def _inner_message(part: Message) -> Optional[Message]:
    payload = part.get_payload()
    if isinstance(payload, list) and payload:
        inner = payload[0]
        return inner if isinstance(inner, Message) else None
    raw = _part_bytes(part)  # some clients base64-encode message/rfc822 (not allowed, but seen)
    if raw:
        try:
            return email.message_from_bytes(raw, policy=email.policy.default)
        except Exception:
            return None
    return None


# ======================================================================================
# Requests
# ======================================================================================

_IMAGE_EXTS = frozenset(".png .jpg .jpeg .gif .bmp .tif .tiff .webp .heic .heif .svg .ico .emz .wmz".split())
_TEXT_EXTS = frozenset(".txt .text .md .markdown".split())
_HTML_EXTS = frozenset(".html .htm .xhtml".split())
_PDF_TYPES = frozenset(
    {"application/pdf", "application/x-pdf", "application/acrobat", "applications/vnd.pdf", "text/pdf"}
)
_GENERIC_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/x-download",
        "application/download",
        "binary/octet-stream",
        "application/unknown",
    }
)
_EXCHANGE_STUB_RE = re.compile(r"(?i)^ATT\d{5}\.(?:txt|htm|html)$")


def _trim_url_text(u: str) -> str:
    """A matched URL without the punctuation after it ("...)." of "(see https://...).")."""
    u = u.rstrip(".,;:!?*'\"»…")
    # Keep balanced parentheses (Wikipedia), drop an unbalanced closing one.
    while u.endswith(")") and u.count(")") > u.count("("):
        u = u[:-1].rstrip(".,;:!?")
    return u


def _trim_url(u: str) -> str:
    u = _trim_url_text(u)
    if u.lower().startswith("www."):
        u = "https://" + u
    return u


def _urls_in(text: str) -> list[str]:
    return list(dict.fromkeys(_trim_url(u) for u in _URL_RE.findall(text) if len(u) > 10))


def _non_url_words(text: str) -> int:
    return _word_count(_URL_RE.sub(" ", text))


def _choose_body(unit: _Unit) -> tuple[str, str]:
    """('plain'|'html', content) for one body unit (§9 / u-extract §9 rule)."""
    plain, html = unit.plain, unit.html
    if html is None or not html.strip():
        return "plain", plain or ""
    if plain is None or not re.search(r"\w", plain):
        return "html", html
    if _STUB_PLAIN_RE.search(plain) and len(plain) < 600:
        return "html", html
    html_text = _html_visible_text(html)
    if len(html_text) > 1.5 * len(plain) + 200:
        return "html", html
    if len(_LINK_ANNOTATION_RE.findall(plain)) >= 3 and len(html_text) >= 0.5 * len(_strip_link_annotations(plain)):
        return "html", html  # newsletter text alternative full of "[https://...]" link dumps
    return "plain", plain


def _title_for_url(subject: str, url: str) -> str:
    if not subject or _URL_RE.fullmatch(subject.strip()) or subject.strip() == url:
        return ""
    return subject


def _url_requests(
    urls: list[str], subject: str, sender: str, anchor_titles: Optional[dict[str, str]] = None
) -> list[IngestRequest]:
    reqs = []
    for u in urls[:MAX_URLS_PER_EMAIL]:
        title = ""
        if len(urls) == 1:
            title = _title_for_url(subject, u) or (anchor_titles or {}).get(u, "")
            if title and _URL_RE.fullmatch(title.strip()):
                title = ""
        reqs.append(IngestRequest(kind="url", source="email", url=u, title=title, sender=sender))
    return reqs


#: Subject tag the "Send to X3" iOS Shortcut puts on its emails ("[x3] <page title>").
_SHORTCUT_TAG_RE = re.compile(r"^\s*\[x3\]", re.I)
#: A host name standing alone ("example.com" under an iOS rich-link title).
_HOST_WORD_RE = re.compile(r"(?i)\(?(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}/?\)?[.,;:]?")


def _norm_words(s: str) -> str:
    return " ".join(w.casefold() for w in _WORD_RE.findall(s))


_LINK_MARK = "\x00"  # where a link was, while its brackets are looked at
#: Words that only introduce a link: "(see https://...)", "[via https://...]", "<URL:https://...>".
_LINK_LEAD_WORDS = frozenset("see via cf source src link links url here more also read full article story".split())
_ANGLE_LINK_RE = re.compile(r"(?i)[ \t]*<\s*(?:URL:)?\s*\x00\s*>")  # "<https://...>", "<URL:https://...>"
_BRACKETS_RE = re.compile(r"([ \t]*)(?:\(([^()\n]*)\)|\[([^\[\]\n]*)\])")
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\(\s*\x00\s*\)")  # Markdown "[text](https://...)"
#: "..., see https://..." / "... via https://..." at the end of a line.
_TRAILING_LEAD_RE = re.compile(r"(?i)[\s,;:–—-]*\b(?:see|via|cf|source|link)\b\s*:?\s*\x00[\s.,;:!?]*$")
_EDGE_SEPARATORS = " \t:;,|-–—"


def _strip_links(line: str) -> str:
    """``line`` without its links and what only framed them: the brackets around a link
    ("Read <https://...> now" -> "Read now"), a "(see https://...)" aside, a "see"/"via"
    or a ":" / dash left dangling before a link at the end of the line. Punctuation
    after a link stays ("Great piece (https://...)." -> "Great piece.")."""
    s = _URL_RE.sub(lambda m: _LINK_MARK + m.group(0)[len(_trim_url_text(m.group(0))) :], line)
    if _LINK_MARK not in s:
        return " ".join(line.split())
    s = _ANGLE_LINK_RE.sub("", _MD_LINK_RE.sub(r"\1", s))

    def bracket(m: re.Match[str]) -> str:
        paren = m.group(2) is not None
        inner = m.group(2) if paren else m.group(3)
        if _LINK_MARK not in inner:
            return m.group(0)
        if all(w.casefold() in _LINK_LEAD_WORDS for w in _WORD_RE.findall(inner)):
            return ""  # "(https://...)", "(see: https://...)", "[via https://...]"
        inner = " ".join(inner.replace(_LINK_MARK, " ").split()).strip(_EDGE_SEPARATORS)
        return m.group(1) + ("(" + inner + ")" if paren else "[" + inner + "]")  # "(my blog, https://...)" -> "(my blog)"

    s = _BRACKETS_RE.sub(bracket, s)
    s = re.sub(r"(?<=[.!?])(\s*\x00)[.!?]+", r"\1", s)  # "Read this! https://...!": one "!" is enough
    s = _TRAILING_LEAD_RE.sub("", s)
    s = re.sub(r"\s*\x00\s*(?=[.,;:!?)\]]|$)", "", s)  # no space left before what followed the link
    s = re.sub(r"(?<=[(\[])\s*\x00\s*", "", s)
    return " ".join(s.replace(_LINK_MARK, " ").split()).strip(_EDGE_SEPARATORS)


def _link_comment(text: str, titles: Iterable[str]) -> tuple[str, int]:
    """The sender's own words in a link share (links removed, see :func:`_strip_links`),
    and how many words that is. Lines that are only links or host names, or that are
    (part of) the link's title (the subject, an anchor text: iOS rich-link cards repeat
    the page title), are not a comment."""
    known = [f" {n} " for n in (_norm_words(t) for t in titles if t) if n]
    lines: list[str] = []
    words = 0
    for ln in text.split("\n"):
        rest = _strip_links(ln)
        n = _norm_words(" ".join(w for w in rest.split() if not _HOST_WORD_RE.fullmatch(w)))
        if n and not any(f" {n} " in k for k in known):
            lines.append(rest)
            words += len(n.split())
    return "\n".join(lines), words


def _link_share_requests(
    text: str, urls: list[str], title: str, sender: str, links: list[tuple[str, str]]
) -> Optional[list[IngestRequest]]:
    """A body made of links plus a short comment (:data:`URL_ONLY_MAX_WORDS` words,
    :data:`URL_ONLY_MAX_CHARS` characters) is a link share, by the same rule as a
    text-only HTTP share: one ``url`` request per link. With a comment, each link is
    sent as ``auto`` text "<comment>\\n\\n<link>" instead, so the Processor fetches the
    article and keeps the comment as a note (or drops it when it is just filler or the
    page title). None: not a link share."""
    if not urls or len(urls) > MAX_URLS_PER_EMAIL:
        return None
    anchors: dict[str, str] = {}
    for h, t in links:  # iOS rich links repeat the href; the first text is the title
        if t and not _URL_RE.fullmatch(t) and not _HOSTNAME_RE.fullmatch(t):
            anchors.setdefault(_trim_url(h), t)
    comment, words = _link_comment(text, [title, *(t for _, t in links)])
    if words > URL_ONLY_MAX_WORDS or len(comment) > URL_ONLY_MAX_CHARS:
        return None
    if not comment:
        return _url_requests(urls, title, sender, anchors)
    return [
        IngestRequest(kind="auto", source="email", text=f"{comment}\n\n{u}", sender=sender,
                      title=(_title_for_url(title, u) or anchors.get(u, "")) if len(urls) == 1 else "")
        for u in urls
    ]


def _link_key(u: str) -> str:
    """One page's link however it is spelled: no scheme, ``www.`` or trailing slash, the
    host in lower case ("http://Example.com/" and "example.com" are the same)."""
    u = re.sub(r"(?i)^(?:https?://)?(?:www\.)?", "", _trim_url_text(u.strip().strip("()<>"))).rstrip("/")
    host, sep, rest = u.partition("/")
    return host.casefold() + sep + rest


def _shortcut_requests(text: str, title: str, sender: str) -> Optional[list[IngestRequest]]:
    """The Shortcut emails ``<link>``, a blank line and ``<text>`` under the subject
    ``[x3] <title>`` whenever the share has a link (a Safari page's address, else the
    first web link iOS finds in the shared text), and ``<text>`` alone when it has none.
    A body starting with a link is handed on as the Shortcut's HTTP request would be:

    - only the link (nothing else, or the same link again, also as a bare host name):
      a ``url`` request titled by the subject;
    - the link and text: ``auto`` with ``url=<link>``, ``text=<text>``, ``title=<subject>``.
      The Processor then decides as for the HTTP request: a Safari selection is kept as a
      note with the page as its source, text that is only the page title gets the fetched
      article; for shared text that contains the link itself it finds the same link(s) and
      comment in the text as it does over HTTP (where ``url`` is empty).

    None (a body without a link): the caller makes a text note of it, as for any email."""
    body = text.strip()
    first = body.split(None, 1)[0] if body else ""
    lead = first.strip("<>")
    if not _URL_RE.fullmatch(lead):
        return None
    lead = _trim_url(lead)
    selection = _tidy_text(body[len(first) :])
    key = _link_key(lead)
    others = _URL_RE.sub(" ", selection).split()
    if all(_link_key(u) == key for u in _URL_RE.findall(selection)) and all(
        _HOST_WORD_RE.fullmatch(w) and _link_key(w) == key for w in others
    ):
        return _url_requests([lead], title, sender)
    return [IngestRequest(kind="auto", source="email", url=lead, text=selection, title=title, sender=sender)]


def _body_requests(units: list[_Unit], subject: str, sender: str, has_other: bool,
                   from_shortcut: bool = False) -> list[IngestRequest]:
    """Requests for the message body (text, html, url or auto). ``has_other`` =
    attachments or forwarded messages exist, so a short covering note ("see attached")
    is dropped. ``from_shortcut``: the subject carries the Shortcut's ``[x3]`` tag (see
    :func:`_shortcut_requests`; a hand-written "check this out: <url>" is still a link
    share)."""
    chosen = [_choose_body(u) for u in units if (u.plain or u.html)]
    if not chosen:
        return []
    if any(kind == "html" for kind, _ in chosen):
        html = "".join(c if k == "html" else _plain_to_html(_clean_plain_body(c)[0]) for k, c in chosen)
        hb = _clean_html_body(html, subject)
        if hb is None or not re.search(r"\w", hb.text):
            # Unparseable (or beyond libxml2's nesting limit): deliver the text, not nothing.
            # Only when the HTML clearly has prose, so cleaned-away chrome stays away.
            text = _crude_html_text(html)
            if hb is not None and _word_count(text) <= 2 * _word_count(_html_visible_text(html)) + SHORT_NOTE_WORDS:
                return []  # the parser saw the same text: it was all chrome, not truncation
            if not re.search(r"\w", text):
                return []
            return [IngestRequest(kind="text", source="email", text=text, title=subject, sender=sender)]
        sender = _forward_sender(hb.forwards) or sender
        title = subject or _forward_subject(hb.forwards)
        if title != subject:
            hb = _clean_html_body(html, title) or hb
        if from_shortcut and (reqs := _shortcut_requests(hb.text, title, sender)) is not None:
            return reqs
        text_urls = _resolve_display_urls(_urls_in(hb.text), hb.links)
        hrefs = list(dict.fromkeys(_trim_url(h) for h, _ in hb.links))
        link_urls = text_urls or (hrefs if len(hrefs) == 1 else [])
        if (reqs := _link_share_requests(hb.text, link_urls, title, sender, hb.links)) is not None:
            return reqs
        if has_other and _non_url_words(hb.text) <= SHORT_NOTE_WORDS and not text_urls:
            return []
        return [IngestRequest(kind="html", source="email", html=hb.html, title=title, sender=sender)]

    raw_plain = "\n\n".join(c for _, c in chosen)
    html_alts = [u.html for u in units if u.html]
    text, forwards = _clean_plain_body(raw_plain)
    if not re.search(r"\w", text):
        return []
    sender = _forward_sender(forwards) or sender
    title = subject or _forward_subject(forwards)
    if from_shortcut and (reqs := _shortcut_requests(text, title, sender)) is not None:
        return reqs
    urls = _urls_in(text)
    if urls and len(urls) <= MAX_URLS_PER_EMAIL:
        links = [link for h in html_alts for link in _html_links(h)]  # rich-link titles in the HTML twin
        if (reqs := _link_share_requests(text, urls, title, sender, links)) is not None:
            return reqs
    if has_other and _non_url_words(text) <= SHORT_NOTE_WORDS and not urls:
        return []
    if html_alts:
        text = _tidy_text(_strip_link_annotations(text))
    source_url = _edge_url(text)
    return [IngestRequest(kind="text", source="email", text=text, title=title, url=source_url, sender=sender)]


_HOSTNAME_RE = re.compile(r"(?i)(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/?")


def _resolve_display_urls(urls: list[str], links: list[tuple[str, str]]) -> list[str]:
    """Clients shorten long link text ("https://example.com/long-pa…"); use the full href
    of the anchor showing it. A complete visible URL wins over its href, which is often
    a tracking redirect."""
    out = []
    for u in urls:
        full = u
        for href, text in links:
            if u in text and href.startswith(u) and len(href) > len(u) and re.search(r"(…|\.\.\.)\s*$", text):
                full = _trim_url(href)
                break
        out.append(full)
    return list(dict.fromkeys(out))


def _edge_url(text: str) -> str:
    """A URL alone on the first or last line ("link + selected text" shares) is the source."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    for ln in (lines[:1] + lines[-1:]) if lines else []:
        if _URL_RE.fullmatch(ln) or re.fullmatch(r"<\s*https?://\S+\s*>", ln):
            return _trim_url(ln.strip("<> "))
    return ""


_ADDR_SPECIALS_RE = re.compile(r'[()<>\[\]:;@\\,."]')


def mailbox(name: str, addr: str) -> str:
    """``IngestRequest.sender`` value: an RFC 5322 mailbox ("Name <addr>", the name quoted
    when needed, never RFC 2047-encoded) so ``email.utils.parseaddr`` gets both parts back;
    just the address without a name, just the name without an address."""
    name = clean_text(name or "")
    addr = (addr or "").strip()
    if not addr:
        return name
    if not name or name.lower() == addr.lower():
        return addr
    if _ADDR_SPECIALS_RE.search(name):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>"


def _forward_sender(infos: list[_ForwardInfo]) -> str:
    for i in reversed(infos):  # the innermost forward is the original author
        if i.from_name or i.from_addr:
            return mailbox(i.from_name, i.from_addr)
    return ""


def _forward_subject(infos: list[_ForwardInfo]) -> str:
    for i in reversed(infos):
        if i.subject:
            return i.subject
    return ""


def _attachment_request(part: Message, sender: str, notes: list[str]) -> Optional[IngestRequest]:
    ctype = part.get_content_type()
    name = _filename(part)
    ext = PurePosixPath(name.lower()).suffix
    label = name or ctype
    if part.get_content_maintype() == "image" or ext in _IMAGE_EXTS:
        return None  # inline and signature images
    if ctype in _SKIP_TYPES or ext in (".ics", ".vcf", ".p7s", ".asc", ".sig") or name.lower() == "winmail.dat":
        return None
    data = _part_bytes(part)
    if not data:
        return None
    generic = ctype in _GENERIC_TYPES or ctype.startswith("application/")
    if (
        ctype == "application/epub+zip"
        or ext == ".epub"
        or (data[:4] == b"PK\x03\x04" and b"application/epub+zip" in data[:200])
    ):
        # Books pass straight through (the Processor keeps EPUB files as they are);
        # IngestKind has no "epub", and a request with ``data`` is classified by its bytes.
        if not name.lower().endswith(".epub"):
            name = (name or "book") + ".epub"
        return IngestRequest(
            kind="auto", source="email", data=data, filename=name, content_type="application/epub+zip", sender=sender
        )
    if ctype in _PDF_TYPES or ext == ".pdf" or (generic and data[:1024].find(b"%PDF-") >= 0):
        if not name.lower().endswith(".pdf"):
            name = (name or "attachment") + ".pdf"
        return IngestRequest(
            kind="pdf", source="email", data=data, filename=name, content_type="application/pdf", sender=sender
        )
    is_text_type = ctype in ("text/plain", "text/markdown", "text/x-markdown")
    if is_text_type or (ext in _TEXT_EXTS and (generic or part.get_content_maintype() == "text")):
        if _EXCHANGE_STUB_RE.match(name) and len(data) < 2048:
            return None
        text = _tidy_text(_repair_mojibake(decode_text_bytes(data, _part_charset(part))))
        if not re.search(r"\w", text):
            return None
        return IngestRequest(
            kind="text", source="email", text=text, title=_stem_title(name), filename=name,
            content_type="text/plain", sender=sender,
        )  # fmt: skip
    if ctype in ("text/html", "application/xhtml+xml") or (
        ext in _HTML_EXTS and (generic or part.get_content_maintype() == "text")
    ):
        if _EXCHANGE_STUB_RE.match(name) and len(data) < 2048:
            return None
        charset = _part_charset(part)
        if not _norm_charset(charset):
            charset = _meta_charset(data) or charset
        html = _repair_mojibake(decode_text_bytes(data, charset))
        if not re.search(r"\w", _html_visible_text(html)):
            return None
        has_title = bool(re.search(r"(?is)<title[^>]*>\s*\S", html))
        return IngestRequest(
            kind="html", source="email", html=html, title="" if has_title else _stem_title(name),
            filename=name, content_type="text/html", sender=sender,
        )  # fmt: skip
    notes.append(f"skipped attachment {label!r} ({ctype})")
    return None


def _stem_title(filename: str) -> str:
    stem = PurePosixPath(filename).stem if filename else ""
    return clean_text(stem.replace("_", " "))


def _message_requests(msg: Message, *, depth: int, notes: list[str]) -> list[IngestRequest]:
    subject = clean_subject(_unfold(_header(msg, "subject")))
    froms = _addresses(_header_values(msg, "from"))
    sender = ""
    if froms:
        name, addr = froms[0]
        sender = mailbox(name, addr)

    w = _Walk()
    _walk(msg, w)
    att_reqs: list[IngestRequest] = []
    for part in w.attachments:
        r = _attachment_request(part, sender, notes)
        if r:
            att_reqs.append(r)
    inner_reqs: list[IngestRequest] = []
    for part in w.messages:
        if depth >= MAX_NESTING:
            notes.append("skipped deeply nested forwarded message")
            continue
        inner = _inner_message(part)
        if inner is None:
            continue
        sub_reqs = _message_requests(inner, depth=depth + 1, notes=notes)
        for r in sub_reqs:
            if not r.title and r.kind in ("text", "html") and subject:
                r.title = subject
        inner_reqs.extend(sub_reqs)

    from_shortcut = bool(_SHORTCUT_TAG_RE.match(_unfold(_header(msg, "subject"))))
    body_reqs = _body_requests(w.units, subject, sender, has_other=bool(att_reqs or inner_reqs),
                               from_shortcut=from_shortcut)
    reqs = body_reqs + inner_reqs + att_reqs

    if not reqs and depth == 0:
        # Nothing in the body: a link in the subject line still counts.
        urls = _urls_in(_header(msg, "subject"))
        if urls:
            reqs = _url_requests(urls, "", sender)

    # A lone attachment in an otherwise empty email is titled by the subject.
    if subject and not body_reqs and not inner_reqs and len(att_reqs) == 1 and att_reqs[0].data is None:
        att_reqs[0].title = subject
    return reqs


def _dedupe(reqs: list[IngestRequest]) -> list[IngestRequest]:
    seen: set[str] = set()
    out: list[IngestRequest] = []
    for r in reqs:
        if r.data is not None:
            key = "file:" + hashlib.sha256(r.data).hexdigest()
        elif r.kind == "url":
            key = "url:" + r.url.rstrip("/")
        elif r.kind == "html":
            key = "txt:" + _content_hash(_html_visible_text(r.html))
        else:
            key = "txt:" + _content_hash(r.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).casefold().encode("utf-8", "surrogatepass")).hexdigest()
