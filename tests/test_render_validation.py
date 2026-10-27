"""External validation of the renderers and titles.

* ``@pytest.mark.docker``: epubcheck 5.4.0 (``e2e/epubcheck``) over a generated corpus with
  ``--failonwarnings``: every book must report 0 fatals / errors / warnings.
  Run with ``SENDTOX3_DOCKER_TESTS=1``.
* firmware: CrossPoint's real EPUB/TXT/file-name code built on the host by
  ``e2e/fwharness/build.sh``. Skipped unless ``SENDTOX3_FIRMWARE_TESTS=1`` and ``CP_REPO``
  is set (several checkouts may be given, separated by ``os.pathsep``, e.g. 1.6.5 and
  2.0.0rc). Optional: ``CP_SDK`` (freeink-sdk for 2.x), ``FWH_WORK`` (build dir outside
  the repo, default ``$TMPDIR/sendtox3-fwharness``).

All corpus text is synthetic (written for these tests).
"""

from __future__ import annotations

import os
import random
import re
import subprocess
import sys

from lxml import etree
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from sendtox3.render.epub import DIGEST_CREATOR, render_epub
from sendtox3.render.names import (
    fw_plugin_filename_ok,
    fw_sanitize_filename,
    fw_sanitize_preserving_extension,
    safe_title,
)
from sendtox3.render.txt import render_txt
from sendtox3.textmodel import Document, Heading, ListItem, Paragraph, Preformatted, Quote, Rule

REPO = Path(__file__).resolve().parents[1]
EPUBCHECK_DIR = REPO / "e2e" / "epubcheck"
FWHARNESS = REPO / "e2e" / "fwharness" / "build.sh"
MODIFIED = 1_761_400_000  # fixed delivery time for digests

sys.path.insert(0, str(Path(__file__).parent))
from test_names import ALL_TITLES, TRICKY  # noqa: E402  (shared title corpus)


# --------------------------------------------------------------------------------------
# Corpus (synthetic text)
# --------------------------------------------------------------------------------------

_WORDS = (
    "the screen holds an image without power so reading feels calm and the battery lasts for weeks "
    "while tiny capsules of pigment move under an electric field to form each letter on the page"
).split()


def _sentences(rnd: random.Random, n: int) -> str:
    out = []
    for _ in range(n):
        words = [rnd.choice(_WORDS) for _ in range(rnd.randint(6, 18))]
        words[0] = words[0].capitalize()
        out.append(" ".join(words) + rnd.choice([".", ".", ".", "?", "!"]))
    return " ".join(out)


def _article(seed: int, paragraphs: int, *, title: str, **kw) -> Document:
    rnd = random.Random(seed)
    blocks = []
    for i in range(paragraphs):
        if i and i % 7 == 0:
            blocks.append(Heading(f"Part {i // 7}: what changes next", 2 if i % 14 else 3))
        blocks.append(Paragraph(_sentences(rnd, rnd.randint(2, 7))))
    base = dict(
        title=title,
        byline="A. Writer",
        site="Example Daily",
        source_url=f"https://www.example.com/{seed}/a-long-path/with-parts?utm_x=1&id={seed}",
        published="2026-10-24",
        lang="en",
        blocks=blocks,
    )
    base.update(kw)
    return Document(**base)


@dataclass
class Case:
    name: str
    docs: list[Document]
    title: str  # expected dc:title / library title
    author: str  # expected dc:creator / library author
    kwargs: dict


