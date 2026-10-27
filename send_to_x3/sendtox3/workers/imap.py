"""IMAP ingest worker (ARCHITECTURE §9).

Polls one mailbox folder over an outbound TLS connection (nothing is exposed on the
Pi) and turns accepted messages into queue items:

1. connect (IMAPS, or STARTTLS on port 143), log in, select ``imap_folder``;
2. find new mail: on the first sync for a folder/UIDVALIDITY ``UID SEARCH UNSEEN``
   (only mail since yesterday in an INBOX without a plus-tag: that is the user's own
   mailbox, not a backlog of shares), afterwards ``UID SEARCH UID <last+1>:*`` (new mail
   even if a phone already opened it). The last handled UID and the UIDVALIDITY are
   kept in ``store.kv_*`` so nothing is processed twice, across reconnects and restarts;
3. fetch headers (``BODY.PEEK[HEADER]`` + ``RFC822.SIZE``) and run ``accept_email`` on
   them, so unwanted or oversized mail is never downloaded; then fetch the message
   (``BODY.PEEK[]``), parse (off the event loop), accept again with the content, and
   hand the requests to ``state.processor.process_many``. Before the download the
   attempt is recorded in ``store.kv``, so a message that keeps crashing or hanging the
   app is given up after :data:`MAX_ATTEMPTS` starts even across restarts. A lost
   connection, a shutdown or a ``StorageError`` (store not writable) is never counted:
   the message stays unread and is retried later. Any other failure (a NO to the
   FETCH, an exception while parsing or processing) counts;
4. flag ``\\Seen`` and, if ``imap_processed_folder`` is set, ``UID MOVE`` there (or
   COPY + ``\\Deleted`` + EXPUNGE on servers without MOVE). Mail that is not accepted
   is left untouched (unread, in place): in a shared or personal mailbox it is someone
   else's or the user's other mail. With neither ``allowed_senders`` nor
   ``imap_plus_tag`` set, or with ``allowed_senders: ['*']`` in an INBOX without a
   plus-tag, the worker does not run at all (fail closed, see ``sender_filter_problem``);
5. wait with IDLE (re-issued every 25 min) when the server supports it, else poll every
   60 s; reconnect with exponential backoff 5 s -> 5 min on any error.

imaplib is blocking, so every IMAP call runs on one dedicated worker thread (calls on
one connection are therefore strictly sequential). Cancelling :func:`run` shuts the
socket down to interrupt a blocking IDLE immediately. The worker never raises out of
:func:`run` except ``CancelledError``, and never logs the password.

Health is published in ``state.status["imap"]`` (see :class:`ImapWorker`).
"""

from __future__ import annotations

import asyncio
import base64
import functools
import imaplib
import logging
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from typing import Any, Optional, Protocol

from ..config import Options
from ..extract.email import (
    ParsedEmail,
    accept_email,
    addressed_to_tag,
    normalize_plus_tag,
    parse_email,
    sender_allowed,
    sender_filter_problem,
    trusted_authserv_ids,
    watches_personal_inbox,
)
from ..ingest import StorageError
from ..models import IngestRequest, Item
from ..store import Store

log = logging.getLogger(__name__)

IDLE_SECONDS = 1500  # RFC 2177: re-issue IDLE before the 29 min server timeout (§9: <= 25 min)
POLL_SECONDS = 60  # servers without IDLE
BACKOFF_MIN = 5.0
BACKOFF_MAX = 300.0
SOCKET_TIMEOUT = 60.0  # per blocking socket operation (not per command)
LOGOUT_TIMEOUT = 10.0
MAX_ATTEMPTS = 3  # processing attempts per message (failures, crashes, hangs) before giving up on it
HEADER_BATCH = 50  # UIDs per header FETCH
MAX_RESYNCS = 5  # immediate re-syncs when mail arrives during processing


# ======================================================================================
# The state contract this worker needs
# ======================================================================================


class ProcessorLike(Protocol):
    async def process_many(self, reqs: list[IngestRequest]) -> list[Item]: ...


class ImapState(Protocol):
    """What :func:`run` needs from the application state object."""

    opts: Options
    store: Store
    processor: ProcessorLike
    status: dict[str, Any]  # the worker owns status["imap"]


class ImapAuthError(Exception):
    """The server rejected the credentials (retrying quickly will not help)."""


class ImapConfigError(Exception):
    """Wrong folder, no TLS, ... (needs the user to change an option)."""


ConnectFn = Callable[[Options], Any]  # test hook: returns an imaplib.IMAP4-like object


async def run(state: ImapState, **kwargs: Any) -> None:
    """Long-running IMAP worker. Returns at once if IMAP is disabled or not configured.

    Keyword arguments are passed to :class:`ImapWorker` (tests use them to shorten
    timings or to inject an SSL context / fake connection)."""
    await ImapWorker(state, **kwargs).run()


