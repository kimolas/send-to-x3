#!/usr/bin/env python3
"""Generate the "Send to X3" Apple Shortcut.

The output is an unsigned shortcut file (a binary property list in the format the
Shortcuts app exports), plus an XML twin for review. iPhones only import *signed*
files, so on a Mac signed in to iCloud you can also sign it::

    python3 shortcuts/build_shortcut.py                               # dist/Send to X3.shortcut + .xml
    python3 shortcuts/build_shortcut.py --sign people-who-know-me     # + dist/signed/Send to X3.shortcut
    python3 shortcuts/build_shortcut.py --sign anyone                 # uploads to Apple to sign (see docs)

The shortcut (see docs/phone-ios.md for the action-by-action description):

1. Settings at the top: five clearly labelled Text actions (ingest URL, ingest token,
   home Wi-Fi names, ingest email address, mode), each also an Import Question so the
   Shortcuts app asks for them when the shortcut is added.
2. Collects what was shared: a Safari web page gives its URL, title and selected text;
   a link on its own (a URL, text that is only a web link, or only a domain name such
   as ``example.com/a``, sent as ``https://example.com/a``) is sent as the URL;
   anything else (text, rich text, an article, or the clipboard when run without input)
   is sent as text alone, and the app finds the links in it (Get URLs from Input also
   returns email addresses as ``mailto:`` links, which are not pages to fetch).
3. Mode ``http`` or ``email`` forces that route; ``auto`` (or anything else) sends
   over HTTP when the current Wi-Fi name is one of the home networks, by email otherwise.
   Curly quotes and dashes (iOS Smart Punctuation) are straightened on both sides of
   that comparison.
4. HTTP: ``POST <ingest URL>`` with JSON ``{url, text, title, source: "ios-shortcut"}``
   and ``Authorization: Bearer <token>``, then a notification with the server's
   ``message`` ("Sent to X3: <title>"), or an alert with its ``error``.
5. Email: Send Email without the compose sheet to the ingest address, subject
   ``[x3] <title>``, body ``<link>\\n\\n<text>`` when there is a link (the URL, or for a
   text the first web link written in it), ``<text>`` alone otherwise.

Only the standard library is used, so this runs on any Python >= 3.10.
"""

from __future__ import annotations

import argparse
import copy
import os
import plistlib
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence, Union

# --------------------------------------------------------------------------------------
# Constants (verified against WorkflowKit on macOS 26.5.1 and Apple's gallery shortcuts)
# --------------------------------------------------------------------------------------

SHORTCUT_NAME = "Send to X3"
HERE = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = HERE / "dist"

#: Version stamp of the app that "wrote" the file. Apple's own gallery files on macOS
#: 26.5 use "4018.0.4"; a current WorkflowKit writes 4610. Files stamped older than the
#: reading app are migrated when opened, files stamped newer are read as they are. We
#: use the gallery value: running WorkflowKit's migrations on this file changes nothing
#: but the stamp (checked with shortcuts/tools/wfcheck.m).
CLIENT_VERSION = "4018.0.4"
MINIMUM_CLIENT_VERSION = 900  # same as every gallery shortcut

#: Share-sheet input types: Text, Rich text, URLs, Safari web pages, Articles.
INPUT_CLASSES: tuple[str, ...] = (
    "WFStringContentItem",
    "WFRichTextContentItem",
    "WFURLContentItem",
    "WFSafariWebPageContentItem",
    "WFArticleContentItem",
)

ICON_COLOR_DARK_GRAY = 255  # WFWorkflowIconStartColor values used by the Shortcuts app
ICON_GLYPH_PAPER_PLANE = 61467

MODES: tuple[str, ...] = ("auto", "http", "email")
SIGN_MODES: tuple[str, ...] = ("people-who-know-me", "anyone")

#: Condition codes of the If action (``WFCondition``).
COND_IS = 4
COND_CONTAINS = 99
COND_HAS_ANY_VALUE = 100
COND_HAS_NO_VALUE = 101

#: Object replacement character: marks where a variable sits inside a text field.
OBJ = "\ufffc"

#: Fixed namespace so every build produces byte-identical output (stable UUIDs).
_UUID_NAMESPACE = uuid.UUID("6d0f3a52-7d1e-4c3e-9a63-3b1f1c2a5e10")