def corpus() -> list[Case]:
    single = _article(1, 12, title="How E-Ink Displays Hold an Image")
    digest = [
        _article(2, 4, title="First of three: morning notes"),
        _article(3, 5, title="Second \u2014 Caf\u00e9 cr\u00e8me & \u00e9clairs", lang="fr", site="Exemple"),
        Document(
            title="Third: a note without a link",
            blocks=[Paragraph("Remember to charge the reader."), Paragraph("And to back up the SD card.")],
        ),
    ]
    long_doc = _article(4, 900, title="A Very Long Read About Displays", byline="", site="")
    unicode_doc = Document(
        title="Unicode \u2014 \u00ab\u00e9t\u00e9\u00bb, \u0141\u00f3d\u017a, \u0110\u1ea1i, \u041f\u0440\u0438\u0432\u0435\u0442",
        byline="Zo\u00eb \u00c6lfgifu \u0160\u0107",
        site="b\u00fccher.example",
        source_url="https://xn--bcher-kva.example/\u00e9t\u00e9?q=\u00fc",
        published="2026-01-05",
        lang="pl",
        blocks=[
            Paragraph("Typographic \u201cquotes\u201d, \u2018single\u2019, en\u2013dash, em\u2014dash, ellipsis\u2026 and NBSP\u00a0here."),
            Paragraph("Combining: e\u0301 a\u0300 o\u0302 \u2014 Vietnamese: Ti\u1ebfng Vi\u1ec7t c\u00f3 d\u1ea5u."),
            Paragraph("Cyrillic: \u0421\u044a\u0435\u0448\u044c \u0436\u0435 \u0435\u0449\u0451 \u044d\u0442\u0438\u0445 \u043c\u044f\u0433\u043a\u0438\u0445 \u0431\u0443\u043b\u043e\u043a."),
            Paragraph("Not in the font (boxes): \u65e5\u672c\u8a9e, \u05e2\u05d1\u05e8\u05d9\u05ea, \u0395\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03ac; emoji removed: \U0001f600\U0001f44d\u2764\ufe0f."),
            Paragraph("XML specials: 5 < 6 && 7 > 3, \"double\" and 'single', &amp; literally, <p>not a tag</p>."),
            Paragraph("Symbols: \u2122 \u2192 \u2264 \u2265 \u221e \u2713 \u2605 \u20ac \u00a3 \u00a5 \u00b0 \u00b1 \u00d7 \u00f7 \u00bd."),
            Paragraph("Zero\u200bwidth, soft\u00adhyphen, BOM\ufeff, bidi\u202eoverride\u202c and controls\x07\x1b removed."),
        ],
    )
    lists_doc = Document(
        title="Lists of every shape",
        source_url="https://lists.example/shapes",
        blocks=[
            Paragraph("Bullets with nesting:"),
            ListItem("Fruit"),
            ListItem("Apples", depth=1),
            ListItem("Granny Smith", depth=2, ordered=True, index=1),
            ListItem("Pink Lady", depth=2, ordered=True, index=2),
            ListItem("Pears", depth=1),
            ListItem("Vegetables"),
            ListItem("A level jump straight to depth three", depth=3),
            Paragraph("Numbered, starting at four:"),
            ListItem("Four", ordered=True, index=4),
            ListItem("Five", ordered=True, index=5),
            ListItem("Then bullets right after", ordered=False),
            ListItem(_sentences(random.Random(9), 30), ordered=True, index=1),
        ],
    )
    quotes_doc = Document(
        title="Quotes and rules",
        byline="Q. Author",
        blocks=[
            Paragraph("Before the quote."),
            Quote("A quoted paragraph, attributed to nobody in particular."),
            Quote("A second paragraph of the same quotation."),
            Rule(),
            ListItem("A list between quotes"),
            Quote("Another quote after a list."),
            Rule(),
            Rule(),
            Paragraph("After the rules."),
        ],
    )
    pre_doc = Document(
        title="Preformatted code",
        source_url="https://code.example/snippet",
        blocks=[
            Paragraph("A function:"),
            Preformatted("def area(r):\n    \"\"\"Circle area.\"\"\"\n    return 3.14159 * r * r\n\n\nif x < 1 and y > 2:\n\tprint('<tab> & done')"),
            Preformatted("    leading indent only\n  x  =  [1,  2,   3]\n" + "long_identifier_" * 40),
            Paragraph("Done."),
        ],
    )
    notes_doc = Document(
        title="Paywalled article (preview only)",
        site="Paper.example",
        source_url="https://paper.example/story",
        notes=["Paywalled: only the preview was available", "Fetched with a 25 s budget"],
        blocks=[],
    )
    weird_title = Document(
        title="Wait... what? {url} <b>bold</b> " + "x" * 700,
        blocks=[Heading("Heading level one in the body", 1), Paragraph("Body.")],
    )
    german = _article(5, 20, title="Silbentrennung f\u00fcr deutsche Texte", lang="de-DE", site="Beispiel")
    return [
        Case("single", [single], single.title, "A. Writer", {}),
        Case(
            "digest3",
            digest,
            "Inbox: 3 items (" + time.strftime("%Y-%m-%d %H:%M", time.localtime(MODIFIED)) + ")",
            DIGEST_CREATOR,
            {"modified": MODIFIED},
        ),
        Case("long", [long_doc], long_doc.title, "example.com", {}),
        Case("unicode", [unicode_doc], unicode_doc.title, unicode_doc.byline, {}),
        Case("lists", [lists_doc], lists_doc.title, "lists.example", {}),
        Case("quotes", [quotes_doc], quotes_doc.title, "Q. Author", {}),
        Case("pre", [pre_doc], pre_doc.title, "code.example", {}),
        Case("notes", [notes_doc], notes_doc.title, "Paper.example", {}),
        Case("weird_title", [weird_title], "", "", {}),
        Case("german", [german], german.title, "A. Writer", {}),
        Case("digest_override", digest[:2], "Two for the road", DIGEST_CREATOR, {"book_title": "Two for the road"}),
    ]


