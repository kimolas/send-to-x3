"""Phone setup: the HTTP Shortcuts (Android) import file, and a note on the iOS shortcut.

The admin Setup page offers :func:`android_import_json` as a download prefilled with
this server's ingest address and token; ``shortcuts/build_android.py`` writes the
generic file committed to the repository (empty settings) from the same code. Only the
standard library is used, so the generator runs without the app's dependencies.

HTTP Shortcuts (ch.rmy.android.http_shortcuts, MIT, github.com/Waboodoo/HTTP-Shortcuts)
imports a plain JSON file in its export format: one category with one shortcut that
appears in Android's share sheet. The format follows the app's source at v4.9.0
(import_export/models/*.kt, Importer.kt, ImportMigrator.kt,
ImportExportDefaultsProvider.kt; format version 92, compatible with 90 and newer):

- Share: global variable ``sendtox3_text`` has "Allow receiving value from share
  dialog" (``isShareText``) for the shared text, ``sendtox3_title`` (``isShareTitle``)
  for the shared subject/title. Both are JSON-encoded (``jsonEncode``: the app
  escapes the value with ``JSONObject.quote``), so ``"{{id}}"`` inside the JSON body
  stays valid JSON for any text. Run without sharing, ``sendtox3_text`` (a Text
  Input variable) asks for the text instead.
- Request: ``POST {{sendtox3_url}}`` with ``Authorization: Bearer {{sendtox3_token}}``
  and ``{"text", "title", "source": "android-http-shortcuts"}``.
- Settings ``sendtox3_url``, ``sendtox3_token``, ``sendtox3_email`` are static
  variables marked "exclude value from export", so importing an updated file keeps
  the values you entered and your own backups do not contain the token.
- "Wait for connection when offline" is on; the app reschedules only when the phone
  has no network at all *and* the host name cannot be resolved (an IP address in the
  URL never triggers it, see HttpExecutionType.shouldReschedule).
- Scripts: before the request, stop with a clear message if the URL or token is
  missing; on success show the server's ``message`` ("Sent to X3: <title>"), and
  treat any answer that is not the app's JSON as a failure; on failure show the
  server's ``error``, or, when the app could not be reached at all (away from home),
  open an email to ``sendtox3_email`` with subject ``[x3] <title>`` so you only have
  to tap Send.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Optional

#: Shown on the Setup page instead of a download: an unsigned shortcut file does not
#: open on an iPhone, and only a Mac signed in to iCloud can sign one.
IOS_SHORTCUT_NOTE = (
    "iPhones and iPads only open shortcut files that are signed, and signing needs a Mac signed in to "
    "iCloud, so this page cannot offer the shortcut as a download. Sign the ready-made shortcut on a Mac "
    "with shortcuts/build_shortcut.py, or build it in the Shortcuts app in about ten minutes: both are "
    "described in docs/phone-ios.md. It needs the address and the ingest token below."
)

INGEST_PATH = "/api/v1/ingest"

#: ImportExport.VERSION / COMPATIBILITY_VERSION in HTTP Shortcuts 4.9.0.
FORMAT_VERSION = 92
COMPATIBILITY_VERSION = 90

SHORTCUT_NAME = "Send to X3"
SOURCE = "android-http-shortcuts"
TIMEOUT_MS = 30_000  # the app answers within its 25 s processing budget

_NS = uuid.UUID("0b8f2f8e-3c5e-4f63-8f1e-6a1f9a4d2c71")


def _id(label: str) -> str:
    """A stable ID in the shape of a random (version 4) UUID: the app only recognises
    ``{{id}}`` placeholders whose ID matches UUIDUtils.UUID_REGEX (version nibble 4,
    variant 8-b), so a plain uuid5 would be sent as literal text."""
    return str(uuid.UUID(bytes=uuid.uuid5(_NS, f"send-to-x3/android/{label}").bytes, version=4))


CATEGORY_ID = _id("category")
SHORTCUT_ID = _id("shortcut")

#: Variable keys (``[A-Za-z0-9_]{1,30}``, prefixed so they never collide with yours).
KEY_URL = "sendtox3_url"
KEY_TOKEN = "sendtox3_token"
KEY_EMAIL = "sendtox3_email"
KEY_TEXT = "sendtox3_text"
KEY_TITLE = "sendtox3_title"
VARIABLE_IDS = {key: _id(f"variable/{key}") for key in (KEY_URL, KEY_TOKEN, KEY_EMAIL, KEY_TEXT, KEY_TITLE)}


def placeholder(key: str) -> str:
    """How the app writes a global variable into a field: ``{{<variable id>}}``."""
    return "{{" + VARIABLE_IDS[key] + "}}"


CODE_ON_PREPARE = f"""\
// Send to X3: stop early with a clear message when the settings are missing.
if (!getVariable('{KEY_URL}') || !getVariable('{KEY_TOKEN}')) {{
  showDialog('Open HTTP Shortcuts, menu > Global Variables, and fill in {KEY_URL} (the ingest address) and '
    + '{KEY_TOKEN} (the ingest token) from the Setup page of the Send to X3 app.', 'Send to X3 is not set up');
  abort();
}}
"""

CODE_ON_SUCCESS = """\
// Show the title the Send to X3 app saved ("Sent to X3: <title>"). Anything that is
// not its JSON answer (a login page, another server) counts as a failure.
let reply = null;
try {
  reply = JSON.parse(response.body);
} catch (e) {}
if (!reply || reply.ok !== true) {
  abortAndTreatAsFailure('not a Send to X3 answer');
}
showToast(reply.message || 'Sent to X3');
"""

CODE_ON_FAILURE = f"""\
// The shared values are JSON-encoded (see the variables); undo that for the email.
function plain(value) {{
  try {{
    return JSON.parse('"' + value + '"');
  }} catch (e) {{
    return value;
  }}
}}
if (response) {{
  // The app answered with an error (wrong token, nothing to send, ...), or something
  // else answered at that address.
  let error = null;
  try {{
    error = JSON.parse(response.body).error;
  }} catch (e) {{}}
  if (!error) {{
    error = 'The address answered HTTP ' + response.statusCode
      + ' but is not the Send to X3 ingest address. Check {KEY_URL} in Global Variables.';
  }}
  showDialog(htmlEncode(error), 'Send to X3: not saved');
}} else {{
  // No answer at all: probably not on the home Wi-Fi. Fall back to email, if set up.
  const to = getVariable('{KEY_EMAIL}');
  if (to) {{
    const title = plain(getVariable('{KEY_TITLE}'));
    const text = plain(getVariable('{KEY_TEXT}'));
    openUrl('mailto:' + to + '?subject=' + encodeURIComponent('[x3] ' + title)
      + '&body=' + encodeURIComponent(text));
  }} else {{
    showDialog(htmlEncode('Could not reach the Send to X3 app (' + networkError + '). '
      + 'Are you on your home Wi-Fi? To send from anywhere, set {KEY_EMAIL} in Global Variables.'), 'Send to X3');
  }}
}}
"""


@dataclass(frozen=True)
class AndroidConfig:
    """Values written into the settings variables. Leave the token empty for a file
    you share; the app keeps values you already entered when they are empty here."""

    ingest_url: str = ""
    token: str = ""
    email: str = ""

    def __post_init__(self) -> None:
        for label, value in (("ingest URL", self.ingest_url), ("token", self.token), ("email", self.email)):
            if any(c in value for c in "\r\n\t") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
                raise ValueError(f"the {label} must be a single line without control characters")


def _body_template() -> str:
    # Field order matches what the server documents; values are JSON-escaped by the app.
    return ('{"text": "' + placeholder(KEY_TEXT) + '", "title": "' + placeholder(KEY_TITLE)
            + '", "source": "' + SOURCE + '"}')


def build_http_shortcuts(cfg: Optional[AndroidConfig] = None) -> dict[str, Any]:
    """The import file as a dictionary (serialize with :func:`json.dumps`)."""
    cfg = cfg or AndroidConfig()
    settings = [
        (KEY_URL, cfg.ingest_url, "Send to X3 ingest address, like http://192.168.1.50:8765/api/v1/ingest"),
        (KEY_TOKEN, cfg.token, "Send to X3 ingest token"),
        (KEY_EMAIL, cfg.email, "Optional: address the Send to X3 app reads, for sending when away from home"),
    ]
    variables: list[dict[str, Any]] = [
        {"id": VARIABLE_IDS[key], "key": key, "type": "constant", "value": value, "title": title,
         "isExcludeValueFromExport": True}
        for key, value, title in settings
    ]
    variables += [
        {"id": VARIABLE_IDS[KEY_TEXT], "key": KEY_TEXT, "type": "text", "value": "", "jsonEncode": True,
         "isShareText": True, "isMultiline": True, "title": SHORTCUT_NAME,
         "message": "Text or link to send to the X3"},
        {"id": VARIABLE_IDS[KEY_TITLE], "key": KEY_TITLE, "type": "constant", "value": "", "jsonEncode": True,
         "isShareTitle": True},
    ]
    shortcut: dict[str, Any] = {
        "id": SHORTCUT_ID,
        "name": SHORTCUT_NAME,
        "description": "Share text or a link to read it on the Xteink X3",
        "executionType": "app",
        "iconName": "flat_color_booklet",
        "method": "POST",
        "url": placeholder(KEY_URL),
        "headers": [{"key": "Authorization", "value": "Bearer " + placeholder(KEY_TOKEN)}],
        "requestBodyType": "custom_text",
        "contentType": "application/json",
        "bodyContent": _body_template(),
        "timeout": TIMEOUT_MS,
        "waitForInternet": True,
        "acceptCookies": False,
        "launcherShortcut": True,
        "excludeFromFileSharing": True,  # text and links only; send PDFs by email
        "responseHandling": {"uiType": "toast", "successOutput": "none", "failureOutput": "none"},
        "codeOnPrepare": CODE_ON_PREPARE,
        "codeOnSuccess": CODE_ON_SUCCESS,
        "codeOnFailure": CODE_ON_FAILURE,
    }
    return {
        "version": FORMAT_VERSION,
        "compatibilityVersion": COMPATIBILITY_VERSION,
        "categories": [{"id": CATEGORY_ID, "name": SHORTCUT_NAME, "layoutType": "linear_list",
                        "shortcuts": [shortcut]}],
        "variables": variables,
    }


def http_shortcuts_bytes(cfg: Optional[AndroidConfig] = None) -> bytes:
    """The import file as written to disk (UTF-8 JSON, two-space indent, final newline)."""
    return (json.dumps(build_http_shortcuts(cfg), indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def ingest_url_for(base_url: str) -> str:
    """``http://<host>:<port>`` -> its ingest address (unchanged if it already is one)."""
    base = (base_url or "").strip().rstrip("/")
    if not base or base.endswith(INGEST_PATH):
        return base
    return base + INGEST_PATH


def android_import_json(base_url: str, ingest_token: str, *, email: str = "") -> bytes:
    """The HTTP Shortcuts import file prefilled with this server's ingest address (from
    ``base_url``, ``http://<host>:<port>``) and ingest token, and optionally the ingest
    email address. It contains the token: serve it only behind admin authentication."""
    return http_shortcuts_bytes(AndroidConfig(ingest_url=ingest_url_for(base_url), token=(ingest_token or "").strip(),
                                              email=(email or "").strip()))