#: Variable names the shortcut uses (shown to the user in the editor).
V_INGEST_URL = "IngestURL"
V_TOKEN = "IngestToken"
V_HOME_WIFI = "HomeWiFi"
V_EMAIL = "IngestEmail"
V_MODE = "Mode"
V_URL = "SharedURL"
V_TITLE = "SharedTitle"
V_TEXT = "SharedText"
V_LINK = "SharedLink"  # the link that heads an email: SharedURL, or the first link in a text
V_BODY = "EmailBody"
V_WIFI = "CurrentWiFi"
V_HOME_LIST = "HomeWiFiList"
V_ROUTE = "Route"

#: Separators accepted in the home Wi-Fi setting: commas, semicolons or new lines, with
#: the spaces around them; runs of them collapse, so an empty entry never matches.
HOME_WIFI_SEPARATOR_RE = r"\s*[,;\n][\s,;\n]*"

#: iOS Smart Punctuation (on by default) curls quotes and turns "--" into a dash in what
#: is typed into the setup questions, so `Miller's` arrives as `Miller’s`; a network
#: named on a router's web page keeps straight ones. Both the home list and the current
#: network name are straightened with these (pattern, replacement) pairs before comparing.
SMART_PUNCTUATION: tuple[tuple[str, str], ...] = (
    ("[\u2018\u2019\u201a\u201b\u02bc]", "'"),
    ("[\u201c\u201d\u201e\u201f]", '"'),
    ("\u2014", "--"),
)

#: Get URLs from Input finds links the way iOS's data detectors do: an email address
#: comes back as a ``mailto:`` link and a bare domain as ``http://``. Run on that list
#: (one link per line) with the replacement ``$1``, this leaves the first web link, or
#: nothing: a ``mailto:`` or ``tel:`` link is never the shared page.
FIRST_WEB_LINK_RE = r"^(?:(?!https?://).*\n)*(https?://.*)?[\s\S]*$"

#: A share that is only a domain name, maybe with a path (``example.com/a``), as people
#: type a link. Replaced with ``http://$1`` it must equal the first link iOS found, so
#: iOS (which knows the real top-level domains) agrees that it is a link; it is then
#: sent as ``https://$1``.
BARE_HOST_RE = r"^\s*((?:[a-z0-9-]+\.)+[a-z]{2,63}(?::[0-9]{1,5})?(?:[/?#]\S*)?)\s*$"

#: An empty Text action still produces an (empty) text item, and WorkflowKit's
#: "has any value" is true for it, so "is this setting empty?" is asked as
#: ``"<" + value + ">" is "<>"`` instead (true for no items and for empty text).
BLANK = "<>"


@dataclass(frozen=True)
class ShortcutConfig:
    """Values written into the shortcut's settings (and offered as import answers).

    Leave secrets empty for a file you share: the person adding the shortcut fills
    them in. Anything set here is visible to whoever gets the file, and with
    ``--sign anyone`` also to Apple's signing service.
    """

    ingest_url: str = ""
    token: str = ""
    home_wifi: str = ""
    email: str = ""
    mode: str = "auto"
    name: str = SHORTCUT_NAME

    def __post_init__(self) -> None:
        mode = self.mode.strip().lower()
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, not {self.mode!r}")
        object.__setattr__(self, "mode", mode)
        if not self.name.strip() or any(c in self.name for c in "/\\:\0"):
            raise ValueError(f"invalid shortcut name {self.name!r}")
        for label, value in (("ingest URL", self.ingest_url), ("token", self.token), ("email", self.email)):
            if "\n" in value or "\r" in value:
                raise ValueError(f"the {label} must be a single line")


# --------------------------------------------------------------------------------------
# Value serialization
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    """Something a parameter can refer to: a named variable, an action's output (a
    "magic variable") or the Shortcut Input. ``value`` is the inner ``Value`` dict of
    a ``WFTextTokenAttachment``."""

    value: dict

    def attachment(self) -> dict:
        """The whole parameter is this value (variable-picker parameters)."""
        return {"Value": copy.deepcopy(self.value), "WFSerializationType": "WFTextTokenAttachment"}

    def condition_input(self) -> dict:
        """Left-hand side of an If action."""
        return {"Type": "Variable", "Variable": self.attachment()}

    def coerced(self, item_class: str) -> Ref:
        """``<this> as <type>`` (e.g. Shortcut Input as Safari web page)."""
        return self._aggrandized({"Type": "WFCoercionVariableAggrandizement", "CoercionItemClass": item_class})

    def _aggrandized(self, aggrandizement: dict) -> Ref:
        v = copy.deepcopy(self.value)
        v.setdefault("Aggrandizements", []).append(aggrandizement)
        return Ref(v)