def write_corpus(dest: Path) -> dict[str, Case]:
    dest.mkdir(parents=True, exist_ok=True)
    cases = {}
    for case in corpus():
        (dest / f"{case.name}.epub").write_bytes(render_epub(case.docs, **case.kwargs))
        cases[case.name] = case
    return cases


def test_corpus_renders():
    """The corpus itself (also used by the docker and firmware tests) always renders."""
    with tempfile.TemporaryDirectory() as d:
        cases = write_corpus(Path(d))
        assert len(cases) == len(corpus())
        for case in cases.values():
            for doc in case.docs:
                assert render_txt(doc).endswith(b"\n")


# --------------------------------------------------------------------------------------
# epubcheck (docker)
# --------------------------------------------------------------------------------------


@pytest.mark.docker
def test_epubcheck_corpus(tmp_path):
    image = os.environ.get("EPUBCHECK_IMAGE", "sendtox3-epubcheck:5.4.0")
    subprocess.run(["docker", "build", "-q", "-t", image, str(EPUBCHECK_DIR)], check=True, capture_output=True, timeout=900)
    out = tmp_path / "corpus"
    cases = write_corpus(out)
    os.chmod(out, 0o755)
    for f in out.iterdir():
        os.chmod(f, 0o644)
    script = (
        'for f in /data/*.epub; do echo "== $(basename "$f")"; '
        'java -jar /opt/epubcheck/epubcheck.jar --failonwarnings "$f" 2>&1; echo "RC $?"; done'
    )
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "-v", f"{out}:/data:ro", "--entrypoint", "/bin/sh", image, "-c", script],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, proc.stderr
    sections = re.split(r"^== (\S+)$", proc.stdout, flags=re.M)[1:]
    results = dict(zip(sections[0::2], sections[1::2]))
    assert sorted(results) == sorted(f"{n}.epub" for n in cases)
    for name, text in results.items():
        assert "RC 0" in text, f"{name}:\n{text}"
        m = re.search(r"Messages: (\d+) fatals? / (\d+) errors? / (\d+) warnings? / (\d+) infos?", text)
        assert m and m.groups()[:3] == ("0", "0", "0"), f"{name}:\n{text}"
        assert "No errors or warnings detected." in text, f"{name}:\n{text}"


# --------------------------------------------------------------------------------------
# CrossPoint firmware harness
# --------------------------------------------------------------------------------------

_FW_ENABLED = os.environ.get("SENDTOX3_FIRMWARE_TESTS") == "1" and bool(os.environ.get("CP_REPO"))
firmware = pytest.mark.skipif(not _FW_ENABLED, reason="set SENDTOX3_FIRMWARE_TESTS=1 and CP_REPO to run")
_REPOS = [p for p in os.environ.get("CP_REPO", "").split(os.pathsep) if p]
TXT_BUDGET_SECONDS = float(os.environ.get("SENDTOX3_TXT_BUDGET", "20"))


