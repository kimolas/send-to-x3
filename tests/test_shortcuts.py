"""The phone shortcuts: the iOS Shortcut generator (structure + logic) and the Android
HTTP Shortcuts import file (schema + request body).

The generated plist is interpreted by ``shortcuts/tools/simulate.py`` so the logic tests
run the real action list. ``tests/test_shortcuts_macos.py`` checks the same file with
Apple's WorkflowKit, ``tests/test_shortcuts_ingest.py`` against the real ingest API.
"""

from __future__ import annotations

import importlib.util
import json
import plistlib
import re
import subprocess
import sys
import uuid
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
SHORTCUTS = REPO / "shortcuts"


def _load(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look their module up while the class is created
    spec.loader.exec_module(mod)
    return mod


bs = _load("sendtox3_build_shortcut", SHORTCUTS / "build_shortcut.py")
ba = _load("sendtox3_build_android", SHORTCUTS / "build_android.py")
sim = _load("sendtox3_shortcut_simulate", SHORTCUTS / "tools" / "simulate.py")

CONFIGURED = bs.ShortcutConfig(ingest_url="http://192.168.1.50:8765/api/v1/ingest", token="tok-123",
                               home_wifi="Home, Guest Net", email="me+x3@example.com")


def ident(action: dict) -> str:
    return action["WFWorkflowActionIdentifier"].removeprefix("is.workflow.actions.")


def params(action: dict) -> dict:
    return action["WFWorkflowActionParameters"]


def find(workflow: dict, identifier: str) -> list[dict]:
    return [a for a in workflow["WFWorkflowActions"] if ident(a) == identifier]


def walk(obj):
    """Every dict nested anywhere in ``obj``."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


@pytest.fixture(scope="module")
def wf() -> dict:
    return bs.build_workflow()


# ======================================================================== iOS: files


def test_build_writes_binary_plist_and_xml_twin(tmp_path):
    binary, xml = bs.write_shortcut(tmp_path)
    assert binary.name == "Send to X3.shortcut" and xml.name == "Send to X3.xml"
    data = binary.read_bytes()
    assert data.startswith(b"bplist00")
    assert plistlib.loads(data) == plistlib.loads(xml.read_bytes()) == bs.build_workflow()
    # byte-identical rebuilds (stable UUIDs, sorted keys)
    assert bs.shortcut_bytes() == data


@pytest.mark.skipif(sys.platform != "darwin", reason="plutil is macOS-only")
def test_plutil_lint(tmp_path):
    binary, xml = bs.write_shortcut(tmp_path)
    out = subprocess.run(["plutil", "-lint", str(binary), str(xml)], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stdout + out.stderr


def test_committed_files_are_up_to_date():
    """shortcuts/dist and shortcuts/android are generated: rerun the generators."""
    assert (SHORTCUTS / "dist" / "Send to X3.shortcut").read_bytes() == bs.shortcut_bytes(), \
        "run: python3 shortcuts/build_shortcut.py"
    assert (SHORTCUTS / "dist" / "Send to X3.xml").read_bytes() == bs.shortcut_bytes(xml=True)
    assert ba.DEFAULT_OUT.read_bytes() == ba.http_shortcuts_bytes(), "run: python3 shortcuts/build_android.py"


def test_committed_files_carry_no_secrets():
    wf = plistlib.loads((SHORTCUTS / "dist" / "Send to X3.shortcut").read_bytes())
    texts = [params(a)["WFTextActionText"] for a in wf["WFWorkflowActions"][:10] if ident(a) == "gettext"]
    assert texts == ["", "", "", "", "auto"]
    assert all(q["DefaultValue"] in ("", "auto") for q in wf["WFWorkflowImportQuestions"])
    android = json.loads(ba.DEFAULT_OUT.read_text(encoding="utf-8"))
    assert {v["key"]: v["value"] for v in android["variables"]} == dict.fromkeys(
        [ba.KEY_URL, ba.KEY_TOKEN, ba.KEY_EMAIL, ba.KEY_TEXT, ba.KEY_TITLE], "")


# ==================================================================== iOS: top level


def test_share_sheet_settings(wf):
    assert wf["WFWorkflowInputContentItemClasses"] == [
        "WFStringContentItem", "WFRichTextContentItem", "WFURLContentItem",
        "WFSafariWebPageContentItem", "WFArticleContentItem"]
    assert wf["WFWorkflowTypes"] == ["ActionExtension"]
    assert wf["WFWorkflowHasShortcutInputVariables"] is True
    assert wf["WFWorkflowNoInputBehavior"] == {"Name": "WFWorkflowNoInputBehaviorGetClipboard", "Parameters": {}}
    assert wf["WFWorkflowMinimumClientVersion"] == 900 and wf["WFWorkflowMinimumClientVersionString"] == "900"
    assert set(wf) == {
        "WFQuickActionSurfaces", "WFWorkflowActions", "WFWorkflowClientVersion", "WFWorkflowHasOutputFallback",
        "WFWorkflowHasShortcutInputVariables", "WFWorkflowIcon", "WFWorkflowImportQuestions",
        "WFWorkflowInputContentItemClasses", "WFWorkflowMinimumClientVersion",
        "WFWorkflowMinimumClientVersionString", "WFWorkflowNoInputBehavior", "WFWorkflowOutputContentItemClasses",
        "WFWorkflowTypes"}


def test_actions_present(wf):
    ids = {ident(a) for a in wf["WFWorkflowActions"]}
    assert ids == {"comment", "gettext", "setvariable", "text.replace", "conditional", "properties.safariwebpage",
                   "detect.text", "detect.link", "getwifi", "downloadurl", "getvalueforkey",
                   "notification", "alert", "exit", "sendemail"}
    props = [params(a)["WFContentItemPropertyName"] for a in find(wf, "properties.safariwebpage")]
    assert props == ["Page URL", "Name", "Page Selection"]
    [wifi] = find(wf, "getwifi")
    assert params(wifi) | {"UUID": None} == {"WFNetworkDetailsNetwork": "Wi-Fi", "WFWiFiDetail": "Network Name",
                                             "UUID": None}


def test_settings_come_first_with_import_questions(wf):
    acts = wf["WFWorkflowActions"]
    assert ident(acts[0]) == "comment" and "Edit the five Text actions below" in params(acts[0])["WFCommentActionText"]
    names = [params(a)["WFVariableName"] for a in acts[:13] if ident(a) == "setvariable"]
    assert names == ["IngestURL", "IngestToken", "HomeWiFi", "IngestEmail", "Mode"]
    questions = wf["WFWorkflowImportQuestions"]
    assert [q["ActionIndex"] for q in questions] == [1, 3, 5, 7, 9]
    for q in questions:
        assert set(q) == {"ActionIndex", "Category", "DefaultValue", "ParameterKey", "Text"}
        action = acts[q["ActionIndex"]]
        assert ident(action) == "gettext" and q["Category"] == "Parameter" and q["ParameterKey"] == "WFTextActionText"
        assert q["DefaultValue"] == params(action)["WFTextActionText"]
    texts = " ".join(q["Text"] for q in questions)
    for word in ("ingest URL", "ingest token", "Wi-Fi", "Email address", "Mode"):
        assert word in texts


def test_config_values_are_baked_into_settings_and_questions():
    wf = bs.build_workflow(CONFIGURED)
    acts = wf["WFWorkflowActions"]
    values = [params(acts[q["ActionIndex"]])["WFTextActionText"] for q in wf["WFWorkflowImportQuestions"]]
    assert values == [CONFIGURED.ingest_url, "tok-123", "Home, Guest Net", "me+x3@example.com", "auto"]
    assert [q["DefaultValue"] for q in wf["WFWorkflowImportQuestions"]] == values


@pytest.mark.parametrize("kwargs", [{"mode": "sometimes"}, {"name": "a/b"}, {"name": " "}, {"token": "a\nb"}])
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        bs.ShortcutConfig(**kwargs)


def test_mode_is_normalised():
    assert bs.ShortcutConfig(mode=" HTTP ").mode == "http"


# ============================================================ iOS: structural checks


def test_control_flow_is_well_formed(wf):
    stack: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for a in find(wf, "conditional"):
        p = params(a)
        mode, group = p["WFControlFlowMode"], p["GroupingIdentifier"]
        if mode == 0:
            assert group not in seen
            seen.add(group)
            stack.append((group, False))
            assert p["WFInput"]["Type"] == "Variable"
            assert p["WFCondition"] in (bs.COND_IS, bs.COND_CONTAINS, bs.COND_HAS_ANY_VALUE)
        elif mode == 1:
            assert stack and stack[-1] == (group, False)
            stack[-1] = (group, True)
        else:
            assert mode == 2 and stack and stack[-1][0] == group
            stack.pop()
    assert not stack


def test_every_reference_resolves(wf):
    """ActionOutput refs point to an earlier action's UUID; variables are set first."""
    uuids: dict[str, str] = {}
    set_vars: set[str] = set()
    for i, a in enumerate(wf["WFWorkflowActions"]):
        p = params(a)
        for d in walk({k: v for k, v in p.items() if k != "UUID"}):
            if d.get("Type") == "ActionOutput":
                assert d["OutputUUID"] in uuids, f"action {i} uses a later or missing output"
            if d.get("Type") == "Variable" and "VariableName" in d:
                assert d["VariableName"] in set_vars, f"action {i} reads {d['VariableName']} before it is set"
        if "UUID" in p:
            assert p["UUID"] not in uuids
            uuids[p["UUID"]] = ident(a)
        if ident(a) == "setvariable":
            set_vars.add(p["WFVariableName"])


def test_text_tokens_are_consistent(wf):
    for d in walk(wf["WFWorkflowActions"]):
        if d.get("WFSerializationType") != "WFTextTokenString":
            continue
        s = d["Value"]["string"]
        units = s.encode("utf-16-le")
        positions = []
        for key in d["Value"].get("attachmentsByRange", {}):
            m = re.fullmatch(r"\{(\d+), 1\}", key)
            assert m, key
            positions.append(int(m.group(1)))
            assert units[int(m.group(1)) * 2:int(m.group(1)) * 2 + 2].decode("utf-16-le") == "￼"
        assert s.count("￼") == len(positions)


def test_text_token_positions_count_utf16_units():
    tok = bs.text_token("😀 é ", bs.variable("X"), "!")
    assert list(tok["Value"]["attachmentsByRange"]) == ["{5, 1}"]  # emoji = 2 units


def test_http_request(wf):
    [post] = find(wf, "downloadurl")
    p = params(post)
    assert p["WFHTTPMethod"] == "POST" and p["WFHTTPBodyType"] == "JSON" and p["ShowHeaders"] is True
    assert p["WFURL"]["Value"]["attachmentsByRange"] == {"{0, 1}": {"Type": "Variable", "VariableName": "IngestURL"}}
    [header] = p["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
    assert header["WFKey"]["Value"]["string"] == "Authorization"
    assert header["WFValue"]["Value"]["string"] == "Bearer ￼"
    assert header["WFValue"]["Value"]["attachmentsByRange"] == {"{7, 1}": {"Type": "Variable",
                                                                           "VariableName": "IngestToken"}}
    body = {i["WFKey"]["Value"]["string"]: i["WFValue"]["Value"] for i in
            p["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]}
    assert list(body) == ["url", "text", "title", "source"]
    assert body["source"] == {"string": "ios-shortcut"}
    for key, var in (("url", "SharedURL"), ("text", "SharedText"), ("title", "SharedTitle")):
        assert body[key]["attachmentsByRange"] == {"{0, 1}": {"Type": "Variable", "VariableName": var}}
    keys = [params(a)["WFDictionaryKey"] for a in find(wf, "getvalueforkey")]
    assert keys == ["message", "error"]


def test_email(wf):
    [mail] = find(wf, "sendemail")
    p = params(mail)
    assert p["WFSendEmailActionShowComposeSheet"] is False
    assert p["WFSendEmailActionToRecipients"] == {"Value": {"Type": "Variable", "VariableName": "IngestEmail"},
                                                  "WFSerializationType": "WFTextTokenAttachment"}
    assert p["WFSendEmailActionSubject"]["Value"]["string"] == "[x3] ￼"
    assert p["WFSendEmailActionInputAttachments"]["Value"] == {
        "string": "￼", "attachmentsByRange": {"{0, 1}": {"Type": "Variable", "VariableName": "EmailBody"}}}
    uuid_of = bs.ShortcutBuilder(seed="send-to-x3").uuid
    body = {params(a)["UUID"]: params(a)["WFTextActionText"] for a in find(wf, "gettext")}
    body = {k: v["Value"] for k, v in body.items() if k in {uuid_of("email-body-text"), uuid_of("email-body-link")}}
    assert body[uuid_of("email-body-text")] == {
        "string": "￼", "attachmentsByRange": {"{0, 1}": {"Type": "Variable", "VariableName": "SharedText"}}}
    assert body[uuid_of("email-body-link")] == {"string": "￼\n\n￼", "attachmentsByRange": {
        "{0, 1}": {"Type": "Variable", "VariableName": "SharedLink"},
        "{3, 1}": {"Type": "Variable", "VariableName": "SharedText"}}}


def test_input_check_uses_safari_page_coercion(wf):
    first_if = find(wf, "conditional")[0]
    assert params(first_if)["WFInput"]["Variable"]["Value"] == {
        "Type": "ExtensionInput",
        "Aggrandizements": [{"Type": "WFCoercionVariableAggrandizement",
                             "CoercionItemClass": "WFSafariWebPageContentItem"}]}


# ================================================================ iOS: simulated runs


def ok_server(log: list):
    def handler(req):
        log.append(req)
        title = req.json_body["title"] or "Derived title"
        return 200, json.dumps({"ok": True, "id": "abcdefghij", "title": title, "status": "ready",
                                "duplicate": False, "error": "", "message": f"Sent to X3: {title}"}).encode()
    return handler


def run(cfg=CONFIGURED, *, wifi="Home", inp=None, clipboard=None, http=None, empty_items=False):
    log: list = []
    result = sim.run(bs.build_workflow(cfg), wifi=wifi, http=http or ok_server(log), shortcut_input=inp,
                     clipboard=clipboard, empty_items=empty_items)
    return result, log


@pytest.fixture(params=[False, True], ids=["nothing", "empty-text"])
def empty_items(request) -> bool:
    """Actions with nothing to report may give no item or one empty text; both must work."""
    return request.param


def test_safari_page_at_home_posts_url_title_and_selection():
    r, log = run(inp=[sim.SafariPage("https://example.com/a?x=1", "A page", "the selected words")])
    [req] = log
    assert req.method == "POST" and req.url == CONFIGURED.ingest_url
    assert req.headers == {"Authorization": "Bearer tok-123"}
    assert req.json_body == {"url": "https://example.com/a?x=1", "text": "the selected words", "title": "A page",
                             "source": "ios-shortcut"}
    assert r.notifications == [("Send to X3", "Sent to X3: A page")] and not r.alerts and not r.emails


def test_safari_page_without_selection_sends_the_link(empty_items):
    _, log = run(inp=[sim.SafariPage("https://example.com/a", "A page")], empty_items=empty_items)
    assert log[0].json_body == {"url": "https://example.com/a", "text": "", "title": "A page",
                                "source": "ios-shortcut"}


def test_shared_link_is_sent_as_url_only():
    _, log = run(inp=[sim.URL("https://example.com/b")])
    assert log[0].json_body == {"url": "https://example.com/b", "text": "", "title": "", "source": "ios-shortcut"}


def test_shared_text_is_sent_alone():
    """The app finds the links in a text itself (a URL next to the text would make the
    text count as a selection from that page)."""
    text = 'He said "hi" — see https://example.com/c, and https://example.com/d.\nSecond line'
    _, log = run(inp=[sim.Text(text)])
    assert log[0].json_body == {"url": "", "text": text, "title": "", "source": "ios-shortcut"}


def test_text_that_is_only_a_link_is_sent_as_the_url():
    _, log = run(inp=[sim.Text("https://example.com/b")])
    assert log[0].json_body == {"url": "https://example.com/b", "text": "", "title": "", "source": "ios-shortcut"}


def test_rich_text_hidden_link_targets_are_not_used():
    _, log = run(inp=[sim.RichText("A paragraph with a link in it.", links=("https://hidden.example/x",))])
    assert log[0].json_body["url"] == "" and log[0].json_body["text"] == "A paragraph with a link in it."


def test_app_and_text_pairs_are_joined():
    _, log = run(inp=[sim.Text("Interesting thread"), sim.URL("https://example.com/t")])
    assert log[0].json_body["text"] == "Interesting thread\nhttps://example.com/t"
    assert log[0].json_body["url"] == ""  # "link + comment": the app finds the link


def test_no_input_uses_clipboard():
    _, log = run(inp=None, clipboard=[sim.Text("copied note")])
    assert log[0].json_body["text"] == "copied note"


def test_nothing_to_send_stops_with_alert():
    r, log = run(inp=None, clipboard=[])
    assert not log and not r.emails and r.stopped
    assert r.alerts and r.alerts[0][0] == "Nothing to send"


def test_away_from_home_sends_email():
    r, log = run(wifi="Cafe Free WiFi", inp=[sim.SafariPage("https://example.com/a", "A page", "words")])
    assert not log
    assert len(r.emails) == 1
    mail = r.emails[0]
    assert mail.to == ["me+x3@example.com"] and mail.show_compose_sheet is False
    assert mail.subject == "[x3] A page" and mail.body == "https://example.com/a\n\nwords"
    assert r.notifications and r.notifications[0][1].startswith("Emailed")


def test_cellular_sends_email(empty_items):
    r, log = run(wifi="", inp=[sim.URL("https://example.com/b")], empty_items=empty_items)
    assert not log and r.emails[0].body == "https://example.com/b\n\n" and r.emails[0].subject == "[x3] "


@pytest.mark.parametrize("home_wifi, wifi, expect_http", [
    ("Home, Guest Net", "Home", True),
    ("Home, Guest Net", "Guest Net", True),
    (" Home ;Guest Net\nOffice ", "Office", True),
    ("Home", "home", False),          # Wi-Fi names are case-sensitive, as on the phone
    ("Home", "Hom", False),
    ("HomeNet", "Home", False),
    ("Home,", "", False),             # a trailing comma never matches "no Wi-Fi"
    (",,Home,,", "", False),
    ("", "", False),
    ("", "Home", False),
])
def test_home_wifi_matching(home_wifi, wifi, expect_http, empty_items):
    cfg = bs.ShortcutConfig(ingest_url=CONFIGURED.ingest_url, token="t", home_wifi=home_wifi, email="me@example.com")
    r, log = run(cfg, wifi=wifi, inp=[sim.Text("x")], empty_items=empty_items)
    assert (len(log) == 1) is expect_http
    assert (len(r.emails) == 1) is (not expect_http)


@pytest.mark.parametrize("mode, wifi, expect_http", [
    ("http", "Cafe", True), ("HTTP ", "Cafe", True), ("Wi-Fi", "", True), ("direct", "", True),
    ("email", "Home", False), (" E-Mail", "Home", False),
    ("auto", "Home", True), ("Auto", "Cafe", False), ("", "Home", True), ("whatever", "Cafe", False),
])
def test_modes(mode, wifi, expect_http):
    base = bs.build_workflow(CONFIGURED)
    # Simulate what a user typed into the Mode question (the generator only accepts canonical values).
    acts = base["WFWorkflowActions"]
    q = base["WFWorkflowImportQuestions"][4]
    params(acts[q["ActionIndex"]])["WFTextActionText"] = mode
    log: list = []
    r = sim.run(base, wifi=wifi, http=ok_server(log), shortcut_input=[sim.Text("x")])
    assert (len(log) == 1) is expect_http
    assert (len(r.emails) == 1) is (not expect_http)


@pytest.mark.parametrize("missing", ["ingest_url", "token"])
def test_http_route_requires_url_and_token(missing):
    cfg = bs.ShortcutConfig(**{**CONFIGURED.__dict__, missing: ""})
    r, log = run(cfg, inp=[sim.Text("x")])
    assert not log and r.stopped and "not set up" in r.alerts[0][0]


def test_email_route_requires_address():
    cfg = bs.ShortcutConfig(**{**CONFIGURED.__dict__, "email": ""})
    r, log = run(cfg, wifi="Cafe", inp=[sim.Text("x")])
    assert not log and not r.emails and r.stopped and "no email address" in r.alerts[0][0]


def test_server_error_is_shown():
    def unauthorized(req):
        return 401, b'{"ok":false,"error":"invalid or missing ingest token"}'
    r, _ = run(inp=[sim.Text("x")], http=unauthorized)
    assert not r.notifications
    [(title, message)] = r.alerts
    assert title == "Send to X3: not saved" and message.startswith("invalid or missing ingest token")


def test_unreachable_server_stops_the_run():
    def down(req):
        raise ConnectionError("Could not connect to the server.")
    with pytest.raises(ConnectionError):
        run(inp=[sim.Text("x")], http=down)


def test_simulator_rejects_types_the_shortcut_does_not_accept():
    with pytest.raises(ValueError):
        sim.run(bs.build_workflow(), shortcut_input=[sim.Dict({"a": 1})])


# ======================================================================= iOS: CLI


def test_cli_writes_files(tmp_path, capsys):
    assert bs.main(["--out-dir", str(tmp_path), "--ingest-url", "http://10.0.0.2:8765/api/v1/ingest"]) == 0
    wf = plistlib.loads((tmp_path / "Send to X3.shortcut").read_bytes())
    assert params(wf["WFWorkflowActions"][1])["WFTextActionText"] == "http://10.0.0.2:8765/api/v1/ingest"
    assert "wrote" in capsys.readouterr().out


def test_cli_refuses_token_with_anyone_signing(tmp_path, monkeypatch):
    calls = []  # a broken guard must fail here, not upload the file to Apple's signing service
    monkeypatch.setattr(bs, "sign_shortcut", lambda unsigned, signed, *, mode: calls.append(mode) or signed)
    monkeypatch.setattr(bs, "shortcuts_cli", lambda: None)
    assert bs.main(["--out-dir", str(tmp_path), "--token", "secret", "--sign", "anyone"]) == 2
    assert not (tmp_path / "Send to X3.shortcut").exists() and calls == []


def test_cli_sign_without_shortcuts_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bs, "shortcuts_cli", lambda: None)
    assert bs.main(["--out-dir", str(tmp_path), "--sign", "people-who-know-me"]) == 1
    assert "macOS" in capsys.readouterr().err
    assert (tmp_path / "Send to X3.shortcut").exists()  # the unsigned file is still written


def fake_shortcuts(tmp_path: Path, script: str) -> str:
    path = tmp_path / "shortcuts"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return str(path)


def test_sign_success_and_arguments(tmp_path, monkeypatch):
    log = tmp_path / "args"
    cli = fake_shortcuts(tmp_path, f'echo "$@" > "{log}"\n'
                                   'while [ $# -gt 0 ]; do [ "$1" = --output ] && printf AEA1rest > "$2"; shift; done\n')
    monkeypatch.setattr(bs, "shortcuts_cli", lambda: cli)
    unsigned = bs.write_shortcut(tmp_path / "out")[0]
    signed = bs.sign_shortcut(unsigned, tmp_path / "out" / "signed" / unsigned.name)
    assert signed.read_bytes() == b"AEA1rest"
    args = log.read_text().split()
    assert args[:3] == ["sign", "--mode", "people-who-know-me"] and "--input" in args
    assert not list((tmp_path / "out" / "signed").glob("*.tmp"))


def test_sign_anyone_retries_then_fails(tmp_path, monkeypatch):
    count = tmp_path / "count"
    cli = fake_shortcuts(tmp_path, f'echo x >> "{count}"\necho "Error: NSURLErrorDomain 500" >&2\nexit 1\n')
    monkeypatch.setattr(bs, "shortcuts_cli", lambda: cli)
    unsigned = bs.write_shortcut(tmp_path / "out")[0]
    with pytest.raises(bs.SigningError, match="500"):
        bs.sign_shortcut(unsigned, tmp_path / "signed.shortcut", mode="anyone", attempts=3, retry_delay=0)
    assert count.read_text().count("x") == 3
    assert not (tmp_path / "signed.shortcut").exists()


def test_sign_rejects_output_that_is_not_signed(tmp_path, monkeypatch):
    cli = fake_shortcuts(tmp_path, 'while [ $# -gt 0 ]; do [ "$1" = --output ] && printf bplist > "$2"; shift; done\n')
    monkeypatch.setattr(bs, "shortcuts_cli", lambda: cli)
    unsigned = bs.write_shortcut(tmp_path / "out")[0]
    with pytest.raises(bs.SigningError):
        bs.sign_shortcut(unsigned, tmp_path / "signed.shortcut", attempts=1)


# ============================================================ Android: schema
#
# Field names and rules from HTTP Shortcuts 4.9.0 (import_export/models/*.kt,
# ImportMigrator.kt, ImportExportDefaultsProvider.kt, variables/Variables.kt).

BASE_FIELDS = {"version", "compatibilityVersion", "originDeviceId", "createdAt", "categories", "variables",
               "certificatePins", "workingDirectories", "title", "globalCode", "userAgent", "defaultBrowser"}
CATEGORY_FIELDS = {"id", "name", "iconName", "layoutType", "alignment", "background", "hidden", "hiddenLabels",
                   "scale", "shortcutClickBehavior", "sections", "shortcuts"}
SHORTCUT_FIELDS = {
    "id", "executionType", "name", "description", "iconName", "iconBackground", "hidden", "method", "url",
    "username", "password", "authToken", "section", "bodyContent", "timeout", "waitForInternet",
    "acceptAllCertificates", "certificateFingerprint", "authentication", "launcherShortcut",
    "secondaryLauncherShortcut", "quickSettingsTileShortcut", "delay", "repetitionInterval", "networkPreference",
    "requestBodyType", "contentType", "responseHandling", "fileUploadOptions", "confirmation", "followRedirects",
    "acceptCookies", "keepConnectionOpen", "protocolVersion", "proxy", "proxyHost", "proxyPort", "proxyUsername",
    "proxyPassword", "wifiSsid", "clientCert", "codeOnPrepare", "codeOnSuccess", "codeOnFailure",
    "browserPackageName", "excludeFromHistory", "excludeFromFileSharing", "runInForegroundService", "runOnAppStart",
    "wolMacAddress", "wolPort", "wolBroadcastAddress", "headers", "parameters"}
VARIABLE_FIELDS = {"id", "key", "type", "value", "data", "rememberValue", "urlEncode", "jsonEncode", "title",
                   "message", "isShareText", "isShareTitle", "isMultiline", "isExcludeValueFromExport", "isSecret"}
RESPONSE_FIELDS = {"actions", "uiType", "successOutput", "failureOutput", "contentType", "charset",
                   "successMessage", "includeMetaInfo", "jsonArrayAsTable", "monospace", "fontSize",
                   "javaScriptEnabled", "storeDirectoryId", "storeFileName", "replaceFileIfExists"}
VARIABLE_TYPES = {"constant", "text", "number", "password", "select", "color", "date", "time", "slider", "toggle",
                  "increment", "uuid", "clipboard", "timestamp"}
#: Variables.RAW_PLACEHOLDER_REGEX: a UUID must look like a random (version 4) one.
PLACEHOLDER = re.compile(r"\{\{([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-4[0-9A-Fa-f]{3}-[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}"
                         r"|[0-9]+|[A-Za-z0-9_]{1,30})\}\}", re.IGNORECASE)
JS_GET_VARIABLE = re.compile(r"""getVariable\(["']([A-Za-z0-9_]{1,30})["']\)""")


@pytest.fixture(scope="module")
def android() -> dict:
    return json.loads(ba.DEFAULT_OUT.read_text(encoding="utf-8"))


def _is_uuid(s) -> bool:
    try:
        return str(uuid.UUID(s)) == s
    except (TypeError, ValueError):
        return False


def test_android_file_matches_the_import_schema(android):
    assert set(android) <= BASE_FIELDS
    assert android["version"] == 92 and android["compatibilityVersion"] == 90  # app 4.9.0 reads up to 92
    assert len(android["categories"]) == 1
    category = android["categories"][0]
    assert set(category) <= CATEGORY_FIELDS and _is_uuid(category["id"]) and category["name"].strip()
    assert category["layoutType"] in {"linear_list", "dense_grid", "medium_grid", "wide_grid"}
    [shortcut] = category["shortcuts"]
    assert set(shortcut) <= SHORTCUT_FIELDS and _is_uuid(shortcut["id"]) and shortcut["name"].strip()
    assert set(shortcut["responseHandling"]) <= RESPONSE_FIELDS
    assert shortcut["executionType"] == "app" and shortcut["method"] == "POST"
    assert shortcut["requestBodyType"] == "custom_text" and shortcut["contentType"] == "application/json"
    assert 250 <= shortcut["timeout"] <= 24 * 3600 * 1000
    assert shortcut["waitForInternet"] is True and shortcut["launcherShortcut"] is True
    assert shortcut["responseHandling"]["uiType"] in {"toast", "notification", "dialog", "window"}
    assert shortcut["responseHandling"]["successOutput"] in {"response", "message", "none"}
    assert shortcut["responseHandling"]["failureOutput"] in {"detailed", "simple", "none"}
    for header in shortcut["headers"]:
        assert header["key"] and all("!" <= c <= "~" for c in header["key"])
        assert all((c > "\x1f" or c == "\t") and c < "\x7f" for c in header["value"])
    keys = [v["key"] for v in android["variables"]]
    ids = [v["id"] for v in android["variables"]]
    assert len(set(keys)) == len(keys) and len(set(ids)) == len(ids)
    for v in android["variables"]:
        assert set(v) <= VARIABLE_FIELDS and _is_uuid(v["id"])
        assert re.fullmatch(r"[A-Za-z0-9_]{1,30}", v["key"]) and v["type"] in VARIABLE_TYPES


def test_android_placeholders_and_share_variables(android):
    shortcut = android["categories"][0]["shortcuts"][0]
    by_id = {v["id"]: v for v in android["variables"]}
    by_key = {v["key"]: v for v in android["variables"]}
    fields = [shortcut["url"], shortcut["bodyContent"]] + [h["value"] for h in shortcut["headers"]]
    used = {m for f in fields for m in PLACEHOLDER.findall(f)}
    assert used <= set(by_id), "every {{placeholder}} must be a variable id"
    assert all(f.count("{{") == len(PLACEHOLDER.findall(f)) for f in fields), "a placeholder the app would not see"
    assert shortcut["url"] == "{{" + by_key["sendtox3_url"]["id"] + "}}"
    assert shortcut["headers"] == [{"key": "Authorization",
                                    "value": "Bearer {{" + by_key["sendtox3_token"]["id"] + "}}"}]
    # it is a text share target: a share variable is used by the request
    share_text = [v for v in android["variables"] if v.get("isShareText")]
    share_title = [v for v in android["variables"] if v.get("isShareTitle")]
    assert [v["key"] for v in share_text] == ["sendtox3_text"] and [v["key"] for v in share_title] == ["sendtox3_title"]
    assert {share_text[0]["id"], share_title[0]["id"]} <= used
    for v in share_text + share_title:
        assert v["jsonEncode"] is True and not v.get("urlEncode")
    for key in ("sendtox3_url", "sendtox3_token", "sendtox3_email"):
        assert by_key[key]["type"] == "constant" and by_key[key]["isExcludeValueFromExport"] is True
    scripts = shortcut["codeOnPrepare"] + shortcut["codeOnSuccess"] + shortcut["codeOnFailure"]
    assert set(JS_GET_VARIABLE.findall(scripts)) <= set(by_key)
    assert not PLACEHOLDER.search(scripts)


def android_json_quote(value: str) -> str:
    """``org.json.JSONObject.quote`` as on Android (JSONStringer.string), minus the quotes."""
    out = []
    for c in value:
        if c in '"\\/':
            out.append("\\" + c)
        elif c in "\t\b\n\r\f":
            out.append({"\t": "\\t", "\b": "\\b", "\n": "\\n", "\r": "\\r", "\f": "\\f"}[c])
        elif ord(c) <= 0x1F:
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return "".join(out)


def render_android_request(data: dict, *, text: str, title: str, values: dict[str, str]) -> tuple[str, dict, bytes]:
    """What HTTP Shortcuts sends for a share: substitute ``{{id}}`` placeholders with the
    (JSON-encoded where configured) variable values."""
    shortcut = data["categories"][0]["shortcuts"][0]
    resolved = {}
    for v in data["variables"]:
        raw = {"sendtox3_text": text, "sendtox3_title": title}.get(v["key"], values.get(v["key"], v["value"]))
        resolved[v["id"]] = android_json_quote(raw) if v.get("jsonEncode") else raw

    def sub(s: str) -> str:
        return PLACEHOLDER.sub(lambda m: resolved[m.group(1)], s)

    headers = {h["key"]: sub(h["value"]) for h in shortcut["headers"]}
    headers["Content-Type"] = shortcut["contentType"]
    return sub(shortcut["url"]), headers, sub(shortcut["bodyContent"]).encode("utf-8")


TRICKY_TEXTS = [
    "https://example.com/article?id=1&utm_source=x",
    'Quote "this" and a backslash \\ and a slash / and </script>',
    "Line one\nLine two\r\nTab\there\x00nul\x1fus ls ps",
    "Ünïcödé, 中文, emoji 😀, rtl שלום",
    "",
]


@pytest.mark.parametrize("text", TRICKY_TEXTS)
def test_android_body_is_valid_json_for_any_shared_text(android, text):
    url, headers, body = render_android_request(android, text=text, title='Ti"tle\\', values={
        "sendtox3_url": "http://10.0.0.5:8765/api/v1/ingest", "sendtox3_token": "tok"})
    assert url == "http://10.0.0.5:8765/api/v1/ingest" and headers["Authorization"] == "Bearer tok"
    assert json.loads(body) == {"text": text, "title": 'Ti"tle\\', "source": "android-http-shortcuts"}


def test_android_generator_bakes_values_and_validates(tmp_path):
    out = tmp_path / "x.json"
    assert ba.main(["--out", str(out), "--ingest-url", "http://10.0.0.5:8765/api/v1/ingest", "--token", "t0k",
                    "--email", "me+x3@example.com"]) == 0
    data = json.loads(out.read_text())
    values = {v["key"]: v["value"] for v in data["variables"]}
    assert values["sendtox3_url"] == "http://10.0.0.5:8765/api/v1/ingest" and values["sendtox3_token"] == "t0k"
    assert values["sendtox3_email"] == "me+x3@example.com"
    with pytest.raises(ValueError):
        ba.AndroidConfig(token="a\nb")
    # ids are stable, so re-importing an updated file updates the same shortcut
    assert data["categories"][0]["shortcuts"][0]["id"] == ba.SHORTCUT_ID


# =================================================== Android: scripts (with node, if present)

NODE = __import__("shutil").which("node")

_HARNESS = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const calls = [];
class Abort extends Error {}
const env = {
  getVariable: (k) => { if (!(k in input.vars)) throw new Error('no variable ' + k); return input.vars[k]; },
  showToast: (m) => calls.push(['toast', m]),
  showDialog: (m, t) => calls.push(['dialog', m, t]),
  openUrl: (u) => calls.push(['openUrl', u]),
  htmlEncode: (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  abort: () => { calls.push(['abort']); throw new Abort(); },
  abortAndTreatAsFailure: (m) => { calls.push(['treatAsFailure', m]); throw new Abort(); },
  response: input.response, networkError: input.networkError,
};
try {
  new Function(...Object.keys(env), input.code)(...Object.values(env));
} catch (e) { if (!(e instanceof Abort)) { calls.push(['error', String(e)]); } }
process.stdout.write(JSON.stringify(calls));
"""


def run_script(code: str, *, vars: dict, response=None, network_error=None) -> list:
    proc = subprocess.run([NODE, "-e", _HARNESS], input=json.dumps(
        {"code": code, "vars": vars, "response": response, "networkError": network_error}),
        capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


ANDROID_VARS = {"sendtox3_url": "http://10.0.0.5:8765/api/v1/ingest", "sendtox3_token": "tok",
                "sendtox3_email": "me+x3@example.com",
                "sendtox3_text": android_json_quote('A "quoted" line\nhttps://example.com/a & more'),
                "sendtox3_title": android_json_quote("Title / with slash")}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestAndroidScripts:
    def code(self, android, name):
        return android["categories"][0]["shortcuts"][0][name]

    def test_prepare_stops_when_not_configured(self, android):
        assert run_script(self.code(android, "codeOnPrepare"), vars=ANDROID_VARS) == []
        calls = run_script(self.code(android, "codeOnPrepare"), vars={**ANDROID_VARS, "sendtox3_token": ""})
        assert calls[0][0] == "dialog" and "sendtox3_token" in calls[0][1] and calls[1] == ["abort"]

    def test_success_shows_the_server_message(self, android):
        body = json.dumps({"ok": True, "message": "Sent to X3: A title", "title": "A title"})
        calls = run_script(self.code(android, "codeOnSuccess"), vars=ANDROID_VARS,
                           response={"body": body, "statusCode": 200})
        assert calls == [["toast", "Sent to X3: A title"]]

    def test_success_with_a_foreign_answer_is_a_failure(self, android):
        calls = run_script(self.code(android, "codeOnSuccess"), vars=ANDROID_VARS,
                           response={"body": "<html>login</html>", "statusCode": 200})
        assert calls == [["treatAsFailure", "not a Send to X3 answer"]]

    def test_failure_with_server_error(self, android):
        calls = run_script(self.code(android, "codeOnFailure"), vars=ANDROID_VARS,
                           response={"body": '{"ok":false,"error":"invalid or missing <ingest> token"}',
                                     "statusCode": 401})
        assert calls == [["dialog", "invalid or missing &lt;ingest&gt; token", "Send to X3: not saved"]]
        calls = run_script(self.code(android, "codeOnFailure"), vars=ANDROID_VARS,
                           response={"body": "<html/>", "statusCode": 200})
        assert "HTTP 200" in calls[0][1] and "sendtox3_url" in calls[0][1]

    def test_unreachable_falls_back_to_email(self, android):
        from urllib.parse import parse_qs, urlsplit
        calls = run_script(self.code(android, "codeOnFailure"), vars=ANDROID_VARS,
                           network_error="Failed to connect to /10.0.0.5:8765")
        [(kind, url)] = calls
        assert kind == "openUrl"
        parts = urlsplit(url)
        assert parts.scheme == "mailto" and parts.path == "me+x3@example.com"
        q = parse_qs(parts.query)
        assert q["subject"] == ["[x3] Title / with slash"]
        assert q["body"] == ['A "quoted" line\nhttps://example.com/a & more']

    def test_unreachable_without_email(self, android):
        calls = run_script(self.code(android, "codeOnFailure"), vars={**ANDROID_VARS, "sendtox3_email": ""},
                           network_error="timeout")
        assert calls[0][0] == "dialog" and "home Wi-Fi" in calls[0][1] and "timeout" in calls[0][1]


# ================================================================== docs stay in sync

DOCS = REPO / "docs"


def test_android_doc_has_the_current_scripts():
    doc = (DOCS / "phone-android.md").read_text(encoding="utf-8")
    for code in (ba.CODE_ON_PREPARE, ba.CODE_ON_SUCCESS, ba.CODE_ON_FAILURE):
        assert code.rstrip("\n") in doc
    for key in (ba.KEY_URL, ba.KEY_TOKEN, ba.KEY_EMAIL, ba.KEY_TEXT, ba.KEY_TITLE):
        assert f"`{key}`" in doc


def test_ios_doc_recipe_matches_the_generator():
    doc = (DOCS / "phone-ios.md").read_text(encoding="utf-8")
    for name in (bs.V_INGEST_URL, bs.V_TOKEN, bs.V_HOME_WIFI, bs.V_EMAIL, bs.V_MODE, bs.V_URL, bs.V_TITLE,
                 bs.V_TEXT, bs.V_WIFI, bs.V_HOME_LIST, bs.V_ROUTE):
        assert f"`{name}`" in doc, name
    assert f"`{bs.HOME_WIFI_SEPARATOR_RE}`" in doc
    assert f"`{bs.FIRST_WEB_LINK_RE}`" in doc and f"`{bs.BARE_HOST_RE}`" in doc
    for name in (bs.V_LINK, bs.V_BODY):
        assert f"`{name}`" in doc, name
    assert "ios-shortcut" in doc and "[x3]" in doc
