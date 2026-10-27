"""The generated iOS Shortcut as Apple's own WorkflowKit reads it (macOS only).

``shortcuts/tools/wfcheck.m`` loads the file with the private WorkflowKit framework,
the code the Shortcuts app uses, without importing anything into a Shortcuts library.
It checks that every parameter decodes, that the Import Questions and the no-input
behavior are understood, that import migrations leave the actions alone, and it runs
the WorkflowKit-native pieces (If conditions, Get Details of Safari Web Page,
parameter resolution) to pin down the semantics the shortcut relies on.

Signing is tested only with ``SENDTOX3_SHORTCUTS_SIGN=1`` (uses this Mac's iCloud
identity, locally; ``people-who-know-me`` mode, nothing is uploaded).
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_shortcuts import CONFIGURED, SHORTCUTS, bs, find, ident, params

WORKFLOWKIT = Path("/System/Library/PrivateFrameworks/WorkflowKit.framework/WorkflowKit")

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("clang") is None or not WORKFLOWKIT.parent.is_dir(),
    reason="needs macOS with WorkflowKit and clang (Xcode command line tools)")

#: Parameters defined in ActionKit, which cannot be loaded outside Apple's own
#: processes: WorkflowKit reports them as unknown. Their keys come from Cherri and
#: shortcuts-js, which both decompile real shortcuts.
ACTIONKIT_KEYS = {
    "alert": {"WFAlertActionCancelButtonShown", "WFAlertActionMessage", "WFAlertActionTitle"},
    "getvalueforkey": {"WFDictionaryKey", "WFGetDictionaryValueType", "WFInput"},
    "getwifi": {"WFNetworkDetailsNetwork"},
}


@pytest.fixture(scope="module")
def wfcheck(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("wfcheck") / "wfcheck"
    proc = subprocess.run(["clang", "-fobjc-arc", "-Wall", "-framework", "Foundation",
                           str(SHORTCUTS / "tools" / "wfcheck.m"), "-o", str(out)], capture_output=True, text=True,
                          check=False)
    if proc.returncode != 0:
        pytest.skip(f"cannot build wfcheck: {proc.stderr[-500:]}")
    return out


@pytest.fixture(scope="module")
def shortcut(tmp_path_factory) -> tuple[Path, dict]:
    path = bs.write_shortcut(tmp_path_factory.mktemp("shortcut"), CONFIGURED)[0]
    return path, bs.build_workflow(CONFIGURED)


def check(tool: Path, *args: str, env: dict | None = None) -> dict:
    proc = subprocess.run([str(tool), *args], capture_output=True, text=True, timeout=60, check=False,
                          env={**os.environ, **(env or {})})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def index_of(wf: dict, pred) -> int:
    hits = [i for i, a in enumerate(wf["WFWorkflowActions"]) if pred(a)]
    assert len(hits) == 1, hits
    return hits[0]


def if_index(wf: dict, condition: int, variable: str | None = None, value=None) -> int:
    def pred(a):
        p = params(a)
        if ident(a) != "conditional" or p.get("WFControlFlowMode") != 0 or p["WFCondition"] != condition:
            return False
        subject = p["WFInput"]["Variable"]["Value"]
        name = subject.get("VariableName") or subject.get("OutputName") or subject.get("Type")
        return (variable is None or name == variable) and (value is None or p.get("WFConditionalActionString") == value)
    return index_of(wf, pred)


def truth(tool, path, index, *variables, env=None) -> bool:
    out = check(tool, "eval", str(path), str(index), *variables, env=env)
    assert out["kind"] == "condition", out
    return out["truth"]


# ================================================================== decoding


def test_workflowkit_decodes_every_parameter(wfcheck, shortcut):
    path, wf = shortcut
    out = check(wfcheck, "decode", str(path))
    assert [a["identifier"] for a in out["actions"]] == [a["WFWorkflowActionIdentifier"] for a in wf["WFWorkflowActions"]]
    for i, (decoded, action) in enumerate(zip(out["actions"], wf["WFWorkflowActions"])):
        name = ident(action)
        assert set(decoded["serialized_parameters"]) == set(params(action)), f"action {i} lost parameters"
        if name == "conditional":
            if params(action)["WFControlFlowMode"] == 0:  # single-condition keys become the condition table
                [row] = decoded["conditions"]["Value"]["WFActionParameterFilterTemplates"]
                assert row["WFCondition"] == params(action)["WFCondition"]
                assert row["WFInput"] == params(action)["WFInput"]
            continue
        assert set(decoded["unknown_keys"]) <= ACTIONKIT_KEYS.get(name, set()), f"action {i} {name}: {decoded}"


def test_import_questions_and_no_input_behavior(wfcheck, shortcut):
    path, wf = shortcut
    out = check(wfcheck, "decode", str(path))
    assert out["no_input_behavior"] == "WFWorkflowNoInputBehaviorGetClipboard"
    questions = out["import_questions"]
    assert [q["action_index"] for q in questions] == [1, 3, 5, 7, 9]
    for q, original in zip(questions, wf["WFWorkflowImportQuestions"]):
        assert q["parameter"] == "WFTextActionText" and q["default_state"] == "WFVariableStringParameterState"
        assert q["reserialized"] == original  # WorkflowKit writes back exactly what we wrote


def test_import_migrations_change_nothing(wfcheck, shortcut):
    path, _ = shortcut
    migration = check(wfcheck, "decode", str(path))["migration"]
    assert migration["changed_actions"] == [] and set(migration["changed_keys"]) <= {"WFWorkflowClientVersion"}


def test_committed_file_decodes(wfcheck):
    out = check(wfcheck, "decode", str(SHORTCUTS / "dist" / "Send to X3.shortcut"))
    assert len(out["import_questions"]) == 5 and out["migration"]["changed_actions"] == []


# ================================================================ semantics


@pytest.mark.parametrize("kind, expected", [("page", True), ("text", False), ("url", False), ("rich", False)])
def test_safari_page_branch(wfcheck, shortcut, kind, expected):
    path, wf = shortcut
    i = if_index(wf, bs.COND_HAS_ANY_VALUE, "ExtensionInput")
    assert truth(wfcheck, path, i, env={"WF_INPUT_KIND": kind, "WF_INPUT": "https://example.com/a"}) is expected


def test_safari_page_details(wfcheck, shortcut):
    path, wf = shortcut
    env = {"WF_INPUT_KIND": "page", "WF_INPUT": "https://example.com/a?x=1", "WF_PAGE_TITLE": "A page"}
    url_i = index_of(wf, lambda a: ident(a) == "properties.safariwebpage"
                     and params(a)["WFContentItemPropertyName"] == "Page URL")
    name_i = index_of(wf, lambda a: ident(a) == "properties.safariwebpage"
                      and params(a)["WFContentItemPropertyName"] == "Name")
    assert check(wfcheck, "eval", str(path), str(url_i), env=env)["items"] == [
        {"class": "WFURLContentItem", "value": "https://example.com/a?x=1"}]
    assert check(wfcheck, "eval", str(path), str(name_i), env=env)["items"] == [
        {"class": "WFStringContentItem", "value": "A page"}]


def test_is_compares_case_sensitively_so_mode_is_normalised(wfcheck, shortcut):
    path, wf = shortcut
    i = if_index(wf, bs.COND_IS, "Mode", "http")
    assert truth(wfcheck, path, i, "Mode=http") is True
    assert truth(wfcheck, path, i, "Mode=HTTP") is False  # hence the case-insensitive Replace Text first
    patterns = [params(a)["WFReplaceTextFind"] for a in find(wf, "text.replace")[:2]]
    assert patterns == [bs.MODE_HTTP_RE, bs.MODE_EMAIL_RE]


def test_home_wifi_contains(wfcheck, shortcut):
    path, wf = shortcut
    i = if_index(wf, bs.COND_CONTAINS, "HomeWiFiList")
    home = "HomeWiFiList=,Home,Guest Net,"
    assert truth(wfcheck, path, i, home, "CurrentWiFi=Home") is True
    assert truth(wfcheck, path, i, home, "CurrentWiFi=Guest Net") is True
    assert truth(wfcheck, path, i, home, "CurrentWiFi=home") is False
    assert truth(wfcheck, path, i, home, "CurrentWiFi=Hom") is False
    assert truth(wfcheck, path, i, home, "CurrentWiFi=") is False     # not on Wi-Fi
    assert truth(wfcheck, path, i, home, "CurrentWiFi:=") is False    # empty name


def test_empty_text_counts_as_a_value_so_blank_checks_use_a_probe(wfcheck, shortcut, tmp_path):
    # WorkflowKit: "has any value" is true for an empty text item ...
    b = bs.ShortcutBuilder(seed="probe")
    b.set_variable("X", b.replace("r", [b.text("t", "")], "x", "y"))
    with b.if_(bs.variable("X"), bs.COND_HAS_ANY_VALUE):
        b.comment("-")
    test_wf = {**bs.build_workflow(), "WFWorkflowActions": b.actions, "WFWorkflowImportQuestions": []}
    test_file = tmp_path / "probe.shortcut"
    test_file.write_bytes(plistlib.dumps(test_wf, fmt=plistlib.FMT_BINARY))
    i = if_index(test_wf, bs.COND_HAS_ANY_VALUE, "X")
    assert truth(wfcheck, test_file, i, "X:=") is True
    assert truth(wfcheck, test_file, i, "X=") is False
    # ... so the shortcut asks whether "<" + value + ">" is "<>" instead.
    path, wf = shortcut
    probe = index_of(wf, lambda a: ident(a) == "gettext" and isinstance(params(a)["WFTextActionText"], dict)
                     and params(a)["WFTextActionText"]["Value"]["string"] == "<￼>"
                     and params(a)["WFTextActionText"]["Value"]["attachmentsByRange"]["{1, 1}"].get("VariableName")
                     == "IngestURL")
    for value, expected in (("IngestURL:=", "<>"), ("IngestURL=", "<>"), ("IngestURL=http://x", "<http://x>")):
        assert check(wfcheck, "params", str(path), str(probe), value)["processed"]["WFTextActionText"] == expected
    assert params(wf["WFWorkflowActions"][probe + 1])["WFConditionalActionString"] == bs.BLANK


def test_http_request_parameters_resolve(wfcheck, shortcut):
    path, wf = shortcut
    i = index_of(wf, lambda a: ident(a) == "downloadurl")
    out = check(wfcheck, "params", str(path), str(i), "IngestURL=http://192.168.1.50:8765/api/v1/ingest",
                "IngestToken=tok-123", "SharedURL=https://example.com/a", 'SharedText=He said "hi"\nbye',
                "SharedTitle=Ünïcode")
    assert out["input_parameter"] == {"key": "WFURL", "value": "http://192.168.1.50:8765/api/v1/ingest"}
    p = out["processed"]
    assert (p["WFHTTPMethod"], p["WFHTTPBodyType"]) == ("POST", "JSON")
    assert p["WFHTTPHeaders"] == {"Authorization": "Bearer tok-123"}
    assert p["WFJSONValues"] == {"url": "https://example.com/a", "text": 'He said "hi"\nbye', "title": "Ünïcode",
                                 "source": "ios-shortcut"}


def test_email_parameters_resolve(wfcheck, shortcut):
    path, wf = shortcut
    i = index_of(wf, lambda a: ident(a) == "sendemail")
    out = check(wfcheck, "params", str(path), str(i), "IngestEmail=me+x3@example.com",
                "EmailBody=https://example.com/a\n\nBody", "SharedTitle=A title")
    assert out["input_parameter"] == {"key": "WFSendEmailActionInputAttachments", "value": "https://example.com/a\n\nBody"}
    p = out["processed"]
    assert p["WFSendEmailActionToRecipients"] == {"contacts": ["me+x3@example.com"]}
    assert p["WFSendEmailActionSubject"] == "[x3] A title" and p["WFSendEmailActionShowComposeSheet"] is False


# =================================================================== signing


@pytest.mark.skipif(os.environ.get("SENDTOX3_SHORTCUTS_SIGN") != "1" or shutil.which("shortcuts") is None,
                    reason="set SENDTOX3_SHORTCUTS_SIGN=1 on a Mac signed in to iCloud")
def test_sign_people_who_know_me(tmp_path):
    unsigned = bs.write_shortcut(tmp_path)[0]
    signed = bs.sign_shortcut(unsigned, tmp_path / "signed" / unsigned.name, mode="people-who-know-me")
    data = signed.read_bytes()
    assert data[:4] == b"AEA1" and len(data) > len(unsigned.read_bytes())