@dataclass
class Harness:
    repo: str
    bin: Path
    family: str

    def tool(self, name: str) -> str:
        return str(self.bin / name)


_BUILT: dict[str, Harness] = {}


def _build(repo: str) -> Harness:
    if repo in _BUILT:
        return _BUILT[repo]
    env = dict(os.environ, CP_REPO=repo)
    env.setdefault("FWH_WORK", str(Path(tempfile.gettempdir()) / "sendtox3-fwharness"))
    proc = subprocess.run(["bash", str(FWHARNESS)], env=env, capture_output=True, text=True, timeout=1800)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    bin_dir = Path(re.findall(r"^FWH_BIN=(.+)$", proc.stdout, flags=re.M)[-1])
    family = "1x" if (Path(repo) / "src/activities/reader/TxtReaderActivity.cpp").exists() else "2x"
    _BUILT[repo] = Harness(repo, bin_dir, family)
    return _BUILT[repo]


@pytest.fixture(params=_REPOS or ["<unset>"], ids=lambda p: Path(p).name)
def harness(request) -> Harness:
    if not _FW_ENABLED:
        pytest.skip("set SENDTOX3_FIRMWARE_TESTS=1 and CP_REPO to run")
    return _build(request.param)


def _sd(tmp_path: Path) -> Path:
    sd = tmp_path / "sd"
    (sd / "books").mkdir(parents=True)
    return sd


def _run(h: Harness, tool: str, sd: Path, sd_path: str, timeout: float = 120) -> tuple[int, str, float]:
    env = dict(os.environ, CP_SDROOT=str(sd))
    env.pop("CP_LOG", None)
    t0 = time.monotonic()
    proc = subprocess.run([h.tool(tool), sd_path], env=env, capture_output=True, text=True, timeout=timeout,
                          errors="replace", cwd=sd)
    return proc.returncode, proc.stdout, time.monotonic() - t0


def _field(out: str, line_prefix: str, key: str) -> str:
    m = re.search(rf"^{re.escape(line_prefix)}.*?\b{key}=\[(.*?)\](?= \w+=\[|$)", out, flags=re.M)
    assert m, f"no {line_prefix} {key} in output"
    return m.group(1)


def _page_lines(out: str) -> list[str]:
    """Laid-out text lines. The dump prints " <word>" per word, so one leading space is
    dropped and indentation (NBSP words) is kept."""
    return [m.group(1)[1:].rstrip() for m in re.finditer(r"^\s+y=\s*-?\d+ x=\s*-?\d+ a=\d \|(.*)$", out, flags=re.M)]


def _squash(s: str) -> str:
    """Compare text ignoring word boundaries (2.x emits "E-" "Ink" as two tokens)."""
    return re.sub(r"\s+", "", s)


def _plain(line: str) -> str:
    """Strip the dump's style markers (*bold, italic/)."""
    return " ".join(w.lstrip("*").rstrip("/") for w in line.split())


def _epub_spine_and_toc(path: Path) -> tuple[list[str], list[str]]:
    """Spine hrefs and nav labels of a generated EPUB, in reading order."""
    import zipfile

    with zipfile.ZipFile(path) as z:
        opf = etree.fromstring(z.read("OEBPS/content.opf"))
        nav = etree.fromstring(z.read("OEBPS/nav.xhtml"))
    ns = {"o": "http://www.idpf.org/2007/opf", "h": "http://www.w3.org/1999/xhtml"}
    hrefs = {i.get("id"): i.get("href") for i in opf.findall(".//o:manifest/o:item", ns)}
    spine = [hrefs[r.get("idref")] for r in opf.findall(".//o:spine/o:itemref", ns)]
    labels = [a.text or "" for a in nav.findall(".//h:nav//h:a", ns)]
    return spine, labels