# ======================================================================================
# Small IMAP helpers (pure functions, unit-tested)
# ======================================================================================


def encode_mailbox(name: str) -> str:
    """Folder name as an IMAP quoted string, non-ASCII in modified UTF-7 (RFC 3501 5.1.3).

    ASCII names are taken as given (a literal ``&`` is escaped as ``&-`` unless it already
    starts a modified UTF-7 run), so names copied from another client keep working."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            b = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(b).decode("ascii").rstrip("=").replace("/", ",") + "-")
            buf.clear()

    if name.isascii():
        wire = re.sub(r"&(?![A-Za-z0-9+,]*-)", "&-", name)
    else:
        for ch in name:
            if 0x20 <= ord(ch) <= 0x7E:
                flush()
                out.append("&-" if ch == "&" else ch)
            else:
                buf.append(ch)
        flush()
        wire = "".join(out)
    return '"' + wire.replace("\\", "\\\\").replace('"', '\\"') + '"'


_META_UID_RE = re.compile(rb"\bUID\s+(\d+)", re.I)
_META_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)", re.I)
_META_FLAGS_RE = re.compile(rb"\bFLAGS\s+\(([^)]*)\)", re.I)
_META_NEW_RE = re.compile(rb"^\s*\d+\s+\(")
_META_LITERAL_RE = re.compile(rb"(BODY(?:\.PEEK)?\[[^\]]*\](?:<\d+>)?|RFC822(?:\.HEADER|\.TEXT)?)\s*\{\d+\}\s*$", re.I)


def _meta_into(rec: dict[str, Any], meta: bytes) -> None:
    if m := _META_UID_RE.search(meta):
        rec["uid"] = int(m.group(1))
    if m := _META_SIZE_RE.search(meta):
        rec["size"] = int(m.group(1))
    if m := _META_FLAGS_RE.search(meta):
        rec["flags"] = m.group(1).decode("ascii", "replace").split()


def parse_fetch(data: list[Any]) -> dict[int, dict[str, Any]]:
    """imaplib FETCH data -> {uid: {"size", "flags", "header", "body"}}.

    Robust to item order (UID before or after the literal), several literals per
    message, and unsolicited flag-only FETCH responses (merged, never overwriting)."""
    recs: dict[int, dict[str, Any]] = {}
    cur: Optional[dict[str, Any]] = None

    def commit() -> None:
        if cur is not None and "uid" in cur:
            target = recs.setdefault(cur["uid"], {})
            for k, v in cur.items():
                if k not in target or k in ("header", "body"):
                    target[k] = v

    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            meta = item[0] if isinstance(item[0], bytes) else bytes(str(item[0]), "ascii", "replace")
            lit = item[1] if isinstance(item[1], bytes) else b""
            if _META_NEW_RE.match(meta) or cur is None:
                commit()
                cur = {}
            _meta_into(cur, meta)
            m = _META_LITERAL_RE.search(meta)
            section = m.group(1).upper().replace(b".PEEK", b"") if m else b"BODY[]"
            if section.startswith(b"BODY[HEADER") or section == b"RFC822.HEADER":
                cur["header"] = lit
            elif section in (b"BODY[]", b"RFC822"):
                cur["body"] = lit
            commit()
        elif isinstance(item, bytes):
            if _META_NEW_RE.match(item):
                commit()
                cur = {}
            if cur is not None:
                _meta_into(cur, item)
                commit()
    commit()
    return recs


def parse_search(data: list[Any]) -> list[int]:
    uids: set[int] = set()
    for item in data or []:
        if isinstance(item, bytes):
            uids.update(int(x) for x in item.split() if x.isdigit())
    return sorted(uids)


def _int_code(conn: Any, code: str) -> Optional[int]:
    try:
        _typ, data = conn.response(code)
    except Exception:
        return None
    for d in data or []:
        if isinstance(d, bytes):
            m = re.search(rb"\d+", d)
            if m:
                return int(m.group(0))
    return None


def _text(data: Any) -> str:
    """Server response data -> short printable text for logs."""
    if isinstance(data, (list, tuple)):
        data = b" ".join(d for d in data if isinstance(d, bytes))
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    return str(data or "").strip()[:300]


def _enable_keepalive(sock: Any) -> None:
    """TCP keepalive so a silently dropped connection (NAT, Wi-Fi) is noticed in minutes."""
    if not isinstance(sock, socket.socket):
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
        if idle_opt is not None:
            sock.setsockopt(socket.IPPROTO_TCP, idle_opt, 60)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
    except OSError:
        pass


def _hard_close(conn: Any) -> None:
    try:
        conn.shutdown()
    except Exception:
        pass


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(ts: float) -> str:
    """IMAP ``date`` (RFC 3501: ``25-Oct-2026``), independent of the C locale."""
    t = time.gmtime(ts)
    return f"{t.tm_mday}-{_MONTHS[t.tm_mon - 1]}-{t.tm_year}"


def _not_the_messages_fault(e: BaseException) -> bool:
    """A lost connection, a shutdown or a storage failure: the attempt it interrupted is
    not counted against the message. Anything else (an IMAP NO/BAD answer, MemoryError,
    a bug) is, so a message that keeps failing is given up after :data:`MAX_ATTEMPTS`."""
    if isinstance(e, imaplib.IMAP4.abort):  # the connection broke
        return True
    if isinstance(e, imaplib.IMAP4.error):  # the server answered NO/BAD for this message
        return False
    return isinstance(e, (OSError, EOFError, StorageError, asyncio.CancelledError, KeyboardInterrupt, SystemExit))


def _consume(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()  # mark retrieved: the abandoned call's error is expected


# ======================================================================================
# Worker
# ======================================================================================


class ImapWorker:
    """One IMAP account/folder. ``status`` (= ``state.status["imap"]``) keys:

    ``enabled``, ``state`` (disabled | not_configured | connecting | connected | syncing |
    idle | polling | backoff | stopped), ``connected``, ``host``, ``user``, ``folder``,
    ``mode`` (idle | poll), ``connected_since``, ``last_sync_at``, ``last_message_at``,
    ``messages_processed``, ``messages_rejected``, ``items_created``,
    ``last_reject_reason``, ``last_error``, ``last_error_at``, ``next_retry_at``,
    ``warning``. Timestamps are unix seconds (0 = never).
    """

    def __init__(
        self,
        state: ImapState,
        *,
        idle_seconds: float = IDLE_SECONDS,
        poll_seconds: float = POLL_SECONDS,
        backoff_min: float = BACKOFF_MIN,
        backoff_max: float = BACKOFF_MAX,
        timeout: float = SOCKET_TIMEOUT,
        ssl_context: Optional[ssl.SSLContext] = None,
        connect: Optional[ConnectFn] = None,
    ) -> None:
        self.state = state
        self.opts: Options = state.opts
        self.store: Store = state.store
        self.idle_seconds = max(1.0, min(float(idle_seconds), float(IDLE_SECONDS)))
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.backoff_min = max(0.01, float(backoff_min))
        self.backoff_max = max(self.backoff_min, float(backoff_max))
        self.timeout = float(timeout)
        self._ssl_context = ssl_context
        self._connect_hook = connect

        status_root = getattr(state, "status", None)
        if not isinstance(status_root, dict):
            status_root = {}
            try:
                state.status = status_root
            except Exception:
                pass
        self.status: dict[str, Any] = {}
        status_root["imap"] = self.status

        self._executor: Optional[ThreadPoolExecutor] = None
        self._conn: Any = None
        self._aborted = False
        self._backoff = self.backoff_min
        self._uidvalidity = 0
        self._uidnext = 0
        self._move_ok = False
        self._done: set[tuple[int, int]] = set()  # (uidvalidity, uid) handled this process
        self._warnings: dict[str, str] = {}

        folder = (self.opts.imap_folder or "INBOX").strip() or "INBOX"
        self.folder = "INBOX" if folder.upper() == "INBOX" else folder
        # The user's own INBOX, not a folder/label or +tag address only shares go to: other
        # mail there is not ours to flag, and its unread backlog is not a queue of shares.
        self.personal_inbox = watches_personal_inbox(self.opts)
        self._kv_prefix = (
            f"imap:{(self.opts.imap_user or '').strip().lower()}@"
            f"{(self.opts.imap_host or '').strip().lower()}:{self.opts.imap_port}/{self.folder}"
        )
        self._set(
            enabled=bool(self.opts.imap_enabled),
            state="starting",
            connected=False,
            host=(self.opts.imap_host or "").strip(),
            user=(self.opts.imap_user or "").strip(),
            folder=self.folder,
            mode="",
            connected_since=0,
            last_sync_at=0,
            last_message_at=0,
            messages_processed=0,
            messages_rejected=0,
            items_created=0,
            last_reject_reason="",
            last_error="",
            last_error_at=0,
            next_retry_at=0,
            warning="",
        )

    # ------------------------------------------------------------------ status/logs
    def _set(self, **kw: Any) -> None:
        self.status.update(kw)

    def _warn(self, key: str, text: str) -> None:
        """Set or clear (``text=""``) one of the warnings shown in ``status["warning"]``."""
        self._warnings[key] = text
        self._set(warning=" ".join(w for w in self._warnings.values() if w))

    def _config_warnings(self) -> None:
        opts = self.opts
        if self.personal_inbox:
            note = ("Watching INBOX without imap_plus_tag: mail there from allowed senders becomes items and is "
                    "marked read (other mail is left alone). A dedicated folder or label, or imap_plus_tag, is safer.")
            self._warn("config", note)
            log.warning("IMAP: %s", note)
        if opts.require_dmarc and trusted_authserv_ids(opts) is None:
            log.warning(
                "IMAP: %s is not a provider whose Authentication-Results header is known; require_dmarc trusts the "
                "topmost one, which is only safe if your mail server adds its own to every message",
                (opts.imap_host or "").strip(),
            )

    def _redact(self, s: str) -> str:
        pw = self.opts.imap_password or ""
        return s.replace(pw, "***") if len(pw) >= 3 else s

    def _describe(self, e: BaseException) -> str:
        msg = str(e).strip() or type(e).__name__
        if isinstance(e, socket.gaierror):
            msg = f"cannot resolve {self.opts.imap_host!r} ({msg})"
        elif isinstance(e, ssl.SSLCertVerificationError):
            msg = f"TLS certificate check failed for {self.opts.imap_host!r} ({e.verify_message or msg})"
        elif isinstance(e, (TimeoutError, socket.timeout)):
            msg = f"timed out talking to {self.opts.imap_host!r}"
        elif isinstance(e, ConnectionRefusedError):
            msg = f"connection refused by {self.opts.imap_host}:{self.opts.imap_port}"
        elif isinstance(e, imaplib.IMAP4.abort):
            msg = f"connection lost ({msg})"
        elif isinstance(e, ssl.SSLError):
            msg = f"TLS error talking to {self.opts.imap_host!r} ({getattr(e, 'reason', None) or msg})"
        elif isinstance(e, (ConnectionResetError, BrokenPipeError, EOFError)):
            msg = f"connection lost ({msg})"
        return self._redact(msg)

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        opts = self.opts
        if not opts.imap_enabled:
            self._set(state="disabled", enabled=False, reason="imap_enabled is off")
            return
        missing = [n for n in ("imap_host", "imap_user", "imap_password") if not str(getattr(opts, n) or "").strip()]
        if missing:
            reason = f"set {', '.join(missing)} to enable email ingest"
            # enabled=False tells the app's supervisor this return is deliberate (no restart loop).
            self._set(state="not_configured", enabled=False, reason=reason, last_error=reason)
            log.warning("IMAP: email ingest enabled but %s not set; worker not started", ", ".join(missing))
            return
        if problem := sender_filter_problem(opts):
            # Fail closed: not even connecting, so no mail is read, flagged, or skipped over
            # (the high-water mark stays put until the filter is fixed).
            self._set(state="not_configured", enabled=False, reason=problem, last_error=problem, warning=problem)
            log.warning("IMAP: %s; worker not started", problem)
            return
        self._config_warnings()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sendtox3-imap")
        try:
            while True:
                try:
                    await self._session()
                    err, delay = "connection closed by server", self._next_delay()
                except asyncio.CancelledError:
                    raise
                except ImapAuthError as e:
                    err, delay = f"login failed for {opts.imap_user!r}: {self._redact(str(e))}", self.backoff_max
                except ImapConfigError as e:
                    err, delay = self._redact(str(e)), self.backoff_max
                except (OSError, imaplib.IMAP4.error, EOFError, ssl.SSLError) as e:
                    err, delay = self._describe(e), self._next_delay()
                except Exception as e:  # a bug: log it, but keep the app alive
                    log.exception("IMAP: unexpected error")
                    err, delay = self._describe(e), self._next_delay()
                now = int(time.time())
                self._set(
                    state="backoff",
                    connected=False,
                    last_error=err,
                    last_error_at=now,
                    next_retry_at=now + int(delay),
                )
                log.warning("IMAP: %s; reconnecting in %g s", err, round(delay, 1))
                await asyncio.sleep(delay)
        finally:
            self._set(state="stopped", connected=False, mode="")
            ex, self._executor = self._executor, None
            if ex is not None:
                ex.shutdown(wait=False, cancel_futures=True)

    def _next_delay(self) -> float:
        d = self._backoff
        self._backoff = min(self._backoff * 2, self.backoff_max)
        return d

    async def _io(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking IMAP call on the worker thread; on cancel, abort the socket."""
        assert self._executor is not None
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._executor, functools.partial(fn, *args))
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            if not fut.done():
                self._abort()
                fut.add_done_callback(_consume)
                await asyncio.wait({fut}, timeout=5)
            raise

    def _abort(self) -> None:
        """Interrupt a blocking read (IDLE) from the event loop thread."""
        self._aborted = True
        conn = self._conn
        if conn is None:
            return
        sock = getattr(conn, "sock", None)
        try:
            if isinstance(sock, socket.socket):
                # The plain socket method, bypassing SSLSocket's own shutdown: it unblocks
                # the reader thread instead of racing with it on the TLS object.
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            else:
                conn.shutdown()
        except Exception:
            pass

    # ------------------------------------------------------------------ session
    async def _session(self) -> None:
        opts = self.opts
        self._set(state="connecting", connected=False)
        self._aborted = False
        conn = await self._io(self._open)
        try:
            idle = "IDLE" in tuple(getattr(conn, "capabilities", ()) or ())
            self._set(
                state="connected",
                connected=True,
                connected_since=int(time.time()),
                mode="idle" if idle else "poll",
                last_error="",
                next_retry_at=0,
            )
            log.info(
                "IMAP: connected to %s:%s as %s, watching %r (%s)",
                opts.imap_host,
                opts.imap_port,
                opts.imap_user,
                self.folder,
                "IDLE" if idle else f"polling every {self.poll_seconds:g} s",
            )
            while True:
                await self._sync_until_quiet(conn)
                self._backoff = self.backoff_min  # a healthy session resets the backoff
                if idle:
                    self._set(state="idle")
                    await self._io(self._idle_wait, conn)
                else:
                    self._set(state="polling")
                    await asyncio.sleep(self.poll_seconds)
                    await self._io(self._noop, conn)
        finally:
            self._conn = None
            await self._close(conn)

    async def _close(self, conn: Any) -> None:
        self._set(connected=False)
        if self._aborted or self._executor is None:
            _hard_close(conn)
            return
        try:
            await asyncio.wait_for(self._io(self._logout, conn), timeout=LOGOUT_TIMEOUT)
        except asyncio.CancelledError:
            _hard_close(conn)
            raise
        except Exception:
            _hard_close(conn)

    # ------------------------------------------------------------------ blocking calls
    def _open(self) -> Any:
        """Connect, log in, select the folder (runs on the worker thread)."""
        opts = self.opts
        host = (opts.imap_host or "").strip()
        port = int(opts.imap_port or 993)
        if self._connect_hook is not None:
            conn = self._connect_hook(opts)
            self._conn = conn
        else:
            ctx = self._ssl_context or ssl.create_default_context()
            if port == 143:
                conn = imaplib.IMAP4(host, port, timeout=self.timeout)
                self._conn = conn
                if "STARTTLS" not in conn.capabilities:
                    _hard_close(conn)
                    raise ImapConfigError(
                        f"{host}:143 does not offer STARTTLS; refusing to send the password unencrypted (use port 993)"
                    )
                conn.starttls(ssl_context=ctx)
            else:
                conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=self.timeout)
                self._conn = conn
        try:
            self._prepare(conn)
        except BaseException:
            self._conn = None
            _hard_close(conn)
            raise
        return conn

    def _prepare(self, conn: Any) -> None:
        if self._aborted:
            raise ImapConfigError("cancelled")
        _enable_keepalive(getattr(conn, "sock", None))
        self._login(conn)
        if self._aborted:
            raise ImapConfigError("cancelled")
        try:
            typ, data = conn.select(encode_mailbox(self.folder))
        except imaplib.IMAP4.readonly:
            raise ImapConfigError(f"folder {self.folder!r} is read-only") from None
        if typ != "OK":
            raise ImapConfigError(f"cannot open folder {self.folder!r}: {_text(data)}")
        self._uidvalidity = _int_code(conn, "UIDVALIDITY") or 0
        self._uidnext = _int_code(conn, "UIDNEXT") or 0
        if not self._uidvalidity or not self._uidnext:
            typ, data = conn.status(encode_mailbox(self.folder), "(UIDVALIDITY UIDNEXT)")
            blob = b" ".join(d for d in (data or []) if isinstance(d, bytes))
            if m := re.search(rb"UIDVALIDITY\s+(\d+)", blob):
                self._uidvalidity = self._uidvalidity or int(m.group(1))
            if m := re.search(rb"UIDNEXT\s+(\d+)", blob):
                self._uidnext = self._uidnext or int(m.group(1))
        self._move_ok = self._ensure_processed_folder(conn)

    def _login(self, conn: Any) -> None:
        user = (self.opts.imap_user or "").strip()
        pw = self.opts.imap_password or ""
        try:
            if user.isascii() and pw.isascii():
                conn.login(user, pw)
            elif "AUTH=PLAIN" in tuple(conn.capabilities or ()):
                conn.authenticate("PLAIN", lambda _challenge: ("\0" + user + "\0" + pw).encode("utf-8"))
            else:
                raise ImapAuthError(
                    "the user name or password has non-ASCII characters and the server has no AUTH=PLAIN"
                )
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error as e:
            raise ImapAuthError(self._redact(str(e)) or "rejected") from None

    def _ensure_processed_folder(self, conn: Any) -> bool:
        name = (self.opts.imap_processed_folder or "").strip()
        if not name or name == self.folder:
            return False
        box = encode_mailbox(name)
        try:
            typ, data = conn.list('""', box)
            if typ == "OK" and any(d for d in (data or []) if d):
                return True
            typ, data = conn.create(box)
        except imaplib.IMAP4.abort:
            raise
        except imaplib.IMAP4.error as e:
            typ, data = "NO", [str(e).encode()]
        if typ == "OK":
            log.info("IMAP: created folder %r for processed mail", name)
            return True
        warning = f"cannot use processed folder {name!r} ({_text(data)}); processed mail stays in {self.folder!r}"
        log.warning("IMAP: %s", warning)
        self._warn("processed_folder", warning)
        return False

    def _search(self, conn: Any, *criteria: str) -> list[int]:
        typ, data = conn.uid("SEARCH", *criteria)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"SEARCH failed: {_text(data)}")
        return parse_search(data)

    def _fetch_headers(self, conn: Any, uids: list[int]) -> dict[int, dict[str, Any]]:
        typ, data = conn.uid("FETCH", ",".join(map(str, uids)), "(UID RFC822.SIZE BODY.PEEK[HEADER])")
        if typ != "OK":
            raise imaplib.IMAP4.error(f"FETCH failed: {_text(data)}")
        return parse_fetch(data)

    def _fetch_message(self, conn: Any, uid: int) -> Optional[bytes]:
        typ, data = conn.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
        if typ != "OK":
            raise imaplib.IMAP4.error(f"FETCH failed: {_text(data)}")
        rec = parse_fetch(data).get(uid)
        return rec.get("body") if rec else None

    def _mark_seen(self, conn: Any, uid: int) -> None:
        typ, data = conn.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Seen)")
        if typ != "OK":
            log.warning("IMAP: could not flag message %d as seen: %s", uid, _text(data))

    def _move(self, conn: Any, uid: int) -> None:
        target = encode_mailbox((self.opts.imap_processed_folder or "").strip())
        caps = tuple(getattr(conn, "capabilities", ()) or ())
        if "MOVE" in caps:
            typ, data = conn.uid("MOVE", str(uid), target)
            if typ == "OK":
                return
            log.info("IMAP: MOVE failed (%s), trying COPY", _text(data))
        typ, data = conn.uid("COPY", str(uid), target)
        if typ != "OK":
            log.warning("IMAP: could not copy message %d to %s: %s", uid, target, _text(data))
            return
        conn.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
        if "UIDPLUS" in caps:
            conn.uid("EXPUNGE", str(uid))
        else:
            conn.expunge()

    def _idle_wait(self, conn: Any) -> None:
        """Block in IDLE until the mailbox changes or ``idle_seconds`` pass."""
        with conn.idle(duration=self.idle_seconds) as idler:
            for typ, data in idler:
                if typ in ("EXISTS", "RECENT"):
                    return
                if typ == "BYE":
                    raise imaplib.IMAP4.abort(f"server said BYE: {_text(data)}")

    def _noop(self, conn: Any) -> None:
        typ, data = conn.noop()
        if typ != "OK":
            raise imaplib.IMAP4.abort(f"NOOP failed: {_text(data)}")

    def _logout(self, conn: Any) -> None:
        try:
            conn.logout()
        except Exception:
            _hard_close(conn)

    @staticmethod
    def _pop_new_mail_hints(conn: Any) -> bool:
        """True if the server announced new mail (EXISTS/RECENT) during our last commands."""
        ur = getattr(conn, "untagged_responses", None)
        if not isinstance(ur, dict):
            return False
        hint = False
        for key in ("EXISTS", "RECENT"):
            if ur.pop(key, None):
                hint = True
        return hint

    # ------------------------------------------------------------------ sync
    async def _sync_until_quiet(self, conn: Any) -> None:
        """Sync; repeat at once while mail keeps arriving during processing."""
        await self._io(self._pop_new_mail_hints, conn)
        for _ in range(MAX_RESYNCS):
            handled = await self._sync(conn)
            hinted = await self._io(self._pop_new_mail_hints, conn)
            if not handled and not hinted:
                return

    def _kv(self, name: str) -> str:
        return f"{self._kv_prefix}:{name}"

    async def _sync(self, conn: Any) -> int:
        """Handle new mail once. Returns how many messages were handled."""
        self._set(state="syncing", last_sync_at=int(time.time()))
        validity = self._uidvalidity
        stored = self.store.kv_get(self._kv("uidvalidity"))
        first_run = stored != str(validity)
        if first_run:
            if stored:
                log.info(
                    "IMAP: UIDVALIDITY of %r changed (%s -> %s); looking at unread mail again",
                    self.folder,
                    stored,
                    validity,
                )
            self._done = {d for d in self._done if d[0] == validity}
            hwm = 0
            criteria = ["UNSEEN", "UNDELETED"]
            if self.personal_inbox:  # not years of the user's unread mail: only what was sent just now
                criteria += ["SINCE", imap_date(time.time() - 86_400)]
            uids = await self._io(self._search, conn, *criteria)
        else:
            hwm = int(self.store.kv_get(self._kv("last_uid")) or 0)
            uids = await self._io(self._search, conn, "UID", f"{hwm + 1}:*", "UNDELETED")
            uids = [u for u in uids if u > hwm]  # "n:*" also matches the highest UID when n > it
        uids = [u for u in uids if (validity, u) not in self._done]

        handled = 0
        for start in range(0, len(uids), HEADER_BATCH):
            batch = uids[start : start + HEADER_BATCH]
            meta = await self._io(self._fetch_headers, conn, batch)
            for uid in batch:
                rec = meta.get(uid)
                if rec is None or "header" not in rec:
                    self._mark_done(uid, first_run)  # expunged meanwhile
                    continue
                if not await self._handle(conn, uid, rec, first_run):
                    return handled  # retry later; later UIDs wait so the high-water mark stays exact
                handled += 1

        if first_run:
            baseline = max([self._uidnext - 1, *uids, 0])
            if not self._uidnext:
                baseline = max(baseline, await self._io(self._highest_uid, conn))
            self.store.kv_set(self._kv("last_uid"), str(baseline))
            self.store.kv_set(self._kv("uidvalidity"), str(validity))
        # Forget per-UID bookkeeping the high-water mark now covers.
        hwm_now = int(self.store.kv_get(self._kv("last_uid")) or 0)
        self._done = {d for d in self._done if d[0] == validity and d[1] > hwm_now}
        return handled

    def _highest_uid(self, conn: Any) -> int:
        typ, data = conn.uid("FETCH", "*", "(UID)")
        if typ != "OK":
            return 0
        recs = parse_fetch(data)
        return max(recs) if recs else 0

    def _mark_done(self, uid: int, first_run: bool) -> None:
        self._done.add((self._uidvalidity, uid))
        if self._attempts(uid):
            self._record_attempts(uid, 0)
        if not first_run:
            hwm = int(self.store.kv_get(self._kv("last_uid")) or 0)
            if uid > hwm:
                self.store.kv_set(self._kv("last_uid"), str(uid))

    # Messages are handled one at a time and a failure stops the sync there, so one kv
    # record "<uidvalidity>:<uid>:<attempts>" covers the message in progress.
    def _attempts(self, uid: int) -> int:
        """Processing attempts started for ``uid`` (by this or an earlier run of the app)."""
        parts = (self.store.kv_get(self._kv("attempts")) or "").split(":")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            if (int(parts[0]), int(parts[1])) == (self._uidvalidity, uid):
                return int(parts[2])
        return 0

    def _record_attempts(self, uid: int, n: int) -> None:
        self.store.kv_set(self._kv("attempts"), f"{self._uidvalidity}:{uid}:{n}" if n else "")

    def _restore_attempts(self, uid: int, n: int) -> None:
        """Undo the count of an attempt that was not the message's fault (best effort)."""
        try:
            self._record_attempts(uid, n)
        except Exception:  # the store may be what failed
            log.debug("IMAP: could not reset the attempt count of message %d", uid, exc_info=True)

    async def _failed(self, conn: Any, uid: int, sender: str, n: int, e: BaseException, what: str,
                      first_run: bool) -> bool:
        """Attempt ``n`` at ``what`` (parsing, processing) failed for a reason of the
        message's own: retry on the next sync, or give up (mark read) after
        :data:`MAX_ATTEMPTS`. Returns what :meth:`_handle` returns."""
        log.error("IMAP: %s message %d from %s failed (attempt %d/%d)", what, uid, sender, n, MAX_ATTEMPTS,
                  exc_info=e)
        self._set(last_error=f"{what} failed: {self._redact(str(e)) or type(e).__name__}",
                  last_error_at=int(time.time()))
        if n < MAX_ATTEMPTS:
            return False
        log.error("IMAP: giving up on message %d after %d attempts; marking it read", uid, n)
        return await self._give_up(conn, uid, first_run)

    async def _give_up(self, conn: Any, uid: int, first_run: bool) -> bool:
        self._mark_done(uid, first_run)
        await self._io(self._mark_seen, conn, uid)
        return True

    async def _handle(self, conn: Any, uid: int, rec: dict[str, Any], first_run: bool) -> bool:
        """Process one message. False = transient failure, retry on the next sync."""
        opts = self.opts
        size = int(rec.get("size") or 0)
        # MIME and HTML parsing of a large message takes seconds on a Pi: not on the event loop.
        try:
            head = await asyncio.to_thread(parse_email, rec.get("header") or b"", headers_only=True)
        except Exception:  # never by design; unreadable headers are rejected (left unread), not retried forever
            log.exception("IMAP: cannot parse the headers of message %d", uid)
            head = ParsedEmail(headers_only=True)
        head.size = max(head.size, size)
        ok, reason = accept_email(head, opts)
        if not ok:
            self._reject(uid, head, reason, first_run)
            return True

        before = self._attempts(uid)
        if before >= MAX_ATTEMPTS:
            # Started that often without finishing: it crashes or hangs the app (the
            # watchdog restarts it), so retrying again would only repeat that.
            log.error("IMAP: message %d from %s was started %d times without finishing; marking it read",
                      uid, head.from_addr, before)
            self._set(last_error=f"gave up on message {uid} after {before} attempts", last_error_at=int(time.time()))
            return await self._give_up(conn, uid, first_run)
        try:
            self._record_attempts(uid, before + 1)  # first: a crash or restart mid-message still counts
        except Exception as e:
            log.warning("IMAP: cannot record work on message %d (%s); will retry", uid, e)
            self._set(last_error=f"storage error: {self._redact(str(e))}; will retry", last_error_at=int(time.time()))
            return False
        try:
            raw = await self._io(self._fetch_message, conn, uid)
        except BaseException as e:
            if _not_the_messages_fault(e):
                self._restore_attempts(uid, before)
            raise  # anything else (a NO to this FETCH, ...) counts: given up after MAX_ATTEMPTS
        try:
            parsed = await asyncio.to_thread(parse_email, raw) if raw is not None else None
        except Exception as e:  # parse_email never raises by design: MemoryError, RecursionError, a bug
            return await self._failed(conn, uid, head.from_addr, before + 1, e, "parsing", first_run)
        except BaseException as e:
            if _not_the_messages_fault(e):
                self._restore_attempts(uid, before)
            raise
        if parsed is None:
            self._mark_done(uid, first_run)
            return True
        ok, reason = accept_email(parsed, opts)
        if not ok:
            self._reject(uid, parsed, reason, first_run)
            return True

        try:
            items = await self.state.processor.process_many(parsed.requests)
        except StorageError as e:
            # Nothing was saved (disk full, database error): keep the message unread and
            # retry later, however often; it is not the message's fault either.
            self._restore_attempts(uid, before)
            log.warning("IMAP: message %d from %s not saved (%s); will retry", uid, parsed.from_addr, e)
            self._set(last_error=f"could not save message {uid}: {self._redact(str(e)) or type(e).__name__}; "
                                 "will retry", last_error_at=int(time.time()))
            return False
        except Exception as e:
            return await self._failed(conn, uid, parsed.from_addr, before + 1, e, "processing", first_run)
        except BaseException as e:
            if _not_the_messages_fault(e):
                self._restore_attempts(uid, before)  # shutting down is not the message's fault
            raise

        self._mark_done(uid, first_run)  # before flagging: a lost STORE must not re-process
        items = list(items or [])
        titles = ", ".join(repr(getattr(i, "title", "")) for i in items[:5])
        log.info("IMAP: message %d from %s -> %d item(s) %s; %s", uid, parsed.from_addr, len(items), titles, reason)
        self._set(
            messages_processed=self.status.get("messages_processed", 0) + 1,
            items_created=self.status.get("items_created", 0) + len(items),
            last_message_at=int(time.time()),
        )
        await self._io(self._mark_seen, conn, uid)
        if self._move_ok:
            await self._io(self._move, conn, uid)
        return True

    def _reject(self, uid: int, p: ParsedEmail, reason: str, first_run: bool) -> None:
        """Mail that is not accepted is left untouched (unread, in place); the reason is
        reported when the mail was meant for us."""
        tag = normalize_plus_tag(self.opts.imap_plus_tag)
        # Meant for us: addressed to the secret tag, in a folder only shares go to, or
        # from an allowed sender in the user's own INBOX (spoofed, too big, empty, ...).
        # Anything else is someone else's mail in a shared or personal mailbox.
        if tag:
            ours = addressed_to_tag(p, tag)
        else:
            ours = not self.personal_inbox or sender_allowed(p, self.opts)
        self._mark_done(uid, first_run)
        if ours:
            log.warning("IMAP: message %d %s", uid, reason)
            self._set(
                messages_rejected=self.status.get("messages_rejected", 0) + 1,
                last_reject_reason=reason,
            )
        else:
            log.debug("IMAP: message %d ignored: %s", uid, reason)


__all__ = [
    "run",
    "ImapWorker",
    "ImapState",
    "ProcessorLike",
    "ImapAuthError",
    "ImapConfigError",
    "encode_mailbox",
    "parse_fetch",
    "parse_search",
    "imap_date",
    "IDLE_SECONDS",
    "POLL_SECONDS",
    "BACKOFF_MIN",
    "BACKOFF_MAX",
]
