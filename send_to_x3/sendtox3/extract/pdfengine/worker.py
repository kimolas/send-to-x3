"""Subprocess entry point: ``python -m sendtox3.extract.pdfengine.worker '<json options>'``.

Reads the PDF from stdin, converts it and writes one JSON object to stdout:
``{"ok": true, "doc": {...}, "stats": {...}}`` or ``{"ok": false, "error": "...",
"kind": "..."}``. Before touching the input it caps its own address space and CPU
time (``resource.setrlimit``), so a hostile or huge PDF can only kill this process.
"""

from __future__ import annotations

import json
import os
import sys
import time


def apply_limits(max_memory_mb: int, cpu_seconds: int) -> dict:
    """Best effort: RLIMIT_AS is enforced on Linux (the add-on), ignored on macOS."""
    applied = {}
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return applied
    if max_memory_mb and max_memory_mb > 0:
        limit = int(max_memory_mb) * 1024 * 1024
        for name in ("RLIMIT_AS", "RLIMIT_DATA"):
            res = getattr(resource, name, None)
            if res is None:
                continue
            try:
                soft, hard = resource.getrlimit(res)
                new = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
                resource.setrlimit(res, (new, hard))
                applied[name] = new
                break  # AS covers DATA; DATA is the fallback where AS is refused
            except (ValueError, OSError):
                continue
    if cpu_seconds and cpu_seconds > 0:
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
            new = cpu_seconds if hard == resource.RLIM_INFINITY else min(cpu_seconds, hard)
            resource.setrlimit(resource.RLIMIT_CPU, (new, hard))
            applied["RLIMIT_CPU"] = new
        except (ValueError, OSError):
            pass
    return applied


def doc_to_dict(doc) -> dict:
    from ...textmodel import Heading, ListItem, Paragraph, Preformatted, Quote, Rule

    blocks = []
    for b in doc.blocks:
        if isinstance(b, Heading):
            blocks.append({"t": "h", "text": b.text, "level": b.level})
        elif isinstance(b, ListItem):
            blocks.append({"t": "li", "text": b.text, "ordered": b.ordered, "index": b.index, "depth": b.depth})
        elif isinstance(b, Quote):
            blocks.append({"t": "q", "text": b.text})
        elif isinstance(b, Preformatted):
            blocks.append({"t": "pre", "text": b.text})
        elif isinstance(b, Rule):
            blocks.append({"t": "hr"})
        elif isinstance(b, Paragraph):
            blocks.append({"t": "p", "text": b.text})
    return {"title": doc.title, "byline": doc.byline, "site": doc.site, "source_url": doc.source_url,
            "published": doc.published, "lang": doc.lang, "notes": list(doc.notes), "blocks": blocks}


def doc_from_dict(d: dict):
    from ...textmodel import Document, Heading, ListItem, Paragraph, Preformatted, Quote, Rule

    blocks = []
    for b in d.get("blocks", []):
        t = b.get("t")
        text = str(b.get("text", ""))
        if t == "h":
            blocks.append(Heading(text, level=int(b.get("level", 2))))
        elif t == "li":
            blocks.append(ListItem(text, ordered=bool(b.get("ordered")), index=int(b.get("index", 1)),
                                   depth=int(b.get("depth", 0))))
        elif t == "q":
            blocks.append(Quote(text))
        elif t == "pre":
            blocks.append(Preformatted(text))
        elif t == "hr":
            blocks.append(Rule())
        elif t == "p":
            blocks.append(Paragraph(text))
    return Document(title=str(d.get("title", "")), blocks=blocks, byline=str(d.get("byline", "")),
                    site=str(d.get("site", "")), source_url=str(d.get("source_url", "")),
                    published=str(d.get("published", "")), lang=str(d.get("lang", "en") or "en"),
                    notes=[str(n) for n in d.get("notes", [])])


def run(data: bytes, opts: dict) -> dict:
    from .convert import PdfError, convert

    budget = float(opts.get("budget", 0) or 0)
    deadline = time.monotonic() + budget if budget > 0 else None
    stats: dict = {}
    try:
        doc = convert(
            data,
            title_hint=str(opts.get("title_hint", "")),
            source_url=str(opts.get("source_url", "")),
            max_pages=int(opts.get("max_pages", 400)),
            ocr_enabled=bool(opts.get("ocr", False)),
            ocr_lang=str(opts.get("ocr_lang", "eng")),
            ocr_dpi=int(opts.get("ocr_dpi", 300)),
            ocr_max_pages=int(opts.get("ocr_max_pages", 20)),
            deadline=deadline,
            stats=stats,
        )
    except PdfError as e:
        return {"ok": False, "kind": "encrypted" if e.encrypted else "invalid", "error": e.reason}
    except MemoryError:
        return {"ok": False, "kind": "memory", "error": "out of memory"}
    if not opts.get("debug"):
        stats.pop("log", None)
    return {"ok": True, "doc": doc_to_dict(doc), "stats": stats}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        opts = json.loads(argv[0]) if argv else {}
    except ValueError:
        opts = {}
    apply_limits(int(opts.get("max_memory_mb", 1024) or 0), int(opts.get("cpu_seconds", 0) or 0))
    # the JSON result owns stdout; anything native code prints goes to stderr instead
    sys.stdout.flush()
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    try:
        data = sys.stdin.buffer.read()
        result = run(data, opts)
    except MemoryError:
        result = {"ok": False, "kind": "memory", "error": "out of memory"}
    except Exception as e:  # never a traceback on stdout
        result = {"ok": False, "kind": "crash", "error": f"{type(e).__name__}: {str(e)[:300]}"}
    try:
        payload = json.dumps(result, ensure_ascii=False)
    except MemoryError:
        payload = json.dumps({"ok": False, "kind": "memory", "error": "out of memory"})
    out.write(payload)
    out.flush()
    out.close()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    sys.exit(main())