def _first_chapter_indexes(spine: list[str]) -> list[int]:
    """Index of each document's first chapter (``item-NNN.xhtml``; continuations are ``item-NNN-KKK``)."""
    return [i for i, href in enumerate(spine) if re.fullmatch(r"text/item-\d+\.xhtml", href)]


@firmware
def test_firmware_epub_corpus(harness, tmp_path):
    sd = _sd(tmp_path)
    cases = write_corpus(sd / "books")
    for name, case in cases.items():
        rc, out, _ = _run(harness, "epub_dump", sd, f"/books/{name}.epub")
        assert rc == 0, f"{name}: exit {rc}\n{out[-3000:]}"
        assert re.search(r"^LIBRARY loadMetadata ok=1", out, flags=re.M), name
        assert re.search(r"^EXIT 0$", out, flags=re.M), name
        spine, labels = _epub_spine_and_toc(sd / "books" / f"{name}.epub")
        n = len(spine)  # long documents are split into several chapters
        assert n >= len(case.docs), name
        assert re.search(rf"^SPINE count={n} ", out, flags=re.M), name
        assert re.search(rf"^TOC count={n}$", out, flags=re.M), name
        chapter_results = re.findall(r"^CHAPTER (\d+) result ok=(\d) pages=(\d+)", out, flags=re.M)
        assert len(chapter_results) == n and all(ok == "1" and int(p) >= 1 for _, ok, p in chapter_results), name
        if case.title:
            assert _field(out, "LIBRARY", "title") == case.title, name
            assert _field(out, "META", "title") == case.title, name
        if case.author:
            assert _field(out, "LIBRARY", "author") == case.author, name
        toc_titles = re.findall(r"^\s+toc\[\d+\] level=\d+ title=\[(.*?)\] href=", out, flags=re.M)
        assert toc_titles == labels, name
        firsts = [toc_titles[i] for i in _first_chapter_indexes(spine)]
        assert firsts == [d.title if len(d.title.encode()) <= 500 else firsts[i] for i, d in enumerate(case.docs)], name
        assert all(t and "\n" not in t for t in toc_titles)
        lines = [_plain(ln) for ln in _page_lines(out)]
        assert lines, name
        assert not any("https://" in ln or "http://" in ln for ln in lines), name  # no long URLs in the body


@firmware
def test_firmware_epub_layout_details(harness, tmp_path):
    sd = _sd(tmp_path)
    cases = write_corpus(sd / "books")
    _, out, _ = _run(harness, "epub_dump", sd, "/books/lists.epub")
    lines = [_plain(ln) for ln in _page_lines(out)]
    # every list item is its own line with its marker; nested items keep their text
    for want in ("\u2022 Fruit", "\u2022 Apples", "1. Granny Smith", "2. Pink Lady", "\u2022 Pears", "\u2022 Vegetables"):
        assert want in lines, (want, lines[:30])
    assert not any("Fruit" in ln and "Apples" in ln for ln in lines)  # parent text not glued to child
    _, out, _ = _run(harness, "epub_dump", sd, "/books/single.epub")
    lines = [_plain(ln) for ln in _page_lines(out)]
    assert _squash(lines[0]) == _squash(cases["single"].title)
    assert "A. Writer" in lines[1]
    assert lines[-1] == "Source: example.com"
    _, out, _ = _run(harness, "epub_dump", sd, "/books/pre.epub")
    lines = [ln for ln in _page_lines(out)]
    assert any(ln.startswith("def area(r):") for ln in lines)
    assert any(re.match(r"\s{4,}return 3\.14159", ln) for ln in lines), lines[:12]  # NBSP indentation kept
    assert any(re.match(r"\s{4,}print\('<tab> & done'\)", ln) for ln in lines), lines[:12]
    _, out, _ = _run(harness, "epub_dump", sd, "/books/unicode.epub")
    assert _field(out, "META", "lang") == "pl"
    text = " ".join(_plain(ln) for ln in _page_lines(out))
    assert "\u00ab\u00e9t\u00e9\u00bb" in text and "Source: b\u00fccher.example" in text
    assert "<p>not a tag</p>." in text and "5 < 6 && 7 > 3" in text


