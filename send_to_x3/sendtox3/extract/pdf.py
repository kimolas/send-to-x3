"""PDF -> :class:`~sendtox3.textmodel.Document` (ARCHITECTURE §14).

The conversion itself lives in :mod:`sendtox3.extract.pdfengine` (PDFium through
pypdfium2, then the layout heuristics of the pdf2epub prototype). It runs in a
separate Python process with a wall-clock timeout, an address-space cap and a page
cap, so a malicious or pathological PDF can only kill that process. The worker gets
a minimal environment (no tokens, no options) and, when this process is root (the
add-on container), runs as an unprivileged account. This function never raises: on
any failure it returns a Document without blocks whose first note says what went
wrong (the ingest marks such items ``failed`` and shows the note).

From asyncio, await :func:`pdf_to_document_async` (or :func:`convert_pdf_async`)
rather than ``asyncio.to_thread(pdf_to_document, ...)``. Cancelling them, as an outer
``asyncio.wait_for`` budget does, kills the worker at once and frees its converter
slot. A cancelled thread cannot be stopped: it keeps converting, and keeps one of the
:data:`MAX_CONCURRENT` slots, until the conversion's own timeout.

Text-only output: headings (level 1-3; the title is ``doc.title``), paragraphs, list
items, plain-text tables (Preformatted) and a final "Notes" section with the
footnotes, referenced in the text as ``[n]``. Images are dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import sysconfig
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..textmodel import Document, clean_text

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0  # seconds, wall clock, whole conversion
DEFAULT_OCR_TIMEOUT = 600.0  # with OCR on (10-20 s per scanned page on a Raspberry Pi 4)
DEFAULT_MAX_MEMORY_MB = 1024  # RLIMIT_AS of the worker process
DEFAULT_MAX_PAGES = 400
DEFAULT_OCR_MAX_PAGES = 20
DEFAULT_OCR_DPI = 300
MAX_CONCURRENT = 2  # conversions running at once (each may use up to the memory cap)

_PKG_ROOT = Path(__file__).resolve().parents[2]  # the directory that contains "sendtox3"

#: The worker's environment holds these variables only (plus PYTHONPATH and two fixed
#: settings), so a worker taken over by a hostile PDF finds no SUPERVISOR_TOKEN,
#: HASSIO_TOKEN or SENDTOX3_* option in it.
_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP", "PYTHONHOME",
             "TESSDATA_PREFIX", "LD_LIBRARY_PATH", "SYSTEMROOT")
#: Accounts the worker runs as when this process is root (the first that exists).
#: ``SENDTOX3_PDF_WORKER_USER`` names another one; ``root`` keeps root.
_WORKER_USERS = ("sendtox3-pdf", "nobody")
_POLL_SECONDS = 0.05  # how often an async caller checks for a free converter slot


class _Slots:
    """At most ``n`` conversions at once, shared by threads (``with``, blocking) and event
    loops (``async with``, polling). A cancelled async waiter never holds a slot."""

    def __init__(self, n: int):
        self._cond = threading.Condition()
        self._free = n
        self._size = n

    @property
    def free(self) -> int:
        with self._cond:
            return self._free

    def try_acquire(self) -> bool:
        with self._cond:
            if self._free <= 0:
                return False
            self._free -= 1
            return True

    def release(self) -> None:
        with self._cond:
            if self._free >= self._size:
                raise ValueError("converter slot released too many times")
            self._free += 1
            self._cond.notify()

    def __enter__(self) -> "_Slots":
        with self._cond:
            while self._free <= 0:
                self._cond.wait()
            self._free -= 1
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    async def __aenter__(self) -> "_Slots":
        while not self.try_acquire():
            await asyncio.sleep(_POLL_SECONDS)
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()


_slots = _Slots(MAX_CONCURRENT)


@dataclass
class PdfResult:
    doc: Document
    ok: bool
    kind: str = ""  # "", "empty", "invalid", "encrypted", "timeout", "memory", "crash"
    error: str = ""
    stats: dict = field(default_factory=dict)
    elapsed: float = 0.0


def _fallback_title(title_hint: str) -> str:
    t = clean_text(title_hint or "")
    t = re.sub(r"\.pdf$", "", t, flags=re.I).strip()
    return t or "PDF document"


def _failure(kind: str, message: str, *, title_hint: str, source_url: str, elapsed: float = 0.0) -> PdfResult:
    doc = Document(title=_fallback_title(title_hint), blocks=[], source_url=source_url or "", notes=[message])
    return PdfResult(doc=doc, ok=False, kind=kind, error=message, elapsed=elapsed)


def _precheck(data: bytes) -> tuple[str, str] | None:
    if not data:
        return "empty", "The PDF file is empty."
    if b"%PDF-" not in data[:1024]:
        return "invalid", "This file is not a PDF."
    return None


def _ocr_default(ocr: bool | None) -> bool:
    if ocr is not None:
        return bool(ocr)
    return os.environ.get("SENDTOX3_PDF_OCR", "").strip().lower() in ("1", "true", "yes", "on")


def _worker_options(*, title_hint, source_url, ocr, timeout, max_memory_mb, max_pages, ocr_lang,
                    ocr_max_pages, ocr_dpi, debug) -> dict:
    return {
        "title_hint": title_hint or "",
        "source_url": source_url or "",
        "ocr": bool(ocr),
        "ocr_lang": ocr_lang or "eng",
        "ocr_max_pages": int(ocr_max_pages),
        "ocr_dpi": int(ocr_dpi),
        "max_pages": int(max_pages),
        "max_memory_mb": int(max_memory_mb),
        # the worker stops starting OCR pages before the parent kills it
        "budget": max(1.0, float(timeout) - 5.0) if timeout else 0,
        "cpu_seconds": int(timeout) + 15 if timeout else 0,
        "debug": bool(debug),
    }


def _command(opts: dict) -> list[str]:
    exe = sys.executable or "python3"
    return [exe, "-s", "-m", "sendtox3.extract.pdfengine.worker", json.dumps(opts)]


def _env() -> dict:
    """The worker's whole environment: what Python, PDFium and tesseract need, no secrets."""
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
    pp = os.environ.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_PKG_ROOT) + (os.pathsep + pp if pp else "")
    env["OMP_THREAD_LIMIT"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


_DIR = stat.S_IROTH | stat.S_IXOTH
_root_warned: set[str] = set()  # which "the worker stays root" warnings were logged
_root_warned_lock = threading.Lock()


def _warn_root(key: str, msg: str, *args) -> None:
    """Log, once per process and reason, that the PDF worker keeps running as root. Only
    account names, uids and paths go in the message: never the environment."""
    with _root_warned_lock:
        first = key not in _root_warned
        _root_warned.add(key)
    log.log(logging.WARNING if first else logging.DEBUG,
            msg + "; the PDF worker runs as root" + ("" if first else " (repeat)"), *args)


def _reachable(path: Path, bits: int) -> bool:
    """Whether every user can reach ``path``: o+x on each directory above it and the
    "other" permission ``bits`` on ``path`` itself."""
    try:
        p = path.resolve()
        if any(not d.stat().st_mode & stat.S_IXOTH for d in p.parents):
            return False
        return p.stat().st_mode & bits == bits
    except OSError:
        return False


def _unprivileged() -> dict:
    """Popen arguments that run the worker as an unprivileged account when this process
    is root, as in the add-on container. A worker taken over by a hostile PDF then
    cannot read ``/proc/<server pid>/environ`` or any other file only root may read,
    and cannot signal the server. ``{}`` when not root, when no such
    account exists, or when the code the worker imports is not readable by everyone
    (a checkout under ``/root``, say), so the worker would not even start."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return {}
    try:
        import pwd
    except ImportError:  # pragma: no cover - not POSIX
        return {}
    wanted = os.environ.get("SENDTOX3_PDF_WORKER_USER", "").strip()
    if wanted in ("root", "0"):
        return {}
    account = None
    for name in [wanted] if wanted else _WORKER_USERS:
        try:
            account = pwd.getpwnam(name)
            break
        except KeyError:
            continue
    if account is None or account.pw_uid == 0:
        if wanted:
            _warn_root("user", "SENDTOX3_PDF_WORKER_USER=%s is not an unprivileged account", wanted)
        else:
            _warn_root("account", "none of the accounts %s exists", ", ".join(_WORKER_USERS))
        return {}
    code = ((_PKG_ROOT / "sendtox3", _DIR), (Path(__file__).parent / "pdfengine" / "worker.py", stat.S_IROTH),
            (Path(sysconfig.get_paths()["purelib"]), _DIR), (Path(sys.executable), stat.S_IXOTH))
    unreadable = [str(path) for path, bits in code if not _reachable(path, bits)]
    if unreadable:  # the worker could not even start as that account
        _warn_root("code", "%s cannot be read by other users", unreadable[0])
        return {}
    return {"user": account.pw_uid, "group": account.pw_gid, "extra_groups": [], "cwd": str(_PKG_ROOT)}


def _popen_args(identity: dict) -> dict:
    # own session/process group, so killing it also kills a running tesseract
    return dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env(),
                start_new_session=True, **identity)


def _warn_setuid(identity: dict, e: PermissionError) -> None:
    # e.g. no CAP_SETUID/CAP_SETGID in the container; the conversion still goes ahead
    _warn_root("setuid", "cannot start the PDF worker as uid %s (%s)", identity["user"],
               e.strerror or type(e).__name__)


def _start(opts: dict) -> subprocess.Popen:
    identity = _unprivileged()
    try:
        return subprocess.Popen(_command(opts), **_popen_args(identity))
    except PermissionError as e:
        if not identity:
            raise
        _warn_setuid(identity, e)
        return subprocess.Popen(_command(opts), **_popen_args({}))


async def _start_async(opts: dict) -> asyncio.subprocess.Process:
    identity = _unprivileged()
    try:
        return await asyncio.create_subprocess_exec(*_command(opts), **_popen_args(identity))
    except PermissionError as e:
        if not identity:
            raise
        _warn_setuid(identity, e)
        return await asyncio.create_subprocess_exec(*_command(opts), **_popen_args({}))


def _parse(stdout: bytes, returncode: int, stderr: bytes, *, title_hint: str, source_url: str,
           elapsed: float) -> PdfResult:
    from .pdfengine.worker import doc_from_dict

    res = None
    if stdout:
        try:
            res = json.loads(stdout.decode("utf-8", "replace"))
        except ValueError:
            res = None
    if isinstance(res, dict) and res.get("ok") and isinstance(res.get("doc"), dict):
        doc = doc_from_dict(res["doc"])
        return PdfResult(doc=doc, ok=True, stats=res.get("stats") or {}, elapsed=elapsed)
    if isinstance(res, dict) and not res.get("ok"):
        kind = str(res.get("kind") or "crash")
        if kind == "memory":
            msg = "This PDF needs more memory than the converter may use, so it could not be converted."
        elif kind == "crash":
            msg = "The PDF converter failed on this file, so it could not be converted."
        else:
            msg = str(res.get("error") or "This PDF could not be converted.")
        if kind == "crash":
            log.warning("PDF worker error: %s", res.get("error"))
        return _failure(kind, msg, title_hint=title_hint, source_url=source_url, elapsed=elapsed)
    tail = stderr.decode("utf-8", "replace").strip().splitlines()[-3:] if stderr else []
    log.warning("PDF worker exited with %s: %s", returncode, " | ".join(tail))
    if returncode in (-9, -24, 137, 152) or any("MemoryError" in t for t in tail):
        # SIGKILL (kernel OOM killer) / SIGXCPU (CPU limit)
        msg = "This PDF needs more memory or time than the converter may use, so it could not be converted."
        return _failure("memory", msg, title_hint=title_hint, source_url=source_url, elapsed=elapsed)
    # SIGABRT/SIGSEGV: PDFium aborts when an allocation fails under the memory cap
    return _failure("crash", "The PDF converter failed on this file (it may be damaged or too large), "
                    "so it could not be converted.", title_hint=title_hint, source_url=source_url, elapsed=elapsed)


def _prepare(data: bytes, *, title_hint: str, source_url: str, ocr: bool | None, timeout: float | None,
             max_memory_mb: int, max_pages: int, ocr_lang: str, ocr_max_pages: int, ocr_dpi: int,
             debug: bool) -> tuple[PdfResult | None, dict, float]:
    """(early failure or None, worker options, timeout in seconds)."""
    bad = _precheck(data)
    if bad is not None:
        return _failure(bad[0], bad[1], title_hint=title_hint, source_url=source_url), {}, 0.0
    ocr = _ocr_default(ocr)
    if timeout is None:
        timeout = DEFAULT_OCR_TIMEOUT if ocr else DEFAULT_TIMEOUT
    opts = _worker_options(title_hint=title_hint, source_url=source_url, ocr=ocr, timeout=timeout,
                           max_memory_mb=max_memory_mb, max_pages=max_pages, ocr_lang=ocr_lang,
                           ocr_max_pages=ocr_max_pages, ocr_dpi=ocr_dpi, debug=debug)
    return None, opts, timeout


def _in_process(data: bytes, opts: dict, *, title_hint: str, source_url: str, t0: float) -> PdfResult:
    from .pdfengine.worker import run

    try:
        res = run(data, opts)
    except Exception:  # never raise
        log.exception("PDF conversion failed")
        return _failure("crash", "The PDF converter failed on this file, so it could not be converted.",
                        title_hint=title_hint, source_url=source_url, elapsed=time.monotonic() - t0)
    payload = json.dumps(res).encode("utf-8")
    return _parse(payload, 0, b"", title_hint=title_hint, source_url=source_url, elapsed=time.monotonic() - t0)


def _not_started(e: OSError, *, title_hint: str, source_url: str, t0: float) -> PdfResult:
    log.error("cannot start the PDF worker: %s", e)
    return _failure("crash", "The PDF converter could not be started.", title_hint=title_hint,
                    source_url=source_url, elapsed=time.monotonic() - t0)


def _timed_out(timeout: float, *, title_hint: str, source_url: str, t0: float) -> PdfResult:
    secs = int(round(timeout))
    return _failure("timeout", f"Converting this PDF took longer than {secs} seconds, so it was stopped.",
                    title_hint=title_hint, source_url=source_url, elapsed=time.monotonic() - t0)


def convert_pdf(data: bytes, *, title_hint: str = "", source_url: str = "", ocr: bool | None = None,
                timeout: float | None = None, max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
                max_pages: int = DEFAULT_MAX_PAGES, ocr_lang: str = "eng",
                ocr_max_pages: int = DEFAULT_OCR_MAX_PAGES, ocr_dpi: int = DEFAULT_OCR_DPI,
                isolate: bool = True, debug: bool = False) -> PdfResult:
    """Convert a PDF; like :func:`pdf_to_document` but also says whether it worked.

    Blocking; from asyncio use :func:`convert_pdf_async`. ``ocr=None`` reads
    ``SENDTOX3_PDF_OCR``; pass ``opts.pdf_ocr``. ``timeout=None`` means 120 s, or 600 s
    with OCR on, counted from when a converter slot is free; when OCR runs out of time
    the pages recognized so far are kept. ``isolate=False`` converts in this process
    (tests, debugging): no time or memory limits apply then.
    """
    t0 = time.monotonic()
    bad, opts, timeout = _prepare(data, title_hint=title_hint, source_url=source_url, ocr=ocr, timeout=timeout,
                                  max_memory_mb=max_memory_mb, max_pages=max_pages, ocr_lang=ocr_lang,
                                  ocr_max_pages=ocr_max_pages, ocr_dpi=ocr_dpi, debug=debug)
    if bad is not None:
        return bad
    if not isolate:
        return _in_process(data, opts, title_hint=title_hint, source_url=source_url, t0=t0)
    with _slots:
        try:
            proc = _start(opts)
        except OSError as e:
            return _not_started(e, title_hint=title_hint, source_url=source_url, t0=t0)
        try:
            out, err = proc.communicate(data, timeout=timeout if timeout else None)
        except subprocess.TimeoutExpired:
            _kill(proc)
            return _timed_out(timeout, title_hint=title_hint, source_url=source_url, t0=t0)
        except BaseException:
            _kill(proc)
            raise
    return _parse(out, proc.returncode, err, title_hint=title_hint, source_url=source_url,
                  elapsed=time.monotonic() - t0)


async def convert_pdf_async(data: bytes, *, title_hint: str = "", source_url: str = "", ocr: bool | None = None,
                            timeout: float | None = None, max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
                            max_pages: int = DEFAULT_MAX_PAGES, ocr_lang: str = "eng",
                            ocr_max_pages: int = DEFAULT_OCR_MAX_PAGES, ocr_dpi: int = DEFAULT_OCR_DPI,
                            isolate: bool = True, debug: bool = False) -> PdfResult:
    """:func:`convert_pdf` for asyncio, without a thread. Cancelling it (an outer
    ``asyncio.wait_for`` budget, shutdown) kills the worker and its tesseract at once
    and frees the converter slot. Waiting for a slot is not charged to ``timeout``."""
    t0 = time.monotonic()
    bad, opts, timeout = _prepare(data, title_hint=title_hint, source_url=source_url, ocr=ocr, timeout=timeout,
                                  max_memory_mb=max_memory_mb, max_pages=max_pages, ocr_lang=ocr_lang,
                                  ocr_max_pages=ocr_max_pages, ocr_dpi=ocr_dpi, debug=debug)
    if bad is not None:
        return bad
    if not isolate:
        return await asyncio.to_thread(_in_process, data, opts, title_hint=title_hint, source_url=source_url, t0=t0)
    async with _slots:
        try:
            proc = await _start_async(opts)
        except OSError as e:
            return _not_started(e, title_hint=title_hint, source_url=source_url, t0=t0)
        try:
            out, err = await asyncio.wait_for(proc.communicate(data), timeout if timeout else None)
        except TimeoutError:
            await _kill_async(proc)
            return _timed_out(timeout, title_hint=title_hint, source_url=source_url, t0=t0)
        except BaseException:  # cancelled: never leave the worker running
            await _kill_async(proc)
            raise
    return _parse(out, proc.returncode, err, title_hint=title_hint, source_url=source_url,
                  elapsed=time.monotonic() - t0)


def _kill_group(pid: int) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill(proc: subprocess.Popen) -> None:
    _kill_group(proc.pid)
    try:
        proc.communicate(timeout=10)
    except Exception:  # pragma: no cover
        pass


async def _kill_async(proc: asyncio.subprocess.Process) -> None:
    _kill_group(proc.pid)
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except TimeoutError:  # pragma: no cover
        pass


def pdf_to_document(data: bytes, *, title_hint: str = "", source_url: str = "", ocr: bool | None = None,
                    timeout: float | None = None, max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
                    max_pages: int = DEFAULT_MAX_PAGES, ocr_lang: str = "eng",
                    ocr_max_pages: int = DEFAULT_OCR_MAX_PAGES, isolate: bool = True) -> Document:
    """PDF bytes -> Document. Never raises; blocking. From asyncio, await
    :func:`pdf_to_document_async` instead of running this in a thread (a cancelled
    thread keeps the conversion and its slot until the timeout)."""
    return convert_pdf(data, title_hint=title_hint, source_url=source_url, ocr=ocr, timeout=timeout,
                       max_memory_mb=max_memory_mb, max_pages=max_pages, ocr_lang=ocr_lang,
                       ocr_max_pages=ocr_max_pages, isolate=isolate).doc


async def pdf_to_document_async(data: bytes, *, title_hint: str = "", source_url: str = "",
                                ocr: bool | None = None, timeout: float | None = None,
                                max_memory_mb: int = DEFAULT_MAX_MEMORY_MB, max_pages: int = DEFAULT_MAX_PAGES,
                                ocr_lang: str = "eng", ocr_max_pages: int = DEFAULT_OCR_MAX_PAGES,
                                isolate: bool = True) -> Document:
    """``await``-able :func:`pdf_to_document`; cancellable (see :func:`convert_pdf_async`)."""
    res = await convert_pdf_async(data, title_hint=title_hint, source_url=source_url, ocr=ocr, timeout=timeout,
                                  max_memory_mb=max_memory_mb, max_pages=max_pages, ocr_lang=ocr_lang,
                                  ocr_max_pages=ocr_max_pages, isolate=isolate)
    return res.doc
