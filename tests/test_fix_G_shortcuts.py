"""Regression tests for the iOS Shortcut fixes: mailto links, Smart Punctuation in Wi-Fi
names, the ``--sign anyone`` guard for the email address, and the docs that go with them."""

from __future__ import annotations

import plistlib

import pytest
from test_shortcuts import CONFIGURED, DOCS, REPO, SHORTCUTS, bs, run, sim

# ----------------------------------------------- 12: Get URLs from Input returns mailto:


def test_simulator_detects_links_like_ios():
    """Get URLs from Input (NSDataDetector) also returns email addresses and bare domains."""
    assert sim.find_links("Email jane@acme.org about the Q3 numbers") == ["mailto:jane@acme.org"]
    assert sim.find_links("I like example.com. See https://e.org/a, ok") == ["http://example.com", "https://e.org/a"]


@pytest.mark.parametrize("text", [
    "Email jane@acme.org about the Q3 numbers",
    "Call bob.smith@example.co.uk, then read https://example.com/q3",
    "Pricing is on example.com somewhere",
])
def test_a_note_with_an_email_address_is_sent_as_text(text):
    r, log = run(inp=[sim.Text(text)])
    [req] = log
    assert req.json_body == {"url": "", "text": text, "title": "", "source": "ios-shortcut"}
    assert r.notifications and not r.alerts


def test_a_shared_mailto_link_is_text_not_a_page():
    _, log = run(inp=[sim.URL("mailto:jane@acme.org")])
    assert log[0].json_body["url"] == "" and log[0].json_body["text"] == "mailto:jane@acme.org"


def test_email_route_does_not_put_a_mailto_link_first():
    r, _ = run(wifi="Cafe", inp=[sim.Text("Email jane@acme.org about the Q3 numbers")])
    [mail] = r.emails
    assert mail.body == "Email jane@acme.org about the Q3 numbers"  # no link: the text alone


def test_a_web_link_alone_is_still_sent_as_the_url():
    _, log = run(inp=[sim.URL("HTTPS://Example.com/b")])
    assert log[0].json_body["url"] == "HTTPS://Example.com/b" and log[0].json_body["text"] == ""


# --------------------------------------------- 31: Smart Punctuation in Wi-Fi network names


@pytest.mark.parametrize("home_wifi, wifi", [
    ("Miller’s Wi-Fi", "Miller's Wi-Fi"),       # typed on the iPhone, named on the router
    ("Miller's Wi-Fi", "Miller’s Wi-Fi"),       # the other way round
    ("Guest, “The Den”", '"The Den"'),
    ("Müller‚Haus‘", "Müller'Haus'"),  # German-style quotes
    ("Lab—Net", "Lab--Net"),                     # Smart Dashes: "--" becomes an em dash
    ("Miller’s Wi-Fi", "Miller’s Wi-Fi"),
])
def test_curly_quotes_in_home_wifi_still_match(home_wifi, wifi):
    cfg = bs.ShortcutConfig(ingest_url=CONFIGURED.ingest_url, token="t", home_wifi=home_wifi, email="me@example.com")
    r, log = run(cfg, wifi=wifi, inp=[sim.Text("x")])
    assert len(log) == 1 and not r.emails


@pytest.mark.parametrize("home_wifi, wifi", [("Miller’s Wi-Fi", "Millers Wi-Fi"), ("Lab—Net", "Lab-Net")])
def test_straightening_does_not_match_other_names(home_wifi, wifi):
    cfg = bs.ShortcutConfig(ingest_url=CONFIGURED.ingest_url, token="t", home_wifi=home_wifi, email="me@example.com")
    r, log = run(cfg, wifi=wifi, inp=[sim.Text("x")])
    assert not log and len(r.emails) == 1


def test_ios_doc_explains_smart_punctuation():
    doc = (DOCS / "phone-ios.md").read_text(encoding="utf-8")
    assert "Smart Punctuation" in doc
    for pattern, replacement in bs.SMART_PUNCTUATION:
        assert f"`{pattern}`" in doc and f"`{replacement}`" in doc


