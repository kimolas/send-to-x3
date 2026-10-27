"""Round 2, group H: the user docs (README, DOCS.md, the option form) say what the code
does. Each check is anchored to the code it describes where it can be, so a behaviour
change fails here until the docs follow it.

- R2-H1: delivery at sleep needs a CrossPoint build newer than 2.0.0rc (develop commit
  facaa87); on 2.0.0rc items arrive at the next Wi-Fi use.
- R2-H2: email ingest fails closed (allowed_senders and/or imap_plus_tag), ``'*'`` only
  with a tag or a dedicated folder, refused mail stays unread, link + <= 30 words.
- R2-H3: the device token can be read from the X3 during File Transfer; how to replace it.
- R2-H4: token character guidance matches ``plugin_bundle._check_token`` exactly, and
  the Supervisor check's comment does not claim the current version had a port option.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from sendtox3 import plugin_bundle
from sendtox3.config import Options
from sendtox3.extract.email import URL_ONLY_MAX_WORDS, sender_filter_problem
from sendtox3.plugin_bundle import PLUGIN_DIR, SLEEP_DELIVERY_CAVEAT, device_token_problem
from test_packaging import APP, CONFIG, REPO, TRANSLATIONS, load_yaml

README = APP / "README.md"
DOCS = APP / "DOCS.md"
SETUP_TEMPLATE = APP / "sendtox3" / "web" / "templates" / "setup.html"
VALIDATE_CONFIG = REPO / "e2e" / "ha" / "validate_config.py"


@pytest.fixture(scope="module")
def tr() -> dict[str, Any]:
    return load_yaml(TRANSLATIONS)["configuration"]


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _units(path: Path) -> list[str]:
    """Paragraphs of a Markdown file, with each table row and each bullet item on its
    own (so one caveat elsewhere in a table or list cannot excuse a row)."""
    units: list[str] = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
        lines = block.strip().splitlines()
        if lines and lines[0].startswith("|"):
            units += lines
        elif lines and lines[0].startswith("- "):
            units += [_flat(item) for item in re.split(r"\n(?=- )", block.strip())]
        else:
            units.append(_flat(block))
    return units


def _docs_row(name: str) -> str:
    return next(line for line in DOCS.read_text(encoding="utf-8").splitlines() if line.startswith(f"| `{name}` |"))


def _docs_section(title: str) -> str:
    text = DOCS.read_text(encoding="utf-8")
    start = text.index(f"### {title}\n")
    end = text.find("\n## ", start)
    nxt = text.find("\n### ", start + 4)
    end = min(e for e in (end, nxt, len(text)) if e != -1)
    return _flat(text[start:end])


# ============================================================== R2-H1 sleep delivery
def _fix_commit() -> str:
    m = re.search(r"commit\s+([0-9a-f]{7,40})", SLEEP_DELIVERY_CAVEAT)
    assert m and "2.0.0rc" in SLEEP_DELIVERY_CAVEAT, SLEEP_DELIVERY_CAVEAT
    return m.group(1)


def test_sleep_delivery_is_promised_only_with_the_firmware_caveat(tr: dict[str, Any]) -> None:
    """Anything that says items arrive when the X3 goes to sleep also says it needs a
    build newer than 2.0.0rc (the commit the plugin's own README names) and what happens
    on 2.0.0rc."""
    commit = _fix_commit()
    units = [*_units(README), *_units(DOCS), *(_flat(v["description"]) for v in tr.values())]
    claims = [u for u in units if re.search(r"goes to sleep|\bat (every|each) sleep\b", u, re.I)]
    for unit in claims:
        assert "2.0.0rc" in unit, unit
        assert commit in unit, unit
        assert "next time the X3 uses Wi-Fi" in unit, unit
    # Not vacuous: the places users read about it carry the caveat.
    assert _docs_row("auto_delivery") in claims
    assert _flat(tr["auto_delivery"]["description"]) in claims
    assert any("Home > Plugins > Send to X3" in u for u in claims)


@pytest.mark.parametrize("path", [README, DOCS, TRANSLATIONS, CONFIG], ids=lambda p: p.name)
def test_no_unconditional_every_sleep_promise(path: Path) -> None:
    text = _flat(path.read_text(encoding="utf-8"))
    bad = re.search(r"(every|each) time the X3 goes to sleep|whenever the X3 goes to sleep|at its next sleep", text,
                    re.I)
    assert not bad, f"{path.name}: {bad.group(0)!r}"


def test_docs_plugin_row_points_to_the_fetch_section() -> None:
    row = next(line for line in DOCS.read_text(encoding="utf-8").splitlines() if line.startswith("| CrossPoint 2.x |"))
    assert "Fetching new items" in row and "Get 3 new" in row, row
    assert "### Fetching new items" in DOCS.read_text(encoding="utf-8")


def test_readme_says_nothing_arrives_by_itself() -> None:
    bullet = next(u for u in _units(README) if u.startswith("- Works with **stock CrossPoint**"))
    assert "Nothing arrives by itself" in bullet and "Get 3 new" in bullet, bullet


# ============================================================== R2-H2 email rule
def _o(**kw: Any) -> Options:
    return Options(imap_enabled=True, **kw)


def test_email_rule_the_docs_describe_is_the_code_rule() -> None:
    """If one of these changes, the DOCS.md email section and the form texts must too."""
    assert sender_filter_problem(_o())  # neither filter: nothing is accepted
    assert not sender_filter_problem(_o(allowed_senders=["me@example.com"]))
    assert not sender_filter_problem(_o(imap_plus_tag="x3-7f2k"))
    assert sender_filter_problem(_o(allowed_senders=["*"]))  # '*' in INBOX without a tag
    assert sender_filter_problem(_o(allowed_senders=["*"], imap_folder="inbox"))
    assert not sender_filter_problem(_o(allowed_senders=["*"], imap_plus_tag="x3-7f2k"))
    assert not sender_filter_problem(_o(allowed_senders=["*"], imap_folder="X3"))
    assert URL_ONLY_MAX_WORDS == 30


def test_docs_email_section_describes_the_fail_closed_rule() -> None:
    sec = _docs_section("Email (IMAP)")
    assert "mail is processed only when `allowed_senders` and/or `imap_plus_tag` is set" in sec, sec
    assert "With neither, the app does not read the mailbox at all" in sec
    assert ("`allowed_senders: ['*']` (any sender) is accepted only together with `imap_plus_tag`, or with a "
            "dedicated `imap_folder` other than `INBOX`") in sec
    assert "is left unread" in sec
    assert f"at most {URL_ONLY_MAX_WORDS} words" in sec
    assert "each page is fetched, and your words are kept as a note" in sec
    row = _docs_row("allowed_senders")
    assert "`'*'`" in row and "`INBOX`" in row and "no mail is processed" in row, row
    assert "no mail is processed" in _docs_row("imap_enabled")
    assert "`allowed_senders` must be set" in _docs_row("imap_plus_tag")


@pytest.mark.parametrize("stale", [
    "Empty: any sender that passes the other checks",
    "accept any sender that passes the other checks",
    "Leave empty to accept any recipient",
])
def test_old_open_email_wording_is_gone(stale: str) -> None:
    for path in (DOCS, TRANSLATIONS, README):
        assert stale.lower() not in _flat(path.read_text(encoding="utf-8")).lower(), (path.name, stale)


def test_email_form_texts_describe_the_rule(tr: dict[str, Any]) -> None:
    enabled = tr["imap_enabled"]["description"]
    senders = tr["allowed_senders"]["description"]
    tag = tr["imap_plus_tag"]["description"]
    # The form shows option names, so the texts must use the names the form shows.
    for name in ("allowed_senders", "imap_plus_tag"):
        assert tr[name]["name"] in enabled, (tr[name]["name"], enabled)
    assert "processed only when" in enabled and "left unread" in enabled
    assert f"at most {URL_ONLY_MAX_WORDS} words" in enabled and "note" in enabled
    assert "* accepts any sender, but only together with a " + tr["imap_plus_tag"]["name"] in senders, senders
    assert tr["imap_folder"]["name"] + " other than INBOX" in senders, senders
    assert "no mail is processed" in senders and "left unread" in senders
    assert tr["allowed_senders"]["name"] + " must be set" in tag, tag


# ============================================================== R2-H3 device token leak
def test_docs_say_the_device_token_is_readable_and_how_to_replace_it(tr: dict[str, Any]) -> None:
    text = _flat(DOCS.read_text(encoding="utf-8"))
    assert f"`{PLUGIN_DIR}/`" in text
    assert ("While File Transfer is open on the X3, any device on your Wi-Fi can read that file from the X3's "
            "web server") in text
    assert "open **Setup** in the web UI and select **New device token**" in text
    assert "A `device_token` you set in the options is replaced by changing it there and restarting" in text
    assert "New device token" in tr["device_token"]["description"]
    assert "File Transfer" in tr["device_token"]["description"]
    # The names the docs use are the ones the web UI shows.
    template = SETUP_TEMPLATE.read_text(encoding="utf-8")
    assert ">New device token</button>" in template
    assert "/admin/rotate-device-token" in template
    base = (APP / "sendtox3" / "web" / "templates" / "base.html").read_text(encoding="utf-8")
    assert ">Setup</a>" in base
    # The token really is written into device.json in that folder.
    files = plugin_bundle.plugin_files(base_url="http://192.168.1.50:8765", device_token="tok-123", opts=Options())
    assert b"tok-123" in files[f"{PLUGIN_DIR}/device.json"]


# ============================================================== R2-H4 token characters
def test_device_token_rule_is_exactly_printable_ascii_without_space_braces_quote_backslash() -> None:
    """The docs describe this rule; it must stay exactly what ``_check_token`` does."""
    forbidden = set('{}"\\')
    for cp in range(0x00, 0x80):
        ch = chr(cp)
        ok = 0x21 <= cp <= 0x7E and ch not in forbidden
        assert (device_token_problem(f"ab{ch}cd") == "") is ok, (hex(cp), ch)
    for ch in ("é", " ", "–", "\U0001f600"):
        assert device_token_problem(f"ab{ch}cd"), ch
    assert device_token_problem("")


def test_docs_token_guidance_matches_check_token() -> None:
    units = _units(DOCS)
    device = next(u for u in units if u.startswith("The device token is checked"))
    assert "only printable ASCII characters other than the space" in device, device
    for ch in ("{", "}", '"', "\\"):
        assert f"`{ch}`" in device, (ch, device)
    assert "web UI refuses to build the plugin" in device
    general = next(u for u in units if u.startswith("If you set a token yourself"))
    assert "letters, digits, `-` and `_`" in general, general
    for ch in ("&", "#", "+", "%"):
        assert f"`{ch}`" in general, (ch, general)
    assert "ASCII letters, digits and symbols without spaces" not in _flat(DOCS.read_text(encoding="utf-8"))
    row = _docs_row("device_token")
    assert "Printable ASCII only: no spaces" in row, row


def test_generated_tokens_use_the_recommended_characters(store: Any) -> None:
    """The docs say "letters, digits, - and _, like the generated tokens"."""
    for name in ("ingest_token", "device_token", "admin_token"):
        value = store.secret(name)
        assert re.fullmatch(r"[A-Za-z0-9_-]{20,}", value), (name, value)
        assert device_token_problem(value) == ""


def test_supervisor_check_comment_does_not_name_the_current_version() -> None:
    version = str(load_yaml(CONFIG)["version"])
    text = VALIDATE_CONFIG.read_text(encoding="utf-8")
    assert f"{version} had a `port`" not in text
    assert "An older version had a `port` option" in text