def variable(name: str) -> Ref:
    """A named variable (set with Set Variable)."""
    return Ref({"Type": "Variable", "VariableName": name})


SHORTCUT_INPUT = Ref({"Type": "ExtensionInput"})

TextPart = Union[str, Ref]


def text(*parts: TextPart) -> Union[str, dict]:
    """A text field value: a plain string, or a ``WFTextTokenString`` when it embeds
    variables. Attachment positions count UTF-16 code units, as Foundation does."""
    if all(isinstance(p, str) for p in parts):
        return "".join(parts)  # type: ignore[arg-type]
    return text_token(*parts)


def text_token(*parts: TextPart) -> dict:
    """Always a ``WFTextTokenString`` (dictionary keys and values need this form)."""
    out: list[str] = []
    attachments: dict[str, dict] = {}
    pos = 0
    for part in parts:
        if isinstance(part, Ref):
            attachments[f"{{{pos}, 1}}"] = copy.deepcopy(part.value)
            out.append(OBJ)
            pos += 1
        else:
            out.append(part)
            pos += len(part.encode("utf-16-le")) // 2
    value: dict[str, Any] = {"string": "".join(out)}
    if attachments:
        value["attachmentsByRange"] = attachments
    return {"Value": value, "WFSerializationType": "WFTextTokenString"}


def dictionary_field(items: Sequence[tuple[str, Sequence[TextPart]]]) -> dict:
    """A dictionary parameter (JSON body, headers) whose values are all text."""
    return {
        "Value": {
            "WFDictionaryFieldValueItems": [
                {"WFItemType": 0, "WFKey": text_token(key), "WFValue": text_token(*value)}
                for key, value in items
            ]
        },
        "WFSerializationType": "WFDictionaryFieldValue",
    }


# --------------------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------------------


@dataclass
class ImportQuestion:
    action_index: int
    parameter_key: str
    text: str
    default_value: str

    def serialized(self) -> dict:
        # Shape confirmed with WorkflowKit's own WFWorkflowImportQuestion
        # (initWithSerializedRepresentation:workflowActions: and back).
        return {
            "ActionIndex": self.action_index,
            "Category": "Parameter",
            "DefaultValue": self.default_value,
            "ParameterKey": self.parameter_key,
            "Text": self.text,
        }


