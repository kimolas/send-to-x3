#!/usr/bin/env python3
"""Check an HTTP Shortcuts import file with the app's own import code, in Docker.

HTTP Shortcuts is an Android app, but its import pipeline (ImportMigrator, Gson model
classes, ImportExportDefaultsProvider, validate()) and its placeholder code
(variables/Variables.kt) are plain Kotlin. This copies those files unchanged from the
app's source at a release tag, adds the few Android stand-ins in
``http_shortcuts_check/stubs``, compiles everything with the Kotlin compiler in a JRE
container (no network inside the container) and runs ``http_shortcuts_check/Main.kt``.

Needs: git, docker, and network access on the host (GitHub, Maven Central) the first
time; downloads are cached and checked against SHA-256 sums.

    python3 shortcuts/tools/http_shortcuts_check.py shortcuts/android/http-shortcuts-send-to-x3.json
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Optional, Sequence

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "http_shortcuts_check"

UPSTREAM = "https://github.com/Waboodoo/HTTP-Shortcuts"
TAG = "v4.9.0"
IMAGE = "eclipse-temurin:21-jre"

#: Kotlin version the app is built with (gradle/libs.versions.toml), and its runtime deps.
DOWNLOADS: dict[str, tuple[str, str]] = {
    "kotlin-compiler-2.4.21.zip": (
        "https://github.com/JetBrains/kotlin/releases/download/v2.4.21/kotlin-compiler-2.4.21.zip",
        "7cc140e76daf416a0424a5557f99afd7ca89ebd463ea1101261e3e01c33e75cd"),
    "gson-2.14.0.jar": (
        "https://repo1.maven.org/maven2/com/google/code/gson/gson/2.14.0/gson-2.14.0.jar",
        "2cbd119bf1961c28788310963dc80ba65f58cdeec1dd139c8bdb1240faa2c36f"),
    "json-20250517.jar": (
        "https://repo1.maven.org/maven2/org/json/json/20250517/json-20250517.jar",
        "3ea61b2a06e31edf1c91134fe9106b0ebb16628be169f3db75bc7a2b06b45796"),
    "javax.inject-1.jar": (
        "https://repo1.maven.org/maven2/javax/inject/javax.inject/1/javax.inject-1.jar",
        "91c77044a50c481636c32d916fd89c9118a72195390452c81065080f957de7ff"),
}

_APP = "HTTPShortcuts/app/src/main/kotlin/ch/rmy/android/http_shortcuts"
#: Upstream files compiled as they are (globs relative to the repository root).
UPSTREAM_FILES: tuple[str, ...] = (
    f"{_APP}/import_export/models/*.kt",
    f"{_APP}/import_export/migration/*.kt",
    f"{_APP}/import_export/ImportMigrator.kt",
    f"{_APP}/import_export/MigrationExtensions.kt",
    f"{_APP}/import_export/ImportExportDefaultsProvider.kt",
    f"{_APP}/import_export/ImportExport.kt",
    f"{_APP}/import_export/InvalidFileException.kt",
    f"{_APP}/import_export/ImportVersionMismatchException.kt",
    *(f"{_APP}/data/enums/{name}.kt" for name in (
        "CategoryAlignment", "CategoryBackgroundType", "CategoryLayoutType", "HttpMethod", "ParameterType",
        "RequestBodyType", "ResponseFailureOutput", "ResponseSuccessOutput", "ResponseUiType",
        "ShortcutExecutionType", "VariableType")),
    *(f"{_APP}/data/domains/{path}.kt" for path in (
        "categories/CategoryId", "sections/SectionId", "shortcuts/ShortcutId", "variables/GlobalVariableId",
        "variables/VariableKey", "variables/VariableKeyOrId", "working_directories/WorkingDirectoryId",
        "certificate_pins/CertificatePinId")),
    f"{_APP}/variables/Variables.kt",
    f"{_APP}/variables/ResolvedVariableValues.kt",
    f"{_APP}/extensions/StringExtensions.kt",
    "HTTPShortcuts/framework/src/main/kotlin/ch/rmy/android/framework/utils/UUIDUtils.kt",
)


class CheckError(RuntimeError):
    pass


def default_cache() -> Path:
    return Path(os.environ.get("SENDTOX3_HTTP_SHORTCUTS_CACHE")
                or Path(tempfile.gettempdir()) / "sendtox3-http-shortcuts-check")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(cache: Path) -> Path:
    """Download (once) the compiler and jars into ``cache``; return the jar folder."""
    dl = cache / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    for name, (url, digest) in DOWNLOADS.items():
        path = dl / name
        if not path.is_file() or _sha256(path) != digest:
            tmp = path.with_suffix(path.suffix + ".part")
            with urllib.request.urlopen(url, timeout=300) as resp, tmp.open("wb") as out:
                shutil.copyfileobj(resp, out)
            if _sha256(tmp) != digest:
                tmp.unlink()
                raise CheckError(f"checksum mismatch for {url}")
            os.replace(tmp, path)
    kotlinc = cache / "kotlinc"
    if not (kotlinc / "lib" / "kotlin-compiler.jar").is_file():
        with zipfile.ZipFile(dl / "kotlin-compiler-2.4.21.zip") as z:
            z.extractall(cache)
    return dl


def upstream_source(cache: Path) -> Path:
    """HTTP Shortcuts at ``TAG`` (or ``$HTTP_SHORTCUTS_SRC`` if set)."""
    if os.environ.get("HTTP_SHORTCUTS_SRC"):
        return Path(os.environ["HTTP_SHORTCUTS_SRC"])
    repo = cache / f"HTTP-Shortcuts-{TAG}"
    if not (repo / _APP).is_dir():
        shutil.rmtree(repo, ignore_errors=True)
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", TAG, UPSTREAM, str(repo)],
                       check=True, timeout=600)
    return repo


def check_import(import_file: Path, cases: Sequence[dict[str, Any]] = (), *, cache: Optional[Path] = None
                 ) -> dict[str, Any]:
    """Run the app's import pipeline on ``import_file`` and render ``cases``
    (``{"text", "title", "values": {key: value}}``). Raises CheckError with the app's
    validation message when the import would be rejected."""
    cache = cache or default_cache()
    fetch(cache)
    repo = upstream_source(cache)
    with tempfile.TemporaryDirectory(prefix="hs-check-") as tmp:
        work = Path(tmp)
        src = work / "src"
        for pattern in UPSTREAM_FILES:
            matches = sorted(repo.glob(pattern))
            if not matches:
                raise CheckError(f"upstream file missing: {pattern}")
            for f in matches:
                dest = src / f.relative_to(repo)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
        shutil.copytree(HARNESS, src / "harness")
        shutil.copy2(import_file, work / "import.json")
        (work / "cases.json").write_text(json.dumps(list(cases)), encoding="utf-8")
        cp = ":".join(f"/c/downloads/{n}" for n in DOWNLOADS if n.endswith(".jar"))
        script = (f"bash /c/kotlinc/bin/kotlinc -nowarn -cp {cp} -d /w/check.jar /w/src 2>/w/compile.log"
                  f" || {{ cat /w/compile.log >&2; exit 3; }}; "
                  f"java -cp /w/check.jar:/c/kotlinc/lib/kotlin-stdlib.jar:{cp} sendtox3check.MainKt"
                  f" /w/import.json /w/cases.json")
        proc = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{work}:/w",
                               "-v", f"{cache}:/c:ro", IMAGE, "bash", "-c", script],
                              capture_output=True, text=True, timeout=900, check=False)
        if proc.returncode != 0:
            raise CheckError(f"exit {proc.returncode}: {proc.stderr[-4000:]}")
        return json.loads(proc.stdout)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: http_shortcuts_check.py <import.json>", file=sys.stderr)
        return 2
    try:
        print(json.dumps(check_import(Path(args[0]), [{"text": "https://example.com/a", "title": "T"}]), indent=2))
    except CheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