@firmware
def test_firmware_txt_corpus(harness, tmp_path):
    sd = _sd(tmp_path)
    docs = {f"{c.name}-{i}": d for c in corpus() for i, d in enumerate(c.docs)}
    for stem, doc in docs.items():
        (sd / "books" / f"{stem}.txt").write_bytes(render_txt(doc))
    for stem in docs:
        rc, out, secs = _run(harness, "txt_dump", sd, f"/books/{stem}.txt", timeout=TXT_BUDGET_SECONDS * 3)
        assert rc == 0, f"{stem}: exit {rc}\n{out[-2000:]}"
        assert "LOAD FAILED" not in out
        title = re.search(r"^TITLE \(status bar / library / recent\) = \[(.*)\]$", out, flags=re.M).group(1)
        assert title == stem  # the TXT title is the file name
        pages = int(re.search(r"totalPages=(\d+)", out).group(1))
        assert pages >= 1, stem
        assert secs < TXT_BUDGET_SECONDS, f"{stem}: {secs:.1f}s"
        assert "<-- control char" not in out, stem


@firmware
def test_firmware_txt_long_article_is_fast(harness, tmp_path):
    """The 900-byte rule keeps TXT indexing linear: compare against one giant paragraph."""
    sd = _sd(tmp_path)
    long_doc = next(c for c in corpus() if c.name == "long").docs[0]
    data = render_txt(long_doc)
    (sd / "books" / "long.txt").write_bytes(data)
    rc, out, secs = _run(harness, "txt_dump", sd, "/books/long.txt", timeout=TXT_BUDGET_SECONDS * 3)
    assert rc == 0
    assert secs < TXT_BUDGET_SECONDS, f"{len(data)} bytes took {secs:.1f}s"
    index_ms = int(re.search(r"indexMs=(\d+)", out).group(1))
    print(f"\n[{harness.family}] long.txt {len(data)} bytes: indexMs={index_ms} wall={secs:.2f}s")


def _name_check(h: Harness, names: list[str]) -> list[list[str]]:
    payload = "\n".join(n.encode("utf-8").hex() for n in names) + "\n"
    proc = subprocess.run([h.tool("name_check")], input=payload, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    rows = [ln.split("\t") for ln in proc.stdout.splitlines()]
    assert len(rows) == len(names)
    return rows


def _unhex(s: str) -> str:
    return bytes.fromhex(s).decode("utf-8")


@firmware
def test_firmware_safe_titles_survive_real_sanitizers(harness):
    titles = sorted({safe_title(t, now=0) for t in ALL_TITLES} | {safe_title(t, max_bytes=60, now=0) for t in TRICKY})
    rows = _name_check(harness, titles)
    for title, row in zip(titles, rows):
        assert _unhex(row[0]) == title, (title, _unhex(row[0]))  # sanitizeFilename (1.x/2.x)
        assert _unhex(row[1]) == title + ".epub", title  # OPDS file name (TitleOnly)
        if harness.family == "2x":
            assert _unhex(row[2]) == title + ".epub", title
            assert _unhex(row[3]) == title + ".txt", title
            assert row[4] == "1" and row[5] == "1", (title, row)


@firmware
def test_firmware_python_ports_match_cpp(harness):
    """names.fw_* reproduce the C++ exactly on raw (unsanitized) input."""
    raw = [t for t in ALL_TITLES if "\x00" not in t]
    raw = [t for t in raw if _encodable(t)]
    rows = _name_check(harness, raw)
    for name, row in zip(raw, rows):
        assert _unhex(row[0]) == fw_sanitize_filename(name), repr(name)
        if harness.family == "2x":
            for ext, col, ok_col in ((".epub", 2, 4), (".txt", 3, 5)):
                cpp = _unhex(row[col])
                assert cpp == fw_sanitize_preserving_extension(name + ext), repr(name)
                assert (row[ok_col] == "1") == fw_plugin_filename_ok(cpp, "/Inbox"), (repr(name), cpp)


def _encodable(s: str) -> bool:
    try:
        s.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False
