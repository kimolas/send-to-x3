"""Round-2 checks of the iOS Shortcut against Apple's own frameworks (macOS only).

- Every Replace Text pattern in the shortcut gives the same result in the simulator
  (Python ``re``) as in NSRegularExpression, the ICU engine behind Replace Text, for
  inputs that exercise the new first-web-link and domain-name patterns (R2-G1, R2-G2).
- The email body texts resolve in WorkflowKit to ``<link>\\n\\n<text>`` or ``<text>``.
"""

from __future__ import annotations

import json
import subprocess

from test_shortcuts import bs, find, params, sim
from test_shortcuts_macos import check, index_of, pytestmark, shortcut, wfcheck  # noqa: F401 (skip mark, fixtures)

SAMPLES = [
    "", "example.com", "  Example.COM/a?b=1 \n", "www.example.org/2026/story", "example.com.", "see example.com",
    "Node.js", "3.14", "example.com:8080/x#y", "jane@acme.org", "two\nlines.com", "https://example.com/a",
    # Get URLs from Input results, one link per line
    "mailto:jane@acme.org", "mailto:a@b.co\nhttps://x.example/a", "https://x.example/a\nmailto:a@b.co",
    "HTTPS://Example.com/b", "tel:+15551234567\nmailto:x@y.zz", "ftp://x\nhttp://a.example/c\nhttps://d.example",
    "mailto:a@b.co\n", "https://a.example\n",
    # settings
    ",Home, Guest Net,", ",a;;\n b ,", " HTTP ", "Wi-Fi", " E-Mail", "Miller’s „Haus“ — Lab",
]


def regex(tool, pattern: str, template: str, case_sensitive: bool, inputs: list[str]) -> list[str]:
    proc = subprocess.run([str(tool), "regex", pattern, template, "1" if case_sensitive else "0"],
                          input=json.dumps(inputs), capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["outputs"]


def test_every_replace_text_pattern_matches_nsregularexpression(wfcheck, shortcut):
    _, wf = shortcut
    replaces = find(wf, "text.replace")
    patterns = {params(a)["WFReplaceTextFind"] for a in replaces}
    assert {bs.FIRST_WEB_LINK_RE, bs.BARE_HOST_RE} <= patterns  # the new ones are among them
    for action in replaces:
        p = params(action)
        find_, template, cs = p["WFReplaceTextFind"], p["WFReplaceTextReplace"], p["WFReplaceTextCaseSensitive"]
        expected = regex(wfcheck, find_, template, cs, SAMPLES)
        assert [sim.replace_text(find_, template, s, case_sensitive=cs) for s in SAMPLES] == expected, find_


def action_index(wf: dict, label: str) -> int:
    """Index of the action the generator added under ``label``."""
    uid = bs.ShortcutBuilder(seed="send-to-x3").uuid(label)
    return index_of(wf, lambda a: params(a).get("UUID") == uid)


def test_first_web_link_and_domain_name_in_nsregularexpression(wfcheck, shortcut):
    _, wf = shortcut
    acts = wf["WFWorkflowActions"]
    first = params(acts[action_index(wf, "input-web-url")])
    assert regex(wfcheck, first["WFReplaceTextFind"], first["WFReplaceTextReplace"], False,
                 ["mailto:a@b.co\nhttps://x.example/a\nhttps://y.example", "mailto:a@b.co", ""]) == [
        "https://x.example/a", "", ""]
    for label, scheme in (("bare-host-probe", "http://"), ("bare-host-url", "https://")):
        p = params(acts[action_index(wf, label)])
        assert (p["WFReplaceTextFind"], p["WFReplaceTextReplace"]) == (bs.BARE_HOST_RE, scheme + "$1")
        assert regex(wfcheck, p["WFReplaceTextFind"], p["WFReplaceTextReplace"], False,
                     [" Example.com/a\n", "see example.com"]) == [f"{scheme}Example.com/a", "see example.com"]


def test_email_bodies_resolve(wfcheck, shortcut):
    path, wf = shortcut
    out = check(wfcheck, "params", str(path), str(action_index(wf, "email-body-link")),
                "SharedLink=https://example.com/a", "SharedText=Interesting\nhttps://example.com/a")
    assert out["processed"]["WFTextActionText"] == "https://example.com/a\n\nInteresting\nhttps://example.com/a"
    out = check(wfcheck, "params", str(path), str(action_index(wf, "email-body-text")),
                "SharedText=Email jane@acme.org")
    assert out["processed"]["WFTextActionText"] == "Email jane@acme.org"


def test_no_link_probe_is_blank_for_no_link_and_an_empty_one(wfcheck, shortcut):
    path, wf = shortcut
    probe = action_index(wf, "no-email-link")
    for value, expected in (("SharedLink:=", "<>"), ("SharedLink=", "<>"), ("SharedLink=https://x", "<https://x>")):
        assert check(wfcheck, "params", str(path), str(probe), value)["processed"]["WFTextActionText"] == expected
    assert params(wf["WFWorkflowActions"][probe + 1])["WFConditionalActionString"] == "<>"
