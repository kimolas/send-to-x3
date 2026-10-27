"""Run the generated "Send to X3" shortcut's logic in Python, for tests.

This is not a Shortcuts replacement: it interprets only the actions that
``build_shortcut.py`` emits, reading them from the generated property list, so the
tests exercise the real action list (branches, variable names, text tokens, JSON
body) rather than the generator's intent. Semantics follow what WorkflowKit does on
macOS 26.5 (measured with ``shortcuts/tools/wfcheck.m``):

- If "is" and "contains" compare text case-sensitively;
- "has any value" is true as soon as there is an item, even an empty text (an empty
  Text action still produces one);
- "Shortcut Input as Safari web page" is empty unless the input is a Safari page;
- a variable embedded in text is its items' text joined with newlines;
- Get URLs from Input finds links as iOS's data detectors (NSDataDetector) do: besides
  ``scheme://`` URLs it returns email addresses as ``mailto:`` links and bare domain
  names as ``http://`` links (modelled with a short list of common top-level domains).

Side effects (HTTP request, email, notifications, alerts) go to callbacks/records.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

OBJ = "￼"
_LINK_RE = re.compile(
    r"""(?P<url>\b[a-z][a-z0-9+.-]*://[^\s<>"']+)"""
    r"""|(?P<email>\b[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b)"""
    r"""|(?P<domain>\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|app|co|uk|de|fr|nl|ch|edu|gov|info)\b"""
    r"""(?:/[^\s<>"']*)?)""",
    re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\""


# ----------------------------------------------------------------------------- items


@dataclass(frozen=True)
class Text:
    value: str


@dataclass(frozen=True)
class URL:
    value: str


@dataclass(frozen=True)
class RichText:
    value: str
    links: tuple[str, ...] = ()  # link targets that are not visible text


@dataclass(frozen=True)
class SafariPage:
    url: str
    title: str
    selection: str = ""


@dataclass(frozen=True)
class Dict:
    value: dict


Item = Union[Text, URL, RichText, SafariPage, Dict]

_CLASS_OF = {Text: "WFStringContentItem", URL: "WFURLContentItem", RichText: "WFRichTextContentItem",
             SafariPage: "WFSafariWebPageContentItem", Dict: "WFDictionaryContentItem"}


def item_text(item: Item) -> str:
    if isinstance(item, (Text, URL, RichText)):
        return item.value
    if isinstance(item, SafariPage):
        return item.url
    return json.dumps(item.value)


def collection_text(items: list[Item]) -> str:
    return "\n".join(item_text(i) for i in items)


def replace_text(pattern: str, template: str, s: str, *, case_sensitive: bool = True) -> str:
    """Replace Text with Regular Expression on: every match of ``pattern`` replaced by
    ``template`` in NSRegularExpression's template syntax (``$1`` is group 1, an unmatched
    group gives nothing, ``\\`` makes the next character literal)."""

    def expand(m: re.Match) -> str:
        out, i = [], 0
        while i < len(template):
            c = template[i]
            if c == "\\" and i + 1 < len(template):
                out.append(template[i + 1])
                i += 2
            elif c == "$" and i + 1 < len(template) and template[i + 1].isdigit():
                j = i + 2  # ICU takes more digits while they still name a group
                while j < len(template) and template[j].isdigit() and int(template[i + 1:j + 1]) <= m.re.groups:
                    j += 1
                n = int(template[i + 1:j])
                if n > m.re.groups:
                    raise ValueError(f"no group {n} in {pattern!r}")
                out.append(m.group(n) or "")
                i = j
            else:
                out.append(c)
                i += 1
        return "".join(out)

    return re.sub(pattern, expand, s, flags=0 if case_sensitive else re.IGNORECASE)


def find_links(s: str) -> list[str]:
    """Get URLs from Input (see the module docstring)."""
    out = []
    for m in _LINK_RE.finditer(s):
        if m.group("email"):
            out.append("mailto:" + m.group("email"))
            continue
        u = m.group(0).rstrip(_TRAILING)
        if u:
            out.append(u if m.group("url") else "http://" + u)
    return out


# ---------------------------------------------------------------------------- result


@dataclass
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str]
    json_body: dict[str, Any]