# ------------------------------------------- 32: --sign anyone and the secret +tag address


def no_signing(monkeypatch) -> list:
    """Replace signing with a recorder, so a broken guard fails the test instead of
    uploading the file to Apple's signing service."""
    calls: list = []

    def fake_sign(unsigned, signed, *, mode):
        calls.append(mode)
        raise AssertionError(f"sign_shortcut called with mode {mode!r}")

    monkeypatch.setattr(bs, "sign_shortcut", fake_sign)
    monkeypatch.setattr(bs, "shortcuts_cli", lambda: None)  # and no way to reach the real command
    return calls


def test_cli_refuses_email_with_anyone_signing(tmp_path, monkeypatch, capsys):
    calls = no_signing(monkeypatch)
    assert bs.main(["--out-dir", str(tmp_path), "--email", "me+x3secret@gmail.com", "--sign", "anyone"]) == 2
    assert "--allow-baked-values" in capsys.readouterr().err
    assert not (tmp_path / "Send to X3.shortcut").exists() and calls == []


def test_cli_allow_baked_values_permits_email_but_never_the_token(tmp_path, monkeypatch, capsys):
    signed_args = []

    def fake_sign(unsigned, signed, *, mode):
        signed_args.append(mode)
        signed.parent.mkdir(parents=True, exist_ok=True)
        signed.write_bytes(b"AEA1")
        return signed

    monkeypatch.setattr(bs, "sign_shortcut", fake_sign)
    assert bs.main(["--out-dir", str(tmp_path), "--email", "me+x3@example.com", "--home-wifi", "Home",
                    "--sign", "anyone", "--allow-baked-values"]) == 0
    err = capsys.readouterr().err
    assert "warning" in err and "--home-wifi" in err and "--email" in err
    assert signed_args == ["anyone"]
    wf = plistlib.loads((tmp_path / "Send to X3.shortcut").read_bytes())
    assert "me+x3@example.com" in [q["DefaultValue"] for q in wf["WFWorkflowImportQuestions"]]
    assert bs.main(["--out-dir", str(tmp_path / "t"), "--token", "secret", "--sign", "anyone",
                    "--allow-baked-values"]) == 2
    assert not (tmp_path / "t").exists() and signed_args == ["anyone"]  # the refused run signed nothing


def test_cli_people_who_know_me_may_bake_the_email(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "sign_shortcut", lambda unsigned, signed, *, mode: signed)
    assert bs.main(["--out-dir", str(tmp_path), "--email", "me+x3@example.com", "--sign", "people-who-know-me"]) == 0


def test_docs_say_the_anyone_file_is_secret_free_only_without_token_and_email():
    doc = " ".join((DOCS / "phone-ios.md").read_text(encoding="utf-8").split())
    assert "You can give this file to others" not in doc  # it said so whatever was baked in
    assert "A file built without `--token` and `--email` can be given to others" in doc
    assert "`--allow-baked-values`" in doc
    readme = (SHORTCUTS / "README.md").read_text(encoding="utf-8")
    assert "--email" in readme and "--allow-baked-values" in readme


# ---------------------------------------------- 34: no "Download iOS Shortcut" promise


def test_docs_do_not_promise_a_setup_page_shortcut_download():
    """The Setup page cannot offer the shortcut (unsigned files do not open on an iPhone);
    it links to docs/phone-ios.md and offers the prefilled Android file instead."""
    ios = (DOCS / "phone-ios.md").read_text(encoding="utf-8")
    readme = (SHORTCUTS / "README.md").read_text(encoding="utf-8")
    assert "Download iOS Shortcut" not in ios and "Download iOS Shortcut" not in readme
    assert "web/static" not in readme
    android = (DOCS / "phone-android.md").read_text(encoding="utf-8")
    assert "Download HTTP Shortcuts file" in android
    assert (REPO / "send_to_x3" / "sendtox3" / "phone.py").is_file()
