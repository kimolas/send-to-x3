"""Packaging checks for the Home Assistant app (no network, no docker).

These keep ``send_to_x3/config.yaml`` in lockstep with ``sendtox3.config.Options``,
and catch the packaging mistakes that only show up on a real Home Assistant host:
a missing translation, CRLF in run.sh, a stray ``config.json`` that the Supervisor
would treat as a second app, an unpinned workflow action, and so on.

The YAML files are parsed with PyYAML when it is installed, else with the small
strict parser below (PyYAML is not a project dependency). The parser accepts only
the subset these files use and raises on anything else, and
``test_mini_yaml_matches_pyyaml`` cross-checks it whenever PyYAML is present (CI
installs it). The Supervisor's own validators run in ``e2e/ha/validate_config.py``.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any, get_type_hints

import pytest

import sendtox3
from sendtox3.config import DEFAULT_PORT, Options, load_options

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "send_to_x3"
CONFIG = APP / "config.yaml"
TRANSLATIONS = APP / "translations" / "en.yaml"
REPOSITORY = REPO / "repository.yaml"
DOCKERFILE = APP / "Dockerfile"
RUN_SH = APP / "run.sh"
WORKFLOWS = REPO / ".github" / "workflows"
CHECK_ALL = REPO / "scripts" / "check_all.sh"

BASE_IMAGE = "ghcr.io/home-assistant/base-python:3.14-alpine3.24"
BUILDER_VERSION = "2026.09.0"
SECRET_OPTIONS = {"ingest_token", "device_token", "admin_token", "imap_password"}
PORT_OPTIONS = {"imap_port"}
# Options fields kept out of the Home Assistant form: settable only outside the
# Supervisor (SENDTOX3_<NAME>). `port` is the container port, and ingress, the
# watchdog and the published host port are all fixed at 8765.
DEV_ONLY_OPTIONS = {"port"}

try:  # optional: used when available, cross-checked against the mini parser
    import yaml as pyyaml
except ImportError:  # pragma: no cover - depends on the environment
    pyyaml = None


# ============================================================ strict mini YAML
class UnsupportedYaml(ValueError):
    """The file uses YAML this parser does not implement (use PyYAML for it)."""


_BOOLS = {
    **dict.fromkeys(("true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"), True),
    **dict.fromkeys(("false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"), False),
}
_NULLS = {"", "~", "null", "Null", "NULL"}
_INT = re.compile(r"[-+]?(0|[1-9][0-9]*)")
_FLOAT = re.compile(r"[-+]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?")
_RISKY_PLAIN = re.compile(r"^[&*!|>%@`{\[\]'\"]|^0[0-9]|^[-+]?0[xob]|^[0-9]+(:[0-9]+)+$|^\.(inf|nan)$", re.I)


def _strip_comment(text: str) -> str:
    quote, escaped = "", False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in "\"'" and (i == 0 or text[i - 1] in " [,:"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] == " "):
            return text[:i].rstrip()
    if quote:
        raise UnsupportedYaml(f"unterminated or multi-line quoted scalar: {text!r}")
    return text.rstrip()


def _split_flow(body: str) -> list[str]:
    parts, buf, quote = [], "", ""
    for ch in body:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            parts.append(buf.strip())
            buf = ""
        elif ch in "[]{}":
            raise UnsupportedYaml(f"nested flow collection: {body!r}")
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _scalar(token: str) -> Any:
    token = token.strip()
    if token.startswith('"'):
        if not token.endswith('"') or len(token) < 2:
            raise UnsupportedYaml(f"bad double-quoted scalar: {token!r}")
        try:
            return json.loads(token)
        except json.JSONDecodeError as err:
            raise UnsupportedYaml(f"YAML-only escape in {token!r}") from err
    if token.startswith("'"):
        if not token.endswith("'") or len(token) < 2:
            raise UnsupportedYaml(f"bad single-quoted scalar: {token!r}")
        return token[1:-1].replace("''", "'")
    if token.startswith("["):
        if not token.endswith("]"):
            raise UnsupportedYaml(f"multi-line flow sequence: {token!r}")
        return [_scalar(p) for p in _split_flow(token[1:-1])]
    if token == "{}":
        return {}
    if token in _NULLS:
        return None
    if token in _BOOLS:
        return _BOOLS[token]
    if _INT.fullmatch(token):
        return int(token)
    if _FLOAT.fullmatch(token):
        return float(token)
    if _RISKY_PLAIN.search(token) or ": " in token or token.endswith(":") or " #" in token:
        raise UnsupportedYaml(f"plain scalar this parser will not guess: {token!r}")
    return token


def _split_key(content: str) -> tuple[str, str]:
    if content[0] in "\"'":
        end = content.index(content[0], 1)
        key, rest = _scalar(content[: end + 1]), content[end + 1 :]
        if not rest.startswith(":"):
            raise UnsupportedYaml(f"expected ':' after quoted key: {content!r}")
        return str(key), rest[1:].strip()
    m = re.match(r"([^\s:#][^:#]*?)\s*:(\s+|$)", content)
    if not m:
        raise UnsupportedYaml(f"not a mapping entry: {content!r}")
    key = m.group(1)
    if key in _BOOLS or key in _NULLS or _INT.fullmatch(key):
        raise UnsupportedYaml(f"non-string key: {key!r}")
    return key, content[m.end() :].strip()


def mini_yaml(text: str) -> Any:
    """Parse block mappings/sequences of scalars, flow lists, quoted strings."""
    lines: list[tuple[int, str]] = []
    for n, raw in enumerate(text.split("\n"), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise UnsupportedYaml(f"line {n}: tab indentation")
        body = _strip_comment(raw)
        if body.strip():
            if body.strip() in ("---", "...") or body.lstrip()[:1] in "|>":
                raise UnsupportedYaml(f"line {n}: document markers / block scalars")
            lines.append((len(body) - len(body.lstrip()), body.strip()))

    def block(i: int, indent: int) -> tuple[Any, int]:
        if lines[i][1].startswith("- ") or lines[i][1] == "-":
            return seq(i, indent)
        return mapping(i, indent)

    def seq(i: int, indent: int) -> tuple[list[Any], int]:
        out: list[Any] = []
        while i < len(lines) and lines[i][0] == indent and (lines[i][1].startswith("- ") or lines[i][1] == "-"):
            item = lines[i][1][2:].strip()
            if not item or re.match(r"[^\"'\[]*:(\s|$)", item):
                raise UnsupportedYaml(f"nested block in sequence item: {lines[i][1]!r}")
            out.append(_scalar(item))
            i += 1
        return out, i

    def mapping(i: int, indent: int) -> tuple[dict[str, Any], int]:
        out: dict[str, Any] = {}
        while i < len(lines) and lines[i][0] == indent and not lines[i][1].startswith("- "):
            key, rest = _split_key(lines[i][1])
            if key in out:
                raise UnsupportedYaml(f"duplicate key {key!r}")
            i += 1
            nxt = lines[i] if i < len(lines) else None
            if rest:
                if nxt and nxt[0] > indent:
                    raise UnsupportedYaml(f"multi-line value for {key!r}")
                out[key] = _scalar(rest)
            elif nxt and (nxt[0] > indent or (nxt[0] == indent and nxt[1].startswith("- "))):
                out[key], i = block(i, nxt[0])
            else:
                out[key] = None
        if i < len(lines) and lines[i][0] > indent:
            raise UnsupportedYaml(f"bad indentation at {lines[i][1]!r}")
        return out, i

    if not lines:
        return None
    value, i = block(0, lines[0][0])
    if i != len(lines):
        raise UnsupportedYaml(f"unparsed content at {lines[i][1]!r}")
    return value


def load_yaml(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return pyyaml.safe_load(text) if pyyaml else mini_yaml(text)


# ===================================================================== fixtures
@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    return load_yaml(CONFIG)


@pytest.fixture(scope="module")
def translations() -> dict[str, Any]:
    return load_yaml(TRANSLATIONS)


def _option_fields() -> list[tuple[str, Any, Any]]:
    """The Options fields the Home Assistant form exposes (all but DEV_ONLY_OPTIONS)."""
    hints = get_type_hints(Options)
    out = []
    for f in fields(Options):
        if f.name in DEV_ONLY_OPTIONS:
            continue
        default = f.default if f.default is not MISSING else f.default_factory()  # type: ignore[misc]
        out.append((f.name, hints[f.name], default))
    return out


# ================================================================== mini parser
@pytest.mark.parametrize("path", [CONFIG, TRANSLATIONS, REPOSITORY], ids=lambda p: p.name)
def test_mini_yaml_matches_pyyaml(path: Path) -> None:
    parsed = mini_yaml(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    if pyyaml is None:
        pytest.skip("PyYAML not installed; mini parser used alone")
    assert parsed == pyyaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "text",
    ["a: |\n  x\n", "a: &x 1\n", "a: 1:20\n", "a: 010\n", "a:\n  - b: 1\n", "a: b: c\n", "a: [1, [2]]\n", "a: 1\na: 2\n"],
)
def test_mini_yaml_refuses_what_it_does_not_implement(text: str) -> None:
    with pytest.raises(UnsupportedYaml):
        mini_yaml(text)


def test_mini_yaml_basics() -> None:
    doc = 'a: "x # y"\nb: [1, \'two\', "3"]\nc:\n  - str?\n  - 2.5\nd: ~\ne: yes\n"8765/tcp": 8765 # c\nf: []\n'
    assert mini_yaml(doc) == {
        "a": "x # y",
        "b": [1, "two", "3"],
        "c": ["str?", 2.5],
        "d": None,
        "e": True,
        "8765/tcp": 8765,
        "f": [],
    }


# ==================================================================== config.yaml
def test_required_keys_and_identity(config: dict[str, Any]) -> None:
    for key in ("name", "version", "slug", "description", "arch"):
        assert config.get(key), f"config.yaml lacks {key}"
    assert config["name"] == "Send to X3"
    assert config["slug"] == APP.name == "send_to_x3"
    assert isinstance(config["version"], str), "quote the version (it is also the image tag)"
    assert config["version"] == sendtox3.__version__
    assert sorted(config["arch"]) == ["aarch64", "amd64"]
    assert config["init"] is False, "the HA base image runs s6-overlay as PID 1"
    assert config["startup"] == "application" and config["boot"] == "auto"
    assert "image" not in config, "image: stays commented out so local installs build"
    assert "ghcr.io/REPLACE_OWNER/send-to-x3" in CONFIG.read_text(encoding="utf-8")


def test_changelog_has_current_version(config: dict[str, Any]) -> None:
    text = (APP / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(config['version'])}\]", text, re.M)


def test_options_and_schema_mirror_options_dataclass(config: dict[str, Any]) -> None:
    names = [name for name, _, _ in _option_fields()]
    assert list(config["options"]) == names, "options must list every Options field, in order"
    assert list(config["schema"]) == names, "schema must list every Options field, in order"


def test_dev_only_options_stay_out_of_the_form(config: dict[str, Any], translations: dict[str, Any]) -> None:
    all_fields = {f.name for f in fields(Options)}
    assert DEV_ONLY_OPTIONS <= all_fields, "a dev-only option must still be an Options field"
    docs = (APP / "DOCS.md").read_text(encoding="utf-8")
    for name in DEV_ONLY_OPTIONS:
        assert name not in config["options"] and name not in config["schema"], name
        assert name not in translations["configuration"], name
        assert f"| `{name}` |" not in docs, f"DOCS.md documents {name} as an option"


def test_dev_only_port_still_works_outside_home_assistant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENDTOX3_PORT", "9001")
    assert load_options(tmp_path / "missing-options.json").port == 9001
    monkeypatch.delenv("SENDTOX3_PORT")
    assert load_options(tmp_path / "missing-options.json").port == DEFAULT_PORT


@pytest.mark.parametrize("name,typ,default", _option_fields(), ids=[n for n, _, _ in _option_fields()])
def test_option_default_and_type(config: dict[str, Any], name: str, typ: Any, default: Any) -> None:
    value = config["options"][name]
    schema = config["schema"][name]
    assert value == default and type(value) is type(default), f"{name}: {value!r} != Options default {default!r}"

    if typ is bool:
        assert schema == "bool"
    elif typ is int:
        if name in PORT_OPTIONS:
            assert schema == "port"
        else:
            m = re.fullmatch(r"int\((-?\d+)?,(-?\d+)?\)", schema)
            assert m, f"{name}: ints need an explicit range, got {schema!r}"
            lo, hi = (int(g) if g else None for g in m.groups())
            assert (lo is None or lo <= default) and (hi is None or default <= hi)
    elif typ is str:
        if name in SECRET_OPTIONS:
            assert schema == "password?", f"{name} is a secret: use password? (hidden, may stay empty)"
        elif schema.startswith("list("):
            assert default in schema[5:-1].split("|"), f"{name}: default not among the choices"
        elif default == "":
            # Empty means "unset/auto"; only str/password accept "" (email/url do not).
            assert schema == "str?", f"{name}: empty default needs str?, got {schema!r}"
        else:
            assert schema == "str", f"{name}: {schema!r}"
    elif getattr(typ, "__origin__", None) is list:
        assert schema == ["str?"], f"{name}: lists of strings use [str?] so an empty list is valid"
    else:  # pragma: no cover - a new Options type needs a mapping here
        pytest.fail(f"{name}: no schema rule for type {typ!r}")


def test_enum_choices_match_code(config: dict[str, Any]) -> None:
    assert config["schema"]["delivery_format"] == "list(epub|txt)"  # load_options accepts exactly these
    levels = config["schema"]["log_level"][5:-1].split("|")
    assert set(levels) <= {"debug", "info", "warning", "error", "critical"}


def test_network_ingress_and_watchdog(config: dict[str, Any]) -> None:
    port_key = f"{DEFAULT_PORT}/tcp"
    assert config["ingress"] is True
    assert config["ingress_port"] == DEFAULT_PORT, "one aiohttp server serves ingress and the LAN"
    assert config["ports"] == {port_key: DEFAULT_PORT}
    assert port_key in config["ports_description"]
    assert config["watchdog"] == f"http://[HOST]:[PORT:{DEFAULT_PORT}]/healthz"
    assert "webui" not in config, "ingress already provides Open Web UI"
    assert config["panel_icon"].startswith("mdi:") and config["panel_title"] == "Send to X3"


def test_privileges_are_minimal(config: dict[str, Any]) -> None:
    assert config["homeassistant_api"] is True  # mobile_app.share over the Core websocket
    assert config["hassio_api"] is True and config["hassio_role"] == "default"  # /network/info
    for key in ("host_network", "full_access", "docker_api", "privileged", "devices", "auth_api", "map"):
        assert not config.get(key), f"{key} is not needed"
    assert config.get("apparmor", True) is True


def test_backup_settings(config: dict[str, Any]) -> None:
    # The SQLite database runs in WAL mode; only a stopped app gives a consistent copy
    # of sendtox3.db + sendtox3.db-wal (and of rows vs item files).
    assert config["backup"] == "cold"
    assert "backup_pre" not in config and "backup_post" not in config, "ignored by cold backups"
    for pattern in config["backup_exclude"]:
        assert not pattern.startswith("/"), "patterns are matched from the right (PurePath.match)"
        assert "sendtox3.db" != pattern and not pattern.endswith("-wal"), "the WAL holds committed data"


# ================================================================== translations
def test_translations_cover_every_option_and_port(config: dict[str, Any], translations: dict[str, Any]) -> None:
    conf = translations["configuration"]
    assert list(conf) == list(config["schema"]), "one entry per option, in schema order"
    for name, entry in conf.items():
        assert set(entry) <= {"name", "description", "fields"}, name
        assert entry["name"].strip(), f"{name}: empty name"
        assert entry["description"].strip(), f"{name}: empty description"
        assert len(entry["name"]) <= 40, f"{name}: keep labels short"
    assert set(translations["network"]) == set(config["ports"])


def test_device_token_characters_are_documented(translations: dict[str, Any]) -> None:
    """The schema stays ``password?`` (a ``match()`` field would show the token in clear
    text), so the form and DOCS.md must say which characters the X3 plugin accepts."""
    from sendtox3.plugin_bundle import device_token_problem

    forbidden = ["{", "}", '"', "\\"]
    for ch in [*forbidden, " "]:
        assert device_token_problem(f"abc{ch}def"), f"{ch!r} is accepted by the plugin now; update the docs"
    assert not device_token_problem("Abc-123_.~!#$%&'()*+,/:;<=>?@[]^`|")
    docs = (APP / "DOCS.md").read_text(encoding="utf-8")
    row = next(line for line in docs.splitlines() if line.startswith("| `device_token` |"))
    for text in (translations["configuration"]["device_token"]["description"], row):
        assert "no spaces" in text.lower(), text
        for ch in forbidden:
            assert ch in text, f"{ch!r} not mentioned in {text!r}"


def test_docs_cover_every_option(config: dict[str, Any]) -> None:
    docs = (APP / "DOCS.md").read_text(encoding="utf-8")
    missing = [name for name in config["schema"] if f"`{name}`" not in docs]
    assert not missing, f"DOCS.md does not document: {missing}"


def test_repository_manifest() -> None:
    repo = load_yaml(REPOSITORY)
    assert repo["name"]
    assert set(repo) <= {"name", "url", "maintainer"}


def test_exactly_one_app_in_repository() -> None:
    """The Supervisor treats every config.{json,yaml,yml} outside dot-folders/rootfs as an app."""
    skip = {"__pycache__", "node_modules"}
    found = []
    for path in REPO.rglob("config.*"):
        rel = path.relative_to(REPO)
        if path.suffix not in (".json", ".yaml", ".yml"):
            continue
        if any(p.startswith(".") or p == "rootfs" or p in skip for p in rel.parts):
            continue
        if rel.parts[:2] == ("e2e", "work") or rel.parts[0] in ("build", "dist"):
            continue  # gitignored, never reaches the Supervisor's clone
        found.append(rel.as_posix())
    assert found == ["send_to_x3/config.yaml"], f"rename these, the Supervisor would load them as apps: {found}"


# ================================================================== image files
def _parse_dockerfile() -> list[tuple[str, str]]:
    joined = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    out = []
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            instr, _, args = line.partition(" ")
            out.append((instr.upper(), args.strip()))
    return out


def test_dockerfile_base_and_entrypoint() -> None:
    instrs = _parse_dockerfile()
    assert instrs[0] == ("ARG", f"BUILD_FROM={BASE_IMAGE}"), "Supervisor 2026.04+ no longer passes BUILD_FROM"
    assert instrs[1] == ("FROM", "${BUILD_FROM}")
    assert not [a for i, a in instrs if i == "ENTRYPOINT"], "s6-overlay's /init must stay the entrypoint"
    assert instrs[-1] == ("CMD", '["/run.sh"]')
    labels = " ".join(a for i, a in instrs if i == "LABEL")
    assert 'io.hass.type="app"' in labels


def test_dockerfile_copies_existing_files_only() -> None:
    copies = [a.split() for i, a in _parse_dockerfile() if i in ("COPY", "ADD")]
    assert copies, "no COPY instructions"
    for parts in copies:
        assert not any(p.startswith("--from") for p in parts)
        *sources, _dest = [p for p in parts if not p.startswith("--")]
        for src in sources:
            assert (APP / src).exists(), f"COPY {src}: not in the build context send_to_x3/"
    copied = {p[0] for p in copies}
    assert {"requirements.txt", "sendtox3", "run.sh"} <= copied


def test_dockerfile_installs_pinned_wheels_only() -> None:
    runs = " ".join(a for i, a in _parse_dockerfile() if i == "RUN")
    assert "-r /tmp/requirements.txt" in runs
    assert "--only-binary=:all:" in runs, "fail fast instead of compiling on a Pi"
    assert "--no-cache-dir" in runs
    assert "tesseract-ocr" in runs and "tesseract-ocr-data-eng" in runs, "tesseract is needed for pdf_ocr"
    for line in (APP / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            assert re.fullmatch(r"[A-Za-z0-9_.\-\[\],]+==[0-9][A-Za-z0-9_.!+\-]*", line), f"unpinned: {line}"


def test_run_sh_is_executable_lf_bashio() -> None:
    data = RUN_SH.read_bytes()
    assert b"\r" not in data, "run.sh must use LF line endings"
    assert data.endswith(b"\n")
    text = data.decode("utf-8")
    assert text.startswith("#!/command/with-contenv bashio\n"), "with-contenv exposes SUPERVISOR_TOKEN"
    assert RUN_SH.stat().st_mode & 0o111, "run.sh must be executable (git: update-index --chmod=+x)"
    code = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert code[-1] == "exec python3 -u -m sendtox3", "exec so the server is the process s6 watches"
    assert "bashio::config" not in text, "options come from /data/options.json, also without a Supervisor"


@pytest.mark.parametrize(
    "path",
    [CONFIG, TRANSLATIONS, REPOSITORY, DOCKERFILE, RUN_SH, APP / "DOCS.md", APP / "README.md", APP / "CHANGELOG.md"],
    ids=lambda p: p.name,
)
def test_text_files_are_clean_utf8(path: Path) -> None:
    data = path.read_bytes()
    data.decode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf"), "no BOM"
    assert b"\r" not in data, "LF only"
    assert data.endswith(b"\n") and not data.endswith(b"\n\n"), "exactly one trailing newline"
    assert b"\t" not in data or path.name == "Dockerfile", "no tabs"


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_icon_and_logo() -> None:
    assert _png_size(APP / "icon.png") == (128, 128)
    w, h = _png_size(APP / "logo.png")
    assert 200 <= w <= 300 and 80 <= h <= 120, (w, h)
    for name in ("icon.png", "logo.png"):
        assert (APP / name).stat().st_size < 64_000


# ===================================================================== workflows
def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def test_workflows_exist() -> None:
    names = {p.name for p in _workflow_files()}
    assert {"tests.yml", "build.yml"} <= names


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_workflow_actions_are_pinned(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "\r" not in text and "\t" not in text
    uses = re.findall(r"uses:\s*([^\s#]+)", text)
    assert uses
    for ref in uses:
        assert "@" in ref, f"{ref}: pin a version"
        name, version = ref.rsplit("@", 1)
        assert version not in ("main", "master", "latest"), f"{ref}: pin a release"
        if name.startswith("home-assistant/builder/"):
            assert version == BUILDER_VERSION, ref
    if pyyaml is not None:
        doc = pyyaml.safe_load(text)
        assert doc["jobs"], path.name
        assert "permissions" in doc, f"{path.name}: set least-privilege permissions"


def _runtime_requirements() -> set[str]:
    """Import names of send_to_x3/requirements.txt (the distribution name, normalised)."""
    names = set()
    for line in (APP / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(re.split(r"[=<>!~\[;\s]", line, maxsplit=1)[0].lower().replace("-", "_"))
    return names


def test_image_smoke_tests_import_exactly_the_runtime_requirements() -> None:
    """build.yml once imported pypdf (a dev-only dependency) and ran pdftotext (not in
    the image), so every pull-request build failed its smoke test."""
    runtime = _runtime_requirements()
    for path in (WORKFLOWS / "build.yml", REPO / "tests" / "test_packaging_docker.py"):
        m = re.search(r"import (sendtox3(?:,\s*\w+)+)", path.read_text(encoding="utf-8"))
        assert m, f"{path.name}: no smoke-test import line"
        modules = {name.strip() for name in m.group(1).split(",")} - {"sendtox3"}
        assert modules == runtime, f"{path.name}: imports {sorted(modules)}, requirements.txt has {sorted(runtime)}"


def test_build_workflow_smoke_test_runs_only_shipped_programs() -> None:
    text = (WORKFLOWS / "build.yml").read_text(encoding="utf-8")
    programs = re.findall(r'docker run --rm "\$\{IMAGE\}" (\S+)', text)
    assert programs and set(programs) <= {"python3", "sh"}, programs
    commands = {
        seg.split()[0]
        for script in re.findall(r"sh -c '([^']*)'", text)
        for seg in re.split(r"&&|\|\||\||;", script)
        if seg.strip()
    }
    # tesseract-ocr(-data-eng) is installed by the Dockerfile; grep is busybox.
    assert commands == {"tesseract", "grep"}, commands
    assert "tesseract --list-langs | grep -qx eng" in text


def test_build_workflow_targets_lowercase_ghcr_image() -> None:
    text = (WORKFLOWS / "build.yml").read_text(encoding="utf-8")
    assert "send-to-x3" in text
    assert ",," in text or "tr '[:upper:]' '[:lower:]'" in text, "GHCR image names must be lowercase"
    for action in ("prepare-multi-arch-matrix", "build-image", "publish-multi-arch-manifest"):
        assert f"home-assistant/builder/actions/{action}@{BUILDER_VERSION}" in text


# ================================================================ developer scripts
_GATED_MARKERS = {"docker": "SENDTOX3_DOCKER_TESTS", "network": "SENDTOX3_NETWORK_TESTS"}


def test_check_all_docker_stage_enables_what_docker_tests_need() -> None:
    """Stage 2 runs ``-m docker``; a docker test that is also network-marked (the
    Android import check) would be skipped unless network tests are enabled too."""
    text = CHECK_ALL.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if re.search(r"pytest .*-m docker\b", ln)]
    assert len(lines) == 1, lines
    needed = {"docker"}
    for path in (REPO / "tests").glob("test_*.py"):
        src = path.read_text(encoding="utf-8")
        if "pytest.mark.docker" in src:
            needed |= {m for m in _GATED_MARKERS if f"pytest.mark.{m}" in src}
    assert "network" in needed, "the Android import test is docker + network"
    for marker in needed:
        assert f"{_GATED_MARKERS[marker]}=1" in lines[0], f"stage 2 skips {marker}-marked docker tests"


def test_check_all_points_to_existing_docs() -> None:
    refs = re.findall(r"docs/[\w./-]+\.md", CHECK_ALL.read_text(encoding="utf-8"))
    assert "docs/TESTING.md" in refs
    for ref in refs:
        assert (REPO / ref).is_file(), f"check_all.sh refers to missing {ref}"