@dataclass
class ShortcutBuilder:
    """Appends actions and keeps UUIDs deterministic."""

    seed: str
    actions: list[dict] = field(default_factory=list)
    questions: list[ImportQuestion] = field(default_factory=list)
    _labels: set[str] = field(default_factory=set)
    _ifs: int = 0

    def uuid(self, label: str) -> str:
        return str(uuid.uuid5(_UUID_NAMESPACE, f"{self.seed}/{label}")).upper()

    def act(self, identifier: str, params: Optional[dict] = None) -> None:
        """Append ``is.workflow.actions.<identifier>`` whose output nothing uses."""
        self.actions.append({"WFWorkflowActionIdentifier": f"is.workflow.actions.{identifier}",
                             "WFWorkflowActionParameters": dict(params or {})})

    def produce(self, identifier: str, params: dict, label: str, output_name: str) -> Ref:
        """Append an action and return a reference to its output ("magic variable").
        ``output_name`` is the name the app shows for it (the action's default)."""
        if label in self._labels:
            raise ValueError(f"duplicate action label {label!r}")
        self._labels.add(label)
        uid = self.uuid(label)
        self.act(identifier, {**params, "UUID": uid})
        return Ref({"OutputName": output_name, "OutputUUID": uid, "Type": "ActionOutput"})

    @property
    def last_index(self) -> int:
        return len(self.actions) - 1

    # ---------------------------------------------------------------- common actions
    def comment(self, body: str) -> None:
        self.act("comment", {"WFCommentActionText": body})

    def text(self, label: str, *parts: TextPart) -> Ref:
        return self.produce("gettext", {"WFTextActionText": text(*parts)}, label, "Text")

    def set_variable(self, name: str, value: Ref) -> Ref:
        self.act("setvariable", {"WFInput": value.attachment(), "WFVariableName": name})
        return variable(name)

    def set_text_variable(self, name: str, label: str, *parts: TextPart) -> Ref:
        return self.set_variable(name, self.text(label, *parts))

    def replace(self, label: str, subject: Sequence[TextPart], pattern: str, replacement: str) -> Ref:
        """Replace Text with a regular expression (ICU syntax, as NSRegularExpression)."""
        return self.produce("text.replace", {
            "WFInput": text(*subject), "WFReplaceTextCaseSensitive": False, "WFReplaceTextFind": pattern,
            "WFReplaceTextRegularExpression": True, "WFReplaceTextReplace": replacement,
        }, label, "Updated Text")

    def straighten(self, label: str, value: Ref) -> Ref:
        """``value`` with curly quotes and dashes made plain (see SMART_PUNCTUATION)."""
        for n, (pattern, replacement) in enumerate(SMART_PUNCTUATION):
            value = self.replace(f"{label}-{n}", [value], pattern, replacement)
        return value

    def alert(self, title: str, *message: TextPart) -> None:
        self.act("alert", {"WFAlertActionCancelButtonShown": False, "WFAlertActionMessage": text(*message),
                           "WFAlertActionTitle": title})

    def stop(self) -> None:
        self.act("exit")

    def notification(self, title: str, *body: TextPart) -> None:
        self.act("notification", {"WFNotificationActionBody": text(*body), "WFNotificationActionSound": False,
                                  "WFNotificationActionTitle": title})

    def question(self, text_: str, default: str) -> None:
        """Ask for the Text action just added when the shortcut is imported."""
        self.questions.append(ImportQuestion(self.last_index, "WFTextActionText", text_, default))

    def setting(self, variable_name: str, label: str, value: str, question: str) -> Ref:
        """A labelled Text action + Import Question + Set Variable."""
        ref = self.text(label, value)
        self.question(question, value)
        return self.set_variable(variable_name, ref)

    # ------------------------------------------------------------------ control flow
    @contextmanager
    def if_(self, subject: Ref, condition: int, value: Optional[Union[str, Sequence[TextPart]]] = None
            ) -> Iterator[_IfBlock]:
        """``If <subject> <condition> [value] ... [Otherwise ...] End If``.

        Written in the single-condition form (``WFCondition``/``WFInput``/
        ``WFConditionalActionString``) that every Shortcuts version reads; the
        current app turns it into its condition table when it loads the file.
        """
        n = self._ifs
        self._ifs += 1
        group = self.uuid(f"if-{n}")
        params: dict[str, Any] = {"GroupingIdentifier": group, "WFCondition": condition,
                                  "WFControlFlowMode": 0, "WFInput": subject.condition_input()}
        if value is not None:
            params["WFConditionalActionString"] = text(value) if isinstance(value, str) else text(*value)
        self.act("conditional", params)
        yield _IfBlock(self, group)
        self.act("conditional", {"GroupingIdentifier": group, "UUID": self.uuid(f"if-{n}-end"),
                                 "WFControlFlowMode": 2})


    @contextmanager
    def if_blank(self, label: str, *values: Ref) -> Iterator[_IfBlock]:
        """``If`` all ``values`` are empty (no items, or only empty text)."""
        probe = self.text(label, "<", *values, ">")
        with self.if_(probe, COND_IS, BLANK) as block:
            yield block


@dataclass
class _IfBlock:
    builder: ShortcutBuilder
    group: str
    has_otherwise: bool = False

    def otherwise(self) -> None:
        if self.has_otherwise:
            raise RuntimeError("Otherwise used twice in one If")
        self.has_otherwise = True
        self.builder.act("conditional", {"GroupingIdentifier": self.group, "WFControlFlowMode": 1})


# --------------------------------------------------------------------------------------
# The shortcut
# --------------------------------------------------------------------------------------

SETTINGS_COMMENT = """\
{name}: settings

Edit the five Text actions below. (When you add this shortcut from a file, the Shortcuts app asks for them.)

1. Ingest URL: the "Address" on the Setup page of the Send to X3 app, like http://192.168.1.50:8765/api/v1/ingest (use the IP address, not a .local name).
2. Ingest token: the "Ingest token" from the same page.
3. Home Wi-Fi names, separated by commas, spelled exactly as on the phone (capitals matter). On these networks the shortcut sends directly; elsewhere it sends an email.
4. Ingest email address: the address the app reads mail from (for example yourname+x3secret@gmail.com). Leave it empty if you do not use email.
5. Mode: auto (direct at home, email elsewhere), http (always direct) or email (always email). Anything else counts as auto."""

