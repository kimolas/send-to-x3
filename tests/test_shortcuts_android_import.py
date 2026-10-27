"""The Android import file through HTTP Shortcuts' own import code (Docker + network).

``shortcuts/tools/http_shortcuts_check.py`` compiles the app's import pipeline and
placeholder code from its source at v4.9.0 and runs it on the generated file. Run with
``SENDTOX3_DOCKER_TESTS=1 SENDTOX3_NETWORK_TESTS=1`` (first run downloads ~90 MB, cached
under ``$SENDTOX3_HTTP_SHORTCUTS_CACHE`` or the system temp dir).
"""

from __future__ import annotations

import json
import shutil

import pytest
from test_shortcuts import SHORTCUTS, _load, ba

pytestmark = [pytest.mark.docker, pytest.mark.network]

check = _load("sendtox3_http_shortcuts_check", SHORTCUTS / "tools" / "http_shortcuts_check.py")

VALUES = {"sendtox3_url": "http://192.168.1.50:8765/api/v1/ingest", "sendtox3_token": "tok-123"}
CASES = [
    {"text": "https://example.com/article?id=1&utm_source=x", "title": "A page"},
    {"text": 'Quote "this", a backslash \\ and </script>\nnext line\ttab\u0001', "title": 'Ti"tle'},
    {"text": "Ünïcödé 中文 😀", "title": ""},
    {"text": "", "title": ""},
]


@pytest.fixture(scope="module")
def result() -> dict:
    if shutil.which("docker") is None or shutil.which("git") is None:
        pytest.skip("needs docker and git")
    return check.check_import(ba.DEFAULT_OUT, [{**c, "values": VALUES} for c in CASES])


def test_the_app_accepts_the_file(result):
    assert result["version"] == 92 and result["compatibilityVersion"] == 90
    s = result["shortcut"]
    assert (s["executionType"], s["method"], s["requestBodyType"]) == ("app", "POST", "custom_text")
    assert s["contentType"] == "application/json" and s["timeout"] == 30000
    assert s["waitForInternet"] is True and s["launcherShortcut"] is True and s["excludeFromFileSharing"] is True
    assert s["responseHandling"]["uiType"] == "toast"
    assert s["responseHandling"]["successOutput"] == "none" and s["responseHandling"]["failureOutput"] == "none"


def test_it_is_a_text_share_target_and_every_placeholder_resolves(result):
    assert result["isTextShareTarget"] is True and result["unresolvedIdentifiers"] == []
    assert set(result["usedVariableKeys"]) == {"sendtox3_url", "sendtox3_token", "sendtox3_email",
                                               "sendtox3_text", "sendtox3_title"}


def test_rendered_requests(result):
    for case, rendered in zip(CASES, result["rendered"]):
        assert rendered["url"] == VALUES["sendtox3_url"]
        assert rendered["headers"] == {"Authorization": "Bearer tok-123"}
        assert "{{" not in rendered["body"]
        assert json.loads(rendered["body"]) == {"text": case["text"], "title": case["title"],
                                                "source": "android-http-shortcuts"}


def test_a_broken_file_is_rejected(tmp_path):
    data = ba.build_http_shortcuts()
    data["variables"][0]["key"] = "not a valid key!"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data))
    with pytest.raises(check.CheckError, match="Invalid variable key"):
        check.check_import(bad)
