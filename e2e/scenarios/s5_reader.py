"""S5: open a delivered EPUB and TXT in the real reader and screenshot pages 1-2.

The files are exactly what the backend serves (``/device/v1/items/<id>.epub|.txt``),
placed in ``/Inbox`` and opened via Home > Browse Files > Inbox > file. OCR-free
verification: the reader activity is entered, the firmware logs no errors, both
pages carry ink, and page 2 differs from page 1 (the page turn happened and the
book has more than one page). The PNGs are for humans to look at.
"""

from __future__ import annotations

from e2e.sim import images, sd as sdmod
from e2e.sim.nav import Nav
from e2e.sim.runner import InputScript, run_sim

from .base import LONG_TEXT, Context, scenario

TITLE = "How e-paper keeps its picture"
#: Firmware log lines that are expected and harmless while opening a fresh book.
#: 2.x opens TXT through the EPUB engine; a TXT "book" has no CSS cache, so
#: Section.cpp:433-434 logs this on every TXT open (layout continues normally).
BENIGN_TXT_2X = "[SCT] Failed to load CSS from cache"


@scenario("S5", "open delivered EPUB and TXT in the reader, screenshot pages 1-2",
          firmwares=("2.0.0rc", "1.6.5", "develop", "fork"))
def run(ctx: Context) -> None:
    backend, _tap = ctx.start_backend()
    item = backend.seed(title=TITLE, text=LONG_TEXT)
    files = {fmt: backend.item_bytes(item["id"], fmt) for fmt in ("epub", "txt")}
    names = {fmt: f"{item['title']}.{fmt}" for fmt in files}

    for fmt in ("epub", "txt"):
        card = sdmod.build_sd(ctx.out / f"sd-{fmt}", ctx.fw, files={f"/Inbox/{names[fmt]}": files[fmt]})
        s = InputScript()
        nav = Nav(ctx.fw, s)
        nav.open_file(card.root, f"/Inbox/{names[fmt]}")
        s.shot("page1")
        nav.page_forward()
        s.shot("page2")
        s.quit()
        run_ = ctx.record_run(run_sim(ctx.program, card.root, s, ctx.out / f"read-{fmt}", name=f"read-{fmt}"))
        activity = ctx.fw.epub_reader_activity if fmt == "epub" else ctx.fw.txt_reader_activity
        ctx.check(f"{fmt}: opened in {activity}", run_.entered("FileBrowser") and run_.entered(activity),
                  run_.activities())
        benign = (BENIGN_TXT_2X,) if fmt == "txt" and ctx.fw.version.startswith("2") else ()
        errs = run_.errors(ignore=benign)
        ctx.check(f"{fmt}: no firmware errors while opening and paging", not errs, errs)
        p1, p2 = run_.shots.get("page1"), run_.shots.get("page2")
        ctx.check(f"{fmt}: both page screenshots saved", bool(p1 and p1.taken and p2 and p2.taken))
        if p1 and p1.taken and p2 and p2.taken:
            g1, g2 = p1.gray(), p2.gray()
            # Ignore the top status bar and the bottom hint row: text must be in the body.
            body1 = g1.region(0, 60, g1.width, g1.height - 60)
            body2 = g2.region(0, 60, g2.width, g2.height - 60)
            ink1, ink2 = body1.ink_ratio(), body2.ink_ratio()
            ctx.check(f"{fmt}: page 1 shows text", ink1 > 0.02, round(ink1, 4))
            ctx.check(f"{fmt}: page 2 shows text", ink2 > 0.02, round(ink2, 4))
            delta = images.difference(body1, body2)
            ctx.check(f"{fmt}: page 2 differs from page 1", delta > 0.01, round(delta, 4))