QUESTION_INGEST_URL = ("Send to X3 address: the ingest URL from the app's Setup page, "
                       "like http://192.168.1.50:8765/api/v1/ingest")
QUESTION_TOKEN = "Send to X3 ingest token (from the app's Setup page)"
QUESTION_HOME_WIFI = ("Your home Wi-Fi network names, separated by commas (capitals matter). On these "
                      "the shortcut sends directly; elsewhere it sends an email.")
QUESTION_EMAIL = ("Email address the Send to X3 app reads, for sending when away from home. "
                  "Leave empty to send over Wi-Fi only.")
QUESTION_MODE = "Mode: auto (direct at home, email elsewhere), http (always direct) or email (always email)"

#: Mode spellings accepted besides the canonical ones (matched case-insensitively).
MODE_HTTP_RE = r"(?i)^\s*(?:https?|wi-?fi|lan|direct)\s*$"
MODE_EMAIL_RE = r"(?i)^\s*e-?mail\s*$"


def build_workflow(cfg: Optional[ShortcutConfig] = None) -> dict:
    """The shortcut as a property-list dictionary."""
    cfg = cfg or ShortcutConfig()
    b = ShortcutBuilder(seed="send-to-x3")

    # ---------------------------------------------------------------- 1. settings
    b.comment(SETTINGS_COMMENT.format(name=cfg.name))
    ingest_url = b.setting(V_INGEST_URL, "cfg-ingest-url", cfg.ingest_url, QUESTION_INGEST_URL)
    token = b.setting(V_TOKEN, "cfg-token", cfg.token, QUESTION_TOKEN)
    home_wifi = b.setting(V_HOME_WIFI, "cfg-home-wifi", cfg.home_wifi, QUESTION_HOME_WIFI)
    email = b.setting(V_EMAIL, "cfg-email", cfg.email, QUESTION_EMAIL)
    mode_text = b.text("cfg-mode", cfg.mode)
    b.question(QUESTION_MODE, cfg.mode)
    # The If action compares text case-sensitively and iOS capitalizes the first typed
    # letter, so "HTTP", " Email " and friends are turned into http / email first.
    as_http = b.replace("mode-http", [mode_text], MODE_HTTP_RE, "http")
    as_email = b.replace("mode-email", [as_http], MODE_EMAIL_RE, "email")
    mode = b.set_variable(V_MODE, as_email)

    # ------------------------------------------------------------ 2. what was shared
    b.comment("What was shared: a Safari page gives its address, title and selected text. A link on its "
              "own, or a domain name like example.com, is sent as the address. Anything else (text, an "
              "article, or the clipboard when run without input) is sent as text, and the app finds the "
              "links in it. SharedLink is the link an email starts with.")
    shared_url, shared_title, shared_text = variable(V_URL), variable(V_TITLE), variable(V_TEXT)
    shared_link = variable(V_LINK)
    with b.if_(SHORTCUT_INPUT.coerced("WFSafariWebPageContentItem"), COND_HAS_ANY_VALUE) as is_page:
        for label, prop, name in (("page-url", "Page URL", V_URL), ("page-name", "Name", V_TITLE),
                                  ("page-selection", "Page Selection", V_TEXT)):
            detail = b.produce("properties.safariwebpage",
                               {"WFContentItemPropertyName": prop, "WFInput": SHORTCUT_INPUT.attachment()},
                               label, prop)
            b.set_variable(name, detail)
        b.set_variable(V_LINK, shared_url)
        is_page.otherwise()
        b.set_variable(V_TEXT, b.produce("detect.text", {"WFInput": SHORTCUT_INPUT.attachment()},
                                         "input-text", "Text"))
        # Links are taken from the plain text, so a rich-text selection's hidden link
        # targets do not become the source URL.
        urls = b.produce("detect.link", {"WFInput": text(shared_text)}, "input-urls", "URLs")
        # Email addresses come back as mailto: links: only a web link is a page.
        b.set_variable(V_URL, b.replace("input-web-url", [urls], FIRST_WEB_LINK_RE, "$1"))
        b.set_text_variable(V_TITLE, "input-title", "")
        with b.if_(shared_text, COND_IS, [shared_url]) as link_only:
            # Only a link was shared: send it as the URL alone (the app fetches the page).
            b.set_text_variable(V_TEXT, "link-only-text", "")
            b.set_variable(V_LINK, shared_url)
            link_only.otherwise()
            host = b.replace("bare-host-probe", [shared_text], BARE_HOST_RE, "http://$1")
            with b.if_(host, COND_IS, [shared_url]) as bare_host:
                # Only a domain name (example.com/a), a link typed without https://.
                b.set_variable(V_URL, b.replace("bare-host-url", [shared_text], BARE_HOST_RE, "https://$1"))
                b.set_text_variable(V_TEXT, "bare-host-text", "")
                b.set_variable(V_LINK, shared_url)
                bare_host.otherwise()
                # Text (with or without links): over HTTP the text goes alone. The app finds
                # the links and tells a link with a comment from a note that mentions a link;
                # a URL sent next to the text would make the text count as a selection from
                # that page. An email starts with the first web link, if the text has it as
                # written (not a domain name iOS made into a link, which the app does not see).
                b.set_text_variable(V_LINK, "text-link", "")
                with b.if_(b.text("text-as-written", shared_text), COND_CONTAINS, [shared_url]):
                    b.set_variable(V_LINK, shared_url)
                b.set_text_variable(V_URL, "text-only-url", "")
    with b.if_blank("nothing-shared", shared_text, shared_url):
        b.alert("Nothing to send", "Share a web page, a link or some text to ", cfg.name,
                ", or copy something and run it again.")
        b.stop()

    # ------------------------------------------------------------- 3. pick the route
    b.comment("Route: http and email always use that route. Otherwise (auto) send directly when the "
              "current Wi-Fi network is one of the home networks, and by email everywhere else.")
    route = variable(V_ROUTE)
    with b.if_(mode, COND_IS, "http") as forced_http:
        b.set_text_variable(V_ROUTE, "route-forced-http", "http")
        forced_http.otherwise()
        with b.if_(mode, COND_IS, "email") as forced_email:
            b.set_text_variable(V_ROUTE, "route-forced-email", "email")
            forced_email.otherwise()
            wifi_name = b.produce("getwifi", {"WFNetworkDetailsNetwork": "Wi-Fi", "WFWiFiDetail": "Network Name"},
                                  "wifi-name", "Network Details")
            current = b.set_variable(V_WIFI, b.straighten("wifi-name-straight", wifi_name))
            home_list = b.set_variable(V_HOME_LIST, b.straighten("home-wifi-list-straight", b.replace(
                "home-wifi-list", [",", home_wifi, ","], HOME_WIFI_SEPARATOR_RE, ",")))
            b.set_text_variable(V_ROUTE, "route-away-email", "email")
            # ",Home,Guest Net," contains ",<current network>,"; off Wi-Fi the name is
            # empty and ",," never occurs in the collapsed list.
            with b.if_(home_list, COND_CONTAINS, [",", current, ","]):
                b.set_text_variable(V_ROUTE, "route-home-http", "http")

    # ---------------------------------------------------------------- 4. send it
    with b.if_(route, COND_IS, "http") as http:
        b.comment("Send directly to the Send to X3 app and show the title it saved.")
        with b.if_blank("no-ingest-url", ingest_url):
            b.alert(f"{cfg.name} is not set up", "Open the shortcut and fill in the ingest URL at the top.")
            b.stop()
        with b.if_blank("no-token", token):
            b.alert(f"{cfg.name} is not set up", "Open the shortcut and fill in the ingest token at the top.")
            b.stop()
        reply = b.produce("downloadurl", {
            "ShowHeaders": True,
            "WFHTTPBodyType": "JSON",
            "WFHTTPHeaders": dictionary_field([("Authorization", ["Bearer ", token])]),
            "WFHTTPMethod": "POST",
            "WFJSONValues": dictionary_field([
                ("url", [shared_url]),
                ("text", [shared_text]),
                ("title", [shared_title]),
                ("source", ["ios-shortcut"]),
            ]),
            "WFURL": text(ingest_url),
        }, "post", "Contents of URL")
        message = b.produce("getvalueforkey", {"WFDictionaryKey": "message", "WFGetDictionaryValueType": "Value",
                                               "WFInput": reply.attachment()}, "reply-message", "Dictionary Value")
        with b.if_(message, COND_HAS_ANY_VALUE) as ok:
            b.notification(cfg.name, message)  # "Sent to X3: <title>" (or "Already in the queue: ...")
            ok.otherwise()
            error = b.produce("getvalueforkey", {"WFDictionaryKey": "error", "WFGetDictionaryValueType": "Value",
                                                 "WFInput": reply.attachment()}, "reply-error", "Dictionary Value")
            b.alert(f"{cfg.name}: not saved", error,
                    "\n\nCheck the ingest URL and token at the top of the shortcut.")
        http.otherwise()
        b.comment("Send by email: the app picks it up from the mailbox it watches.")
        with b.if_blank("no-email", email):
            b.alert(f"{cfg.name}: no email address",
                    "To send from outside your home Wi-Fi, fill in the ingest email address at the top of "
                    "the shortcut. To always send directly, set the mode to http.")
            b.stop()
        # "<link>\n\n<text>" when there is a link (the app reads it as the url/text pair
        # an HTTP request would carry), else the text alone.
        with b.if_blank("no-email-link", shared_link) as no_link:
            b.set_text_variable(V_BODY, "email-body-text", shared_text)
            no_link.otherwise()
            b.set_text_variable(V_BODY, "email-body-link", shared_link, "\n\n", shared_text)
        b.act("sendemail", {
            "WFSendEmailActionInputAttachments": text(variable(V_BODY)),
            "WFSendEmailActionShowComposeSheet": False,
            "WFSendEmailActionSubject": text("[x3] ", shared_title),
            "WFSendEmailActionToRecipients": email.attachment(),
        })
        b.notification(cfg.name, "Emailed. It shows up on the X3 after the app reads the mailbox.")

    return {
        "WFQuickActionSurfaces": [],
        "WFWorkflowActions": b.actions,
        "WFWorkflowClientVersion": CLIENT_VERSION,
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowIcon": {"WFWorkflowIconGlyphNumber": ICON_GLYPH_PAPER_PLANE,
                           "WFWorkflowIconStartColor": ICON_COLOR_DARK_GRAY},
        "WFWorkflowImportQuestions": [q.serialized() for q in b.questions],
        "WFWorkflowInputContentItemClasses": list(INPUT_CLASSES),
        "WFWorkflowMinimumClientVersion": MINIMUM_CLIENT_VERSION,
        "WFWorkflowMinimumClientVersionString": str(MINIMUM_CLIENT_VERSION),
        "WFWorkflowNoInputBehavior": {"Name": "WFWorkflowNoInputBehaviorGetClipboard", "Parameters": {}},
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": ["ActionExtension"],
    }