@dataclass
class Email:
    to: list[str]
    subject: str
    body: str
    show_compose_sheet: bool


@dataclass
class RunResult:
    variables: dict[str, list[Item]] = field(default_factory=dict)
    requests: list[HttpRequest] = field(default_factory=list)
    emails: list[Email] = field(default_factory=list)
    notifications: list[tuple[str, str]] = field(default_factory=list)
    alerts: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False
    executed: list[str] = field(default_factory=list)  # action identifiers, in run order

    def var(self, name: str) -> str:
        return collection_text(self.variables.get(name, []))


class UnsupportedAction(Exception):
    pass


#: ``http(request) -> (status, body bytes)``; raise to simulate a connection failure.
HttpHandler = Callable[[HttpRequest], tuple[int, bytes]]


# ------------------------------------------------------------------------ interpreter


class Simulator:
    def __init__(self, workflow: dict, *, wifi: str = "", http: Optional[HttpHandler] = None,
                 shortcut_input: Optional[list[Item]] = None, clipboard: Optional[list[Item]] = None,
                 empty_items: bool = False):
        """``empty_items``: actions with nothing to report (no Wi-Fi, no selection) return
        one empty text instead of nothing; the shortcut must behave the same either way."""
        self.actions: list[dict] = workflow["WFWorkflowActions"]
        no_input = (workflow.get("WFWorkflowNoInputBehavior") or {}).get("Name", "")
        if not shortcut_input and no_input == "WFWorkflowNoInputBehaviorGetClipboard":
            shortcut_input = list(clipboard or [])
        accepted = set(workflow.get("WFWorkflowInputContentItemClasses", []))
        for it in shortcut_input or []:
            if _CLASS_OF[type(it)] not in accepted:
                raise ValueError(f"the shortcut does not accept {_CLASS_OF[type(it)]}")
        self.input: list[Item] = list(shortcut_input or [])
        self.wifi = wifi
        self.http = http
        self.empty_items = empty_items
        self.vars: dict[str, list[Item]] = {}
        self.outputs: dict[str, list[Item]] = {}
        self.result = RunResult(variables=self.vars)

    # ------------------------------------------------------------------ values
    def resolve(self, value: dict) -> list[Item]:
        """A ``WFTextTokenAttachment`` Value dict -> items."""
        kind = value.get("Type")
        if kind == "Variable":
            items = list(self.vars.get(value["VariableName"], []))
        elif kind == "ActionOutput":
            items = list(self.outputs.get(value["OutputUUID"], []))
        elif kind == "ExtensionInput":
            items = list(self.input)
        else:
            raise UnsupportedAction(f"variable type {kind!r}")
        for agg in value.get("Aggrandizements", []):
            if agg.get("Type") == "WFCoercionVariableAggrandizement":
                items = self._coerce(items, agg["CoercionItemClass"])
            else:
                raise UnsupportedAction(f"aggrandizement {agg!r}")
        return items

    @staticmethod
    def _coerce(items: list[Item], item_class: str) -> list[Item]:
        if item_class == "WFSafariWebPageContentItem":
            return [i for i in items if isinstance(i, SafariPage)]
        if item_class == "WFStringContentItem":
            return [Text(item_text(i)) for i in items]
        raise UnsupportedAction(f"coercion to {item_class}")

    def attachment(self, param: Any) -> list[Item]:
        if not isinstance(param, dict) or param.get("WFSerializationType") != "WFTextTokenAttachment":
            raise UnsupportedAction(f"expected a variable, got {param!r}")
        return self.resolve(param["Value"])

    def text(self, param: Any) -> str:
        """A text field: plain string or ``WFTextTokenString``."""
        if param is None:
            return ""
        if isinstance(param, str):
            return param
        if param.get("WFSerializationType") == "WFTextTokenAttachment":
            return collection_text(self.resolve(param["Value"]))
        if param.get("WFSerializationType") != "WFTextTokenString":
            raise UnsupportedAction(f"text value {param!r}")
        value = param["Value"]
        s: str = value.get("string", "")
        attachments = value.get("attachmentsByRange", {})
        units = s.encode("utf-16-le")
        out, pos = [], 0
        by_pos = {}
        for key, ref in attachments.items():
            m = re.fullmatch(r"\{(\d+), 1\}", key)
            if not m:
                raise UnsupportedAction(f"range {key!r}")
            by_pos[int(m.group(1))] = ref
        for p in sorted(by_pos):
            if units[p * 2:p * 2 + 2].decode("utf-16-le") != OBJ:
                raise ValueError(f"attachment at {p} does not sit on U+FFFC in {s!r}")
            out.append(units[pos * 2:p * 2].decode("utf-16-le"))
            out.append(collection_text(self.resolve(by_pos[p])))
            pos = p + 1
        out.append(units[pos * 2:].decode("utf-16-le"))
        return "".join(out)

    def dictionary(self, param: dict) -> dict[str, str]:
        if param.get("WFSerializationType") != "WFDictionaryFieldValue":
            raise UnsupportedAction(f"dictionary {param!r}")
        out = {}
        for entry in param["Value"]["WFDictionaryFieldValueItems"]:
            if entry["WFItemType"] != 0:
                raise UnsupportedAction("only text dictionary values are simulated")
            out[self.text(entry["WFKey"])] = self.text(entry["WFValue"])
        return out

    # -------------------------------------------------------------- conditions
    def condition(self, p: dict) -> bool:
        subject = p["WFInput"]
        if subject.get("Type") != "Variable":
            raise UnsupportedAction(f"condition input {subject!r}")
        items = self.attachment(subject["Variable"])
        code = p["WFCondition"]
        value = collection_text(items)
        if code == 100:
            return bool(items)
        if code == 101:
            return not items
        other = self.text(p.get("WFConditionalActionString"))
        if code == 4:
            return value == other
        if code == 5:
            return value != other
        if code == 99:
            return other in value
        if code == 999:
            return other not in value
        raise UnsupportedAction(f"condition {code}")

    def _jump_target(self, index: int, modes: tuple[int, ...]) -> int:
        group = self.actions[index]["WFWorkflowActionParameters"]["GroupingIdentifier"]
        for j in range(index + 1, len(self.actions)):
            p = self.actions[j]["WFWorkflowActionParameters"]
            if self.actions[j]["WFWorkflowActionIdentifier"].endswith(".conditional") \
                    and p.get("GroupingIdentifier") == group and p.get("WFControlFlowMode") in modes:
                return j
        raise ValueError(f"unterminated If at action {index}")

    # ------------------------------------------------------------------- run
    def run(self) -> RunResult:
        i = 0
        while i < len(self.actions):
            action = self.actions[i]
            ident = action["WFWorkflowActionIdentifier"].removeprefix("is.workflow.actions.")
            p = action["WFWorkflowActionParameters"]
            self.result.executed.append(ident)
            if ident == "conditional":
                mode = p["WFControlFlowMode"]
                if mode == 0 and not self.condition(p):
                    i = self._jump_target(i, (1, 2)) + 1
                    continue
                if mode == 1:  # reached Otherwise from the true branch
                    i = self._jump_target(i, (2,)) + 1
                    continue
                i += 1
                continue
            if ident == "exit":
                self.result.stopped = True
                break
            out = self._perform(ident, p)
            if "UUID" in p:
                self.outputs[p["UUID"]] = out
            i += 1
        return self.result

    def _perform(self, ident: str, p: dict) -> list[Item]:
        if ident == "comment":
            return []
        if ident == "gettext":
            s = self.text(p.get("WFTextActionText", ""))
            return [Text(s)]
        if ident == "setvariable":
            self.vars[p["WFVariableName"]] = self.attachment(p["WFInput"])
            return []
        if ident == "text.replace":
            if not p.get("WFReplaceTextRegularExpression"):
                raise UnsupportedAction("literal Replace Text")
            return [Text(replace_text(p["WFReplaceTextFind"], p.get("WFReplaceTextReplace", ""),
                                      self.text(p["WFInput"]),
                                      case_sensitive=p.get("WFReplaceTextCaseSensitive", True)))]
        if ident == "properties.safariwebpage":
            pages = [i for i in self.attachment(p["WFInput"]) if isinstance(i, SafariPage)]
            if not pages:
                raise RuntimeError("Get Details of Safari Web Page: input is not a Safari web page")
            prop = p["WFContentItemPropertyName"]
            page = pages[0]
            if prop == "Page URL":
                return [URL(page.url)]
            if prop == "Name":
                return [Text(page.title)]
            if prop == "Page Selection":
                return [Text(page.selection)] if page.selection or self.empty_items else []
            raise UnsupportedAction(f"page property {prop}")
        if ident == "detect.text":
            return [Text(item_text(i)) for i in self.attachment(p["WFInput"])]
        if ident == "detect.link":
            return [URL(u) for u in find_links(self.text(p["WFInput"]))]
        if ident == "getitemfromlist":
            items = self.attachment(p["WFInput"])
            if p.get("WFItemSpecifier") != "First Item":
                raise UnsupportedAction("only First Item")
            return items[:1]
        if ident == "getwifi":
            if p.get("WFNetworkDetailsNetwork") != "Wi-Fi" or p.get("WFWiFiDetail") != "Network Name":
                raise UnsupportedAction("only the Wi-Fi network name")
            return [Text(self.wifi)] if self.wifi or self.empty_items else []
        if ident == "downloadurl":
            return self._http(p)
        if ident == "getvalueforkey":
            src = self.attachment(p["WFInput"])
            key = self.text(p["WFDictionaryKey"])
            for it in src:
                if isinstance(it, Dict) and key in it.value and it.value[key] is not None:
                    v = it.value[key]
                    return [Text(v if isinstance(v, str) else json.dumps(v))]
            return []
        if ident == "notification":
            self.result.notifications.append((self.text(p.get("WFNotificationActionTitle")),
                                              self.text(p.get("WFNotificationActionBody"))))
            return []
        if ident == "alert":
            self.result.alerts.append((self.text(p.get("WFAlertActionTitle")),
                                       self.text(p.get("WFAlertActionMessage"))))
            return []
        if ident == "sendemail":
            to = [item_text(i) for i in self.attachment(p["WFSendEmailActionToRecipients"])]
            self.result.emails.append(Email(to=to, subject=self.text(p.get("WFSendEmailActionSubject")),
                                            body=self.text(p.get("WFSendEmailActionInputAttachments")),
                                            show_compose_sheet=bool(p.get("WFSendEmailActionShowComposeSheet", True))))
            return []
        raise UnsupportedAction(ident)

    def _http(self, p: dict) -> list[Item]:
        if p.get("WFHTTPBodyType") != "JSON":
            raise UnsupportedAction("only JSON bodies")
        req = HttpRequest(method=p.get("WFHTTPMethod", "GET"), url=self.text(p["WFURL"]),
                          headers=self.dictionary(p["WFHTTPHeaders"]) if p.get("ShowHeaders") else {},
                          json_body=self.dictionary(p["WFJSONValues"]))
        self.result.requests.append(req)
        if self.http is None:
            raise RuntimeError("no HTTP handler: the request would fail and stop the shortcut")
        _status, body = self.http(req)  # Shortcuts returns the body for any status; errors stop the run
        try:
            data = json.loads(body)
        except ValueError:
            return [Text(body.decode("utf-8", "replace"))]
        return [Dict(data)] if isinstance(data, dict) else [Text(json.dumps(data))]


def run(workflow: dict, **kwargs: Any) -> RunResult:
    return Simulator(workflow, **kwargs).run()
