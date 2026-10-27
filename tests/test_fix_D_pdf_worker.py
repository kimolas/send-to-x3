"""PDF worker lifecycle and sandbox (sendtox3.extract.pdf).

Regression tests for review findings: a cancelled conversion (an outer per-item
budget) must kill its worker and free its converter slot at once (15, 10), and the
worker must not inherit the server's secrets or root privileges (41).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("pypdfium2")

from sendtox3.extract import pdf  # noqa: E402

_GEN = Path(__file__).parent / "fixtures" / "pdfs" / "pdfgen.py"
_spec = importlib.util.spec_from_file_location("pdfgen", _GEN)
pdfgen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pdfgen)

FAKE_PDF = b"%PDF-1.7\n fake, never parsed by the fake workers below"

# Fake workers. They follow the real worker's protocol: PDF on stdin, one JSON object on stdout.
SLEEPER = ("import os, sys, time\n"
           "open(sys.argv[1], 'a').write(f'{os.getpid()}\\n')\n"
           "sys.stdin.buffer.read()\n"
           "time.sleep(60)\n")
NAPPER = ("import json, sys, time\n"
          "sys.stdin.buffer.read()\n"
          "time.sleep(float(sys.argv[1]))\n"
          "print(json.dumps({'ok': True, 'doc': {'title': 'T', 'blocks': [{'t': 'p', 'text': 'done'}]}}))\n")
ENV_REPORTER = ("import json, os, sys\n"
                "sys.stdin.buffer.read()\n"
                "print(json.dumps({'ok': False, 'kind': 'invalid',\n"
                "                  'error': json.dumps({'env': sorted(os.environ), 'uid': os.getuid()})}))\n")

SECRETS = {"SUPERVISOR_TOKEN": "sv-secret", "HASSIO_TOKEN": "ha-secret", "SENDTOX3_ADMIN_TOKEN": "adm-secret",
           "SENDTOX3_IMAP_PASSWORD": "imap-secret"}


def _real_pdf() -> bytes:
    p = pdfgen.Page()
    p.text(72, 100, "After the cancelled conversions this one still runs.", size=10)
    return pdfgen.build([p])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - someone else's process reused the pid
        return False
    return True


def _pids(path: Path) -> list[int]:
    return [int(x) for x in path.read_text().split()] if path.exists() else []


async def _until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


# ---------------------------------------------------------------------------------------
# cancellation (findings 15 and 10)
# ---------------------------------------------------------------------------------------


async def test_outer_budget_kills_the_workers_and_frees_their_slots(monkeypatch, tmp_path):
    pid_file = tmp_path / "pids"
    real_command = pdf._command
    monkeypatch.setattr(pdf, "_command", lambda opts: [sys.executable, "-c", SLEEPER, str(pid_file)])

    t0 = time.monotonic()
    # As ingest does: the per-item budget (here 1 s) runs out long before the
    # conversion's own timeout (30 s), once for every converter slot.
    results = await asyncio.gather(
        *(asyncio.wait_for(pdf.pdf_to_document_async(FAKE_PDF, timeout=30), 1.0) for _ in range(pdf.MAX_CONCURRENT)),
        return_exceptions=True,
    )
    assert all(isinstance(r, TimeoutError) for r in results), results
    assert time.monotonic() - t0 < 5
    pids = _pids(pid_file)
    assert len(pids) == pdf.MAX_CONCURRENT
    assert await _until(lambda: not any(_alive(p) for p in pids)), "a cancelled worker is still running"
    assert pdf._slots.free == pdf.MAX_CONCURRENT

    # The next PDF does not queue behind the abandoned conversions.
    monkeypatch.setattr(pdf, "_command", real_command)
    doc = await asyncio.wait_for(pdf.pdf_to_document_async(_real_pdf(), title_hint="after"), 15)
    assert "still runs" in " ".join(b.text for b in doc.blocks)


async def test_own_timeout_kills_the_worker_with_a_pdf_message(monkeypatch, tmp_path):
    pid_file = tmp_path / "pids"
    monkeypatch.setattr(pdf, "_command", lambda opts: [sys.executable, "-c", SLEEPER, str(pid_file)])
    r = await pdf.convert_pdf_async(FAKE_PDF, title_hint="scan.pdf", timeout=0.5)
    assert not r.ok and r.kind == "timeout"
    assert r.doc.notes[0].startswith("Converting this PDF took longer than")
    assert r.doc.title == "scan"
    assert not any(_alive(p) for p in _pids(pid_file))
    assert pdf._slots.free == pdf.MAX_CONCURRENT


async def test_waiting_for_a_slot_is_not_charged_to_the_timeout(monkeypatch):
    monkeypatch.setattr(pdf, "_command", lambda opts: [sys.executable, "-c", NAPPER, opts["title_hint"]])
    blockers = [asyncio.create_task(pdf.convert_pdf_async(FAKE_PDF, title_hint="1.5", timeout=20))
                for _ in range(pdf.MAX_CONCURRENT)]
    await _until(lambda: pdf._slots.free == 0)
    # Gives up while queued: no slot is taken or leaked.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(pdf.convert_pdf_async(FAKE_PDF, title_hint="0", timeout=20), 0.2)
    # Queued for ~1.5 s, converts in 0.1 s: a 1 s timeout is enough.
    late = await pdf.convert_pdf_async(FAKE_PDF, title_hint="0.1", timeout=1.0)
    assert late.ok, late.error
    assert all(r.ok for r in await asyncio.gather(*blockers))
    assert pdf._slots.free == pdf.MAX_CONCURRENT


def test_sync_and_async_callers_share_the_slots():
    held = [pdf._slots.try_acquire() for _ in range(pdf.MAX_CONCURRENT)]
    try:
        assert all(held) and not pdf._slots.try_acquire()
    finally:
        for _ in held:
            pdf._slots.release()
    assert pdf._slots.free == pdf.MAX_CONCURRENT
    with pytest.raises(ValueError):
        pdf._slots.release()


# ---------------------------------------------------------------------------------------
# sandbox (finding 41)
# ---------------------------------------------------------------------------------------


def _reported(r) -> dict:
    assert r.kind == "invalid", (r.kind, r.error)
    return json.loads(r.error)


@pytest.mark.parametrize("use_async", [False, True])
async def test_worker_environment_has_no_secrets(monkeypatch, use_async):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(pdf, "_command", lambda opts: [sys.executable, "-c", ENV_REPORTER])
    r = await pdf.convert_pdf_async(FAKE_PDF) if use_async else pdf.convert_pdf(FAKE_PDF)
    env = set(_reported(r)["env"])
    assert not env & set(SECRETS)
    assert not any(k.startswith("SENDTOX3_") for k in env)
    assert {"PYTHONPATH", "OMP_THREAD_LIMIT", "PYTHONDONTWRITEBYTECODE"} <= env


def test_env_keeps_what_the_worker_needs(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("TESSDATA_PREFIX", "/usr/share/tessdata")
    monkeypatch.setenv("PYTHONPATH", "/extra")
    env = pdf._env()
    assert env["TESSDATA_PREFIX"] == "/usr/share/tessdata"
    assert env["PYTHONPATH"] == str(pdf._PKG_ROOT) + os.pathsep + "/extra"
    assert env.get("PATH") == os.environ.get("PATH")
    assert not set(env) & set(SECRETS)


class _Pw:
    def __init__(self, uid: int, gid: int):
        self.pw_uid, self.pw_gid = uid, gid


@pytest.fixture
def as_root(monkeypatch):
    pwd = pytest.importorskip("pwd")
    accounts = {"nobody": _Pw(65534, 65534), "root": _Pw(0, 0), "reader": _Pw(1234, 99)}

    def getpwnam(name):
        try:
            return accounts[name]
        except KeyError:
            raise KeyError(name) from None

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(pdf, "_reachable", lambda path, bits: True)
    monkeypatch.delenv("SENDTOX3_PDF_WORKER_USER", raising=False)
    monkeypatch.setattr(pdf, "_root_warned", set())  # each test sees its first warning
    return accounts


def test_root_runs_the_worker_unprivileged(as_root, monkeypatch):
    assert pdf._unprivileged() == {"user": 65534, "group": 65534, "extra_groups": [], "cwd": str(pdf._PKG_ROOT)}
    monkeypatch.setenv("SENDTOX3_PDF_WORKER_USER", "reader")
    assert pdf._unprivileged()["user"] == 1234 and pdf._unprivileged()["group"] == 99
    monkeypatch.setenv("SENDTOX3_PDF_WORKER_USER", "root")
    assert pdf._unprivileged() == {}
    monkeypatch.setenv("SENDTOX3_PDF_WORKER_USER", "no-such-user")
    assert pdf._unprivileged() == {}


def test_no_privilege_change_when_unsafe_or_unneeded(as_root, monkeypatch):
    # the code the worker imports is not readable by the account: it would not start
    monkeypatch.setattr(pdf, "_reachable", lambda path, bits: False)
    assert pdf._unprivileged() == {}
    monkeypatch.setattr(pdf, "_reachable", lambda path, bits: True)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)  # not root: nothing to drop
    assert pdf._unprivileged() == {}


def test_reachable(tmp_path):
    import stat

    assert pdf._reachable(Path("/usr"), pdf._DIR)
    private = tmp_path / "private"
    private.mkdir()
    (private / "f.py").write_text("x = 1\n")
    private.chmod(0o700)
    try:
        assert not pdf._reachable(private, pdf._DIR)
        assert not pdf._reachable(private / "f.py", stat.S_IROTH)  # a directory above is closed
        assert not pdf._reachable(private / "missing", stat.S_IROTH)
    finally:
        private.chmod(0o755)


class _FakePopen:
    calls: list[dict] = []
    refuse_user = False

    def __init__(self, cmd, **kw):
        type(self).calls.append(kw)
        if self.refuse_user and "user" in kw:
            raise PermissionError(1, "Operation not permitted")
        self.pid = 999_999
        self.returncode = 0

    def communicate(self, data=None, timeout=None):
        return json.dumps({"ok": True, "doc": {"title": "T", "blocks": [{"t": "p", "text": "x"}]}}).encode(), b""


def test_start_passes_the_account_to_popen(as_root, monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(_FakePopen, "calls", [])
    monkeypatch.setattr(pdf.subprocess, "Popen", _FakePopen)
    assert pdf.convert_pdf(FAKE_PDF).ok
    (kw,) = _FakePopen.calls
    assert kw["user"] == 65534 and kw["group"] == 65534 and kw["extra_groups"] == []
    assert kw["start_new_session"] is True
    assert not set(kw["env"]) & set(SECRETS)


def test_start_falls_back_when_the_account_cannot_be_used(as_root, monkeypatch, caplog):
    monkeypatch.setattr(_FakePopen, "calls", [])
    monkeypatch.setattr(_FakePopen, "refuse_user", True)
    monkeypatch.setattr(pdf.subprocess, "Popen", _FakePopen)
    with caplog.at_level(logging.WARNING, logger=pdf.log.name):
        assert pdf.convert_pdf(FAKE_PDF).ok
    assert len(_FakePopen.calls) == 2 and "user" not in _FakePopen.calls[1]
    assert "runs as root" in caplog.text


@pytest.mark.skipif(not (sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs root on Linux (the add-on container)")
def test_worker_really_runs_unprivileged_as_root(monkeypatch):  # pragma: no cover - container only
    if not pdf._unprivileged():
        pytest.skip("no unprivileged account, or the code is not world-readable")
    monkeypatch.setattr(pdf, "_command", lambda opts: [sys.executable, "-c", ENV_REPORTER])
    assert _reported(pdf.convert_pdf(FAKE_PDF))["uid"] != 0
    monkeypatch.undo()
    assert pdf.convert_pdf(_real_pdf()).ok