# --------------------------------------------------------------------------------------
# Files, signing, CLI
# --------------------------------------------------------------------------------------


def shortcut_bytes(cfg: Optional[ShortcutConfig] = None, *, xml: bool = False) -> bytes:
    """The unsigned shortcut file: a binary property list (``xml=True``: XML)."""
    fmt = plistlib.FMT_XML if xml else plistlib.FMT_BINARY
    return plistlib.dumps(build_workflow(cfg), fmt=fmt, sort_keys=True)


def write_shortcut(out_dir: Path, cfg: Optional[ShortcutConfig] = None) -> tuple[Path, Path]:
    """Write ``<name>.shortcut`` (binary) and ``<name>.xml`` (same content, readable)."""
    cfg = cfg or ShortcutConfig()
    out_dir.mkdir(parents=True, exist_ok=True)
    binary, xml = out_dir / f"{cfg.name}.shortcut", out_dir / f"{cfg.name}.xml"
    _write_if_changed(binary, shortcut_bytes(cfg))
    _write_if_changed(xml, shortcut_bytes(cfg, xml=True))
    return binary, xml


def _write_if_changed(path: Path, data: bytes) -> None:
    if path.is_file() and path.read_bytes() == data:
        return  # keep the mtime (the repo lives on a synced drive)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class SigningError(RuntimeError):
    pass


