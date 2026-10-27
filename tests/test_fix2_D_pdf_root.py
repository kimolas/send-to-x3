"""The PDF worker staying root is logged, once (sendtox3.extract.pdf), review R2-D4.

When this process is root (the add-on container) the worker should run as an
unprivileged account. If that cannot happen (the account cannot be switched to, no
such account exists, the code is not readable by it) the conversion still goes ahead
as root, and one warning per reason says so, without anything from the environment.
"""

from __future__ import annotations

import json
import logging
import os
import sys

import pytest

from sendtox3.extract import pdf

FAKE_PDF = b"%PDF-1.7\n fake, never parsed by the fake Popen below"
SECRETS = {"SUPERVISOR_TOKEN": "sv-secret-123", "HASSIO_TOKEN": "ha-secret-456",
           "SENDTOX3_ADMIN_TOKEN": "adm-secret-789"}


class _Pw:
    def __init__(self, uid: int, gid: int):
        self.pw_uid, self.pw_gid = uid, gid


@pytest.fixture
def as_root(monkeypatch):
    pwd = pytest.importorskip("pwd")
    accounts = {"nobody": _Pw(65534, 65534), "root": _Pw(0, 0)}

    def getpwnam(name):
        try:
            return accounts[name]
        except KeyError:
            raise KeyError(name) from None

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(pdf, "_reachable", lambda path, bits: True)
    monkeypatch.delenv("SENDTOX3_PDF_WORKER_USER", raising=False)
    monkeypatch.setattr(pdf, "_root_warned", set(), raising=False)
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    return accounts


def _ok_stdout() -> bytes:
    return json.dumps({"ok": True, "doc": {"title": "T", "blocks": [{"t": "p", "text": "x"}]}}).encode()


class _RefusingPopen:
    """Popen whose privilege drop fails, as without CAP_SETUID."""

    calls: list[dict] = []

    def __init__(self, cmd, **kw):
        type(self).calls.append(kw)
        if "user" in kw:
            raise PermissionError(1, "Operation not permitted")
        self.pid = 999_999
        self.returncode = 0

    def communicate(self, data=None, timeout=None):
        return _ok_stdout(), b""


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == pdf.log.name and r.levelno >= logging.WARNING]


def _no_secrets(caplog) -> None:
    text = caplog.text
    assert not any(v in text for v in SECRETS.values())


def test_failed_privilege_drop_warns_once_and_keeps_converting(as_root, monkeypatch, caplog):
    monkeypatch.setattr(_RefusingPopen, "calls", [])
    monkeypatch.setattr(pdf.subprocess, "Popen", _RefusingPopen)
    with caplog.at_level(logging.DEBUG, logger=pdf.log.name):
        results = [pdf.convert_pdf(FAKE_PDF) for _ in range(3)]
    assert all(r.ok for r in results)
    # every conversion tried the account first, then went ahead as root
    assert ["user" in kw for kw in _RefusingPopen.calls] == [True, False] * 3
    (warning,) = _warnings(caplog)
    assert "uid 65534" in warning and "Operation not permitted" in warning and "runs as root" in warning
    _no_secrets(caplog)


async def test_async_path_shares_the_single_warning(as_root, monkeypatch, caplog):
    calls: list[dict] = []

    class _Proc:
        pid = 999_998
        returncode = 0

        async def communicate(self, data=None):
            return _ok_stdout(), b""

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        calls.append(kw)
        if "user" in kw:
            raise PermissionError(1, "Operation not permitted")
        return _Proc()

    monkeypatch.setattr(pdf.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(_RefusingPopen, "calls", [])
    monkeypatch.setattr(pdf.subprocess, "Popen", _RefusingPopen)
    with caplog.at_level(logging.WARNING, logger=pdf.log.name):
        assert (await pdf.convert_pdf_async(FAKE_PDF)).ok
        assert (await pdf.convert_pdf_async(FAKE_PDF)).ok
        assert pdf.convert_pdf(FAKE_PDF).ok
    assert ["user" in kw for kw in calls] == [True, False, True, False]
    assert len(_warnings(caplog)) == 1
    _no_secrets(caplog)


def test_missing_account_warns_once(as_root, monkeypatch, caplog):
    as_root.pop("nobody")
    with caplog.at_level(logging.WARNING, logger=pdf.log.name):
        assert pdf._unprivileged() == {}
        assert pdf._unprivileged() == {}
    (warning,) = _warnings(caplog)
    assert "sendtox3-pdf, nobody" in warning and "runs as root" in warning


def test_bad_configured_account_warns_once(as_root, monkeypatch, caplog):
    monkeypatch.setenv("SENDTOX3_PDF_WORKER_USER", "no-such-user")
    with caplog.at_level(logging.WARNING, logger=pdf.log.name):
        assert pdf._unprivileged() == {}
        assert pdf._unprivileged() == {}
    (warning,) = _warnings(caplog)
    assert "SENDTOX3_PDF_WORKER_USER=no-such-user" in warning


def test_unreadable_code_warns_once_with_the_path(as_root, monkeypatch, caplog):
    monkeypatch.setattr(pdf, "_reachable", lambda path, bits: path != pdf.Path(sys.executable))
    with caplog.at_level(logging.WARNING, logger=pdf.log.name):
        assert pdf._unprivileged() == {}
        assert pdf._unprivileged() == {}
    (warning,) = _warnings(caplog)
    assert sys.executable in warning and "cannot be read by other users" in warning


def test_explicit_root_and_non_root_do_not_warn(as_root, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=pdf.log.name):
        monkeypatch.setenv("SENDTOX3_PDF_WORKER_USER", "root")
        assert pdf._unprivileged() == {}  # the user asked for it
        monkeypatch.delenv("SENDTOX3_PDF_WORKER_USER")
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        assert pdf._unprivileged() == {}  # nothing to drop
    assert _warnings(caplog) == []


def test_reasons_are_warned_separately(as_root, monkeypatch, caplog):
    monkeypatch.setattr(_RefusingPopen, "calls", [])
    monkeypatch.setattr(pdf.subprocess, "Popen", _RefusingPopen)
    with caplog.at_level(logging.WARNING, logger=pdf.log.name):
        assert pdf.convert_pdf(FAKE_PDF).ok  # setuid refused
        monkeypatch.setattr(pdf, "_reachable", lambda path, bits: False)
        assert pdf.convert_pdf(FAKE_PDF).ok  # then the code became unreadable
        assert pdf.convert_pdf(FAKE_PDF).ok
    assert len(_warnings(caplog)) == 2


def test_env_allowlist_is_unchanged(as_root):
    env = pdf._env()
    assert not set(env) & set(SECRETS)
    assert not any(k.startswith("SENDTOX3_") for k in env)