def shortcuts_cli() -> Optional[str]:
    """Path of macOS's ``shortcuts`` command, if this is a Mac that has it."""
    return shutil.which("shortcuts") if sys.platform == "darwin" else None


def sign_shortcut(unsigned: Path, signed: Path, *, mode: str = "people-who-know-me", attempts: int = 3,
                  retry_delay: float = 5.0) -> Path:
    """Sign with ``shortcuts sign`` (needs a Mac signed in to iCloud).

    ``people-who-know-me`` signs locally with your iCloud identity: the file can be
    imported by you and by people who have you in their contacts. ``anyone`` sends
    the shortcut to Apple, which signs it so anyone can import it; Apple's service
    sometimes answers HTTP 500, so it is retried with a growing delay.
    """
    if mode not in SIGN_MODES:
        raise ValueError(f"sign mode must be one of {', '.join(SIGN_MODES)}")
    cli = shortcuts_cli()
    if cli is None:
        raise SigningError("signing needs macOS with the 'shortcuts' command (macOS 12 or newer)")
    signed.parent.mkdir(parents=True, exist_ok=True)
    tmp = signed.with_name(signed.name + ".tmp")
    last = ""
    for attempt in range(1, max(1, attempts) + 1):
        tmp.unlink(missing_ok=True)
        proc = subprocess.run([cli, "sign", "--mode", mode, "--input", str(unsigned), "--output", str(tmp)],
                              capture_output=True, text=True, timeout=180, check=False)
        if proc.returncode == 0 and tmp.is_file() and tmp.read_bytes()[:4] == b"AEA1":
            os.replace(tmp, signed)
            return signed
        last = (proc.stderr or proc.stdout or f"exit status {proc.returncode}").strip()
        if mode != "anyone" or attempt == attempts:
            break
        time.sleep(retry_delay * attempt)
    tmp.unlink(missing_ok=True)
    raise SigningError(f"shortcuts sign --mode {mode} failed: {last}")


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate the 'Send to X3' Apple Shortcut (unsigned, plus a signed copy with --sign).",
        epilog="Values given here are written into the file as the settings' defaults, readable by anyone "
               "who gets the file. Leave the token and the email address empty for a file you share: "
               "--sign anyone refuses --token, and --email unless --allow-baked-values is given.")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                   help=f"output folder (default: {DEFAULT_OUT_DIR})")
    p.add_argument("--name", default=SHORTCUT_NAME, help="shortcut name, also the file name")
    p.add_argument("--ingest-url", default="", help="e.g. http://192.168.1.50:8765/api/v1/ingest")
    p.add_argument("--token", default="", help="ingest token (avoid in files you share)")
    p.add_argument("--home-wifi", default="", help="home Wi-Fi names, comma separated")
    p.add_argument("--email", default="", help="ingest email address")
    p.add_argument("--mode", default="auto", choices=MODES)
    p.add_argument("--sign", choices=SIGN_MODES,
                   help="also write a signed copy to <out-dir>/signed/ (macOS only; 'anyone' uploads to Apple)")
    p.add_argument("--allow-baked-values", action="store_true",
                   help="with --sign anyone, allow --email in the file (never --token)")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        cfg = ShortcutConfig(ingest_url=args.ingest_url.strip(), token=args.token.strip(),
                             home_wifi=args.home_wifi.strip(), email=args.email.strip(), mode=args.mode,
                             name=args.name.strip())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.sign == "anyone":
        if cfg.token:
            print("error: refusing to send a file with a baked-in token to Apple's signing service; "
                  "leave --token empty (the importer is asked for it)", file=sys.stderr)
            return 2
        # The address carries the secret +tag that lets mail in: whoever has the file
        # could add items. A file anyone can open is meant to be passed around.
        if cfg.email and not args.allow_baked_values:
            print("error: refusing to bake the ingest email address into a file anyone can open (its +tag "
                  "is what lets mail in); leave --email empty (the importer is asked for it), or pass "
                  "--allow-baked-values if the file stays with you", file=sys.stderr)
            return 2
        baked = [flag for flag, value in (("--ingest-url", cfg.ingest_url), ("--home-wifi", cfg.home_wifi),
                                          ("--email", cfg.email)) if value]
        if baked:
            print(f"warning: {', '.join(baked)} will be readable by anyone who gets the signed file",
                  file=sys.stderr)
    binary, xml = write_shortcut(args.out_dir, cfg)
    print(f"wrote {binary}")
    print(f"wrote {xml}")
    if args.sign:
        try:
            signed = sign_shortcut(binary, args.out_dir / "signed" / binary.name, mode=args.sign)
        except (SigningError, subprocess.TimeoutExpired, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {signed} (signed: {args.sign})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
