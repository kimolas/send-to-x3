"""S6: Confirm opens the book (the Send to X3 firmware, ``download.open``).

On the fork, Confirm on a catalog row downloads the book and sends the queued plugin
events while Wi-Fi is still up (so the app hears about the download before the X3
restarts), or finds it already on the card and sends nothing; either way it records
the book in state.json and restarts straight into the reader. Runs with
``restart="exec"``: the simulator's restart patch relaunches the process like the
device's reboot, so the reader comes up in a second boot that must start in
EpubReader (no Boot, no Home) with the RTC flags intact.

One card, four runs:

1. "Get 2 new": GET with ``?confirm=1``; the file lands; ``POST /device/v1/fetched``
   with the saved path goes out before the relaunch; boot 2 opens that file
   (state.json and recent.json name it), page 1 is the same page Browse Files shows
   for it, DOWN turns the page. Afterwards the first row reads "Nothing new right now".
2. A single row (cold boot): downloads, reports and opens the same way; DOWN turns to
   page 2, which the reader saves (``progress.bin``).
3. The same row again: the file is on the card, so there is no request at all; it
   still opens ("Already on the card"), on page 2: the reading position survived
   (``progress.bin`` unchanged, the reader loaded it, the page is run 2's page 2).
4. "Nothing new right now", whose download is a 404: the error screen, no restart, no
   file, state.json unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re

from e2e.sim import images, sd as sdmod
from e2e.sim.firmware import HOME_RECENT_ROWS
from e2e.sim.nav import Nav
from e2e.sim.runner import InputScript, Shot, SimRun, run_sim

from .base import LONG_TEXT, Context, scenario, valid_epub

SEED = [
    {"title": "How e-paper keeps its picture", "text": LONG_TEXT},
    # Different text: the app treats a repeat of the same text as a duplicate.
    {"title": "Notes on calm reading", "text": LONG_TEXT + "\n\nA closing note, so this is not the same text."},
]
#: Errors that would mean the download, the report or the book went wrong.
ERROR_TAGS = ("PCAT", "CAT", "HTTP", "PEVT", "EBP", "READER")
_RESTORED_RE = re.compile(r"^\[SIM\] software restart: boot \d+, restored (\d+)/(\d+) rtc bytes$", re.M)


def _first_row(backend) -> dict:
    status, body, _ = backend.http("GET", "/device/v1/catalog?page=1&limit=9")
    return json.loads(body)["items"][0] if status == 200 else {}


def _state(card: sdmod.SdCard) -> dict:
    p = card.path("/.crosspoint/state.json")
    return json.loads(p.read_text("utf-8")) if p.is_file() else {}


def _recent(card: sdmod.SdCard) -> list[str]:
    p = card.path("/.crosspoint/recent.json")
    return [b.get("path") for b in json.loads(p.read_text("utf-8")).get("books", [])] if p.is_file() else []


def _progress(card: sdmod.SdCard) -> dict[str, bytes]:
    """``progress.bin`` of each book the reader has opened, by its cache folder
    (``/.crosspoint/epub_<hash of the path>``): spine index and page (u16 LE each), then
    the chapter's page count and text offset (EpubReaderActivity.cpp:277-296)."""
    return {p.parent.name: p.read_bytes() for p in card.path("/.crosspoint").glob("epub_*/progress.bin")}


def _position(progress: bytes) -> tuple[int, int]:
    """(spine index, page) from ``progress.bin``; (-1, -1) when too short."""
    return (int.from_bytes(progress[0:2], "little"), int.from_bytes(progress[2:4], "little")) \
        if len(progress) >= 4 else (-1, -1)


def _body(shot: Shot | None) -> images.Gray | None:
    """The page without the top status bar and the bottom hint row, which are not the book."""
    if not (shot and shot.taken):
        return None
    g = shot.gray()
    return g.region(0, 60, g.width, g.height - 60)


def _open_row(ctx: Context, card: sdmod.SdCard, name: str, row: int, *, recent_books: int,
              reader_shots: tuple[str, ...] = ("reader",)) -> SimRun:
    """Home > Plugins > Send to X3, Confirm ``row``; the relaunched process takes
    ``reader_shots`` (DOWN between them) and quits."""
    s = InputScript()
    nav = Nav(ctx.fw, s, recent_books=recent_books)
    nav.open_plugin("Send to X3", plugin_titles=["Send to X3"])
    s.shot("catalog")
    nav.catalog_fetch(row)
    s.shot("after-confirm")  # only taken when no restart happens
    s.quit()
    after = InputScript(start_ms=nav.w.open_book)
    for i, shot in enumerate(reader_shots):
        if i:
            Nav(ctx.fw, after).page_forward()
        after.shot(shot)
    after.quit()
    return ctx.record_run(run_sim(ctx.program, card.root, s, ctx.out / name, name=name, restart="exec",
                                  after_restart=after))


def _check_opened(ctx: Context, run: SimRun, label: str, book: str) -> None:
    """The run restarted once into the reader with ``book``."""
    boots = run.boots()
    ctx.check(f"{label}: the process relaunched exactly once (the X3's reboot), and quit normally",
              len(boots) == 2 and len(run.relaunched_at) == 1 and run.returncode == 0 and not run.timed_out,
              {"boots": len(boots), "returncode": run.returncode, "timed_out": run.timed_out})
    restored = _RESTORED_RE.findall(boots[1]) if len(boots) > 1 else []
    ctx.check(f"{label}: the RTC variables survived the restart",
              len(restored) == 1 and restored[0][0] == restored[0][1] and int(restored[0][1]) > 0, restored)
    ctx.check(f"{label}: boot 1 asked for a restart into the reader, after \"Opening in the reader\"",
              run.silent_restarts(boot=0)[:1] == ["reader"] and f"Opening in the reader: {book}" in boots[0],
              {"targets": run.silent_restarts(boot=0), "pcat": [ln for ln in run.lines(tag="PCAT")][-4:]})
    ctx.check(f"{label}: boot 1 never entered the reader (the book opened through the restart)",
              run.entered("PluginCatalog", boot=0) and not run.entered("EpubReader", boot=0), run.activities(boot=0))
    ctx.check(f"{label}: boot 2 started in EpubReader with {book} (no Boot, no Home)",
              len(boots) == 2 and run.activities(boot=1)[:1] == ["EpubReader"]
              and not {"Boot", "Home"} & set(run.activities(boot=1)) and f"Loading ePub: {book}" in boots[1],
              run.activities(boot=1))
    errs = [ln for ln in run.errors() if any(f"[{t}]" in ln for t in ERROR_TAGS)]
    ctx.check(f"{label}: no firmware errors about the download, the report or the book", not errs, errs)


def _check_landed(ctx: Context, card: sdmod.SdCard, backend, tap, label: str, item_id: str, book: str) -> None:
    got = [e for e in tap.find(f"/device/v1/items/{item_id}.epub") if e.status == 200]
    ctx.check(f"{label}: fetched {item_id}.epub with ?confirm=1 and the device token",
              len(got) == 1 and "confirm=1" in got[0].target
              and got[0].request.header("Authorization") == f"Bearer {backend.device_token}",
              [e.target for e in tap.exchanges()])
    on_card = card.inbox_files().get(book.rsplit("/", 1)[1])
    ctx.check(f"{label}: {book} is on the card, byte for byte as served",
              bool(got) and on_card is not None and got[0].response is not None
              and on_card.sha256 == hashlib.sha256(got[0].response.body).hexdigest()
              and valid_epub(got[0].response.body), sorted(card.inbox_files()))


def _check_reported(ctx: Context, run: SimRun, tap, label: str, book: str, before: int) -> None:
    posts = tap.find("/device/v1/fetched", "POST")[before:]
    ctx.check(f"{label}: the X3 reported {book} (POST /device/v1/fetched, 200)",
              len(posts) == 1 and posts[0].request.body.decode("utf-8", "replace") == book and posts[0].status == 200,
              [(e.request.body[:120], e.status) for e in posts])
    ctx.check(f"{label}: ... before it restarted (the events were sent while Wi-Fi was up)",
              bool(posts) and bool(run.relaunched_at) and posts[0].t < run.relaunched_at[0]
              and bool(run.find(r"\[PEVT\] sendtox3: drained \(stalled=0\)")),
              {"post_at": [e.t for e in posts], "relaunched_at": run.relaunched_at})


@scenario("S6", "Confirm opens the book (download.open, restart into the reader)", firmwares=("fork",))
def run(ctx: Context) -> None:
    if not (ctx.fw.opens_after_download and ctx.fw.sim_restart):
        ctx.check(f"{ctx.fw.ref} opens downloads and relaunches on restart", False,
                  "S6 needs a build with download.open and the simulator restart patch")
        return
    backend, tap = ctx.start_backend()
    seeded = [backend.seed(**s) for s in SEED]
    card = sdmod.build_sd(ctx.out / "sd", ctx.fw, bundle=backend.bundle_zip(source=ctx.bundle_source))
    manifest = json.loads(card.path(f"{card.plugin_dir}/device.json").read_text("utf-8"))
    ctx.check("the plugin asks for books to open (download.open)", manifest.get("download", {}).get("open") is True,
              manifest.get("download"))
    top = _first_row(backend)
    ctx.check("first row is \"Get 2 new\"", str(top.get("id", "")).startswith("all-")
              and str(top.get("title", "")).startswith("Get 2 new · "), top)
    fetched = f"/Inbox/{top.get('title', '?')}.epub"

    # 1. "Get 2 new" opens.
    run1 = _open_row(ctx, card, "run1-get-new", 0, recent_books=0, reader_shots=("reader-p1", "reader-p2"))
    _check_landed(ctx, card, backend, tap, "1", str(top.get("id")), fetched)
    _check_reported(ctx, run1, tap, "1", fetched, 0)
    _check_opened(ctx, run1, "1", fetched)
    ctx.check("1: the outbox is empty", card.outbox() == [], card.outbox())
    ctx.check("1: state.json and recent.json name the book",
              _state(card).get("openEpubPath") == fetched and _recent(card)[:1] == [fetched],
              {"state": _state(card), "recent": _recent(card)})
    body1, body2 = _body(run1.shots.get("reader-p1")), _body(run1.shots.get("reader-p2"))
    ctx.check("1: reader screenshots saved", bool(body1 and body2))
    if body1 and body2:
        ctx.check("1: page 1 shows text", body1.ink_ratio() > 0.02, round(body1.ink_ratio(), 4))
        delta = images.difference(body1, body2)
        ctx.check("1: DOWN turned the page", delta > 0.01, round(delta, 4))
        # The same file opened the usual way, Home > Browse Files, on a card of its own.
        ref_card = sdmod.build_sd(ctx.out / "sd-reference", ctx.fw,
                                  files={fetched: card.path(fetched).read_bytes()})
        s = InputScript()
        Nav(ctx.fw, s).open_file(ref_card.root, fetched)
        s.shot("reference-p1")
        s.quit()
        ref = ctx.record_run(run_sim(ctx.program, ref_card.root, s, ctx.out / "reference", name="reference"))
        r1 = _body(ref.shots.get("reference-p1"))
        if r1:
            same = images.difference(body1, r1)
            ctx.check("1: page 1 is the page Browse Files shows for the same file", same < 0.002, round(same, 5))
        else:
            ctx.check("1: page 1 is the page Browse Files shows for the same file", False, ref.activities())
    after = _first_row(backend)
    ctx.check("1: afterwards the first row reads \"Nothing new right now\"", after.get("id") == "none", after)

    # 2. A single row downloads and opens. Rows: "Nothing new right now", then newest first.
    #    DOWN turns to page 2, the reading position run 3 must come back to.
    single = seeded[1]
    book = f"/Inbox/{single['title']}.epub"
    posts = len(tap.find("/device/v1/fetched", "POST"))
    caches = set(_progress(card))
    run2 = _open_row(ctx, card, "run2-single", 1, recent_books=HOME_RECENT_ROWS, reader_shots=("reader", "reader-p2"))
    _check_landed(ctx, card, backend, tap, "2", single["id"], book)
    _check_reported(ctx, run2, tap, "2", book, posts)
    _check_opened(ctx, run2, "2", book)
    ctx.check("2: state.json names the book", _state(card).get("openEpubPath") == book, _state(card))
    new = sorted(set(_progress(card)) - caches)
    saved = _progress(card).get(new[0], b"") if len(new) == 1 else b""
    ctx.check("2: the reader saved page 2 of the book (progress.bin)", len(new) == 1 and _position(saved)[1] == 1,
              {"new caches": new, "progress": saved.hex()})
    r2p1, r2p2 = _body(run2.shots.get("reader")), _body(run2.shots.get("reader-p2"))
    ctx.check("2: DOWN turned the page", bool(r2p1 and r2p2) and images.difference(r2p1, r2p2) > 0.01,
              round(images.difference(r2p1, r2p2), 4) if r2p1 and r2p2 else "screenshots missing")

    # 3. The same row again: already on the card, so no request; it still opens.
    items_before = len(tap.find("/device/v1/items/"))
    posts = len(tap.find("/device/v1/fetched", "POST"))
    on_card = card.inbox_files()
    run3 = _open_row(ctx, card, "run3-already-on-card", 1, recent_books=HOME_RECENT_ROWS)
    ctx.check("3: no download request and no report",
              len(tap.find("/device/v1/items/")) == items_before and len(tap.find("/device/v1/fetched")) == posts,
              [e.target for e in tap.exchanges()][-6:])
    ctx.check("3: the firmware found it on the card", f"Already on the card: {book}" in run3.boots()[0],
              run3.lines(tag="PCAT")[-4:])
    ctx.check("3: the card is unchanged", card.inbox_files() == on_card, sorted(card.inbox_files()))
    _check_opened(ctx, run3, "3", book)
    # Where you left off: downloading again would have cleared the cache and with it the
    # position, so run 3 must open on run 2's page 2, not page 1.
    spine, page = _position(saved)
    boots3 = run3.boots()
    ctx.check(f"3: the reader loaded the saved position (spine {spine}, page {page})",
              bool(saved) and len(boots3) == 2 and f"[ERS] Loaded cache: {spine}, {page}" in boots3[1],
              [ln for ln in run3.lines(tag="ERS") if "Loaded cache" in ln])
    after3 = _progress(card).get(new[0], b"") if len(new) == 1 else b""
    ctx.check("3: progress.bin still holds page 2, byte for byte", bool(saved) and after3 == saved,
              {"after run 2": saved.hex(), "after run 3": after3.hex()})
    r3 = _body(run3.shots.get("reader"))
    if r3 and r2p1 and r2p2:
        same, first = images.difference(r3, r2p2), images.difference(r3, r2p1)
        ctx.check("3: it opened on run 2's page 2, not on page 1", same < 0.002 and first > 0.01,
                  {"vs page 2": round(same, 5), "vs page 1": round(first, 4)})
    else:
        ctx.check("3: it opened on run 2's page 2, not on page 1", False, "screenshots missing")

    # 4. "Nothing new right now" has nothing to download (404): the error screen, no restart.
    state_before = _state(card)
    on_card = card.inbox_files()
    run4 = _open_row(ctx, card, "run4-nothing-new", 0, recent_books=HOME_RECENT_ROWS)
    none = [e for e in tap.find("/device/v1/items/none.epub")]
    ctx.check("4: the download of \"Nothing new right now\" got 404", len(none) == 1 and none[0].status == 404,
              [e.summary() for e in none])
    ctx.check("4: the catalog showed the failure", bool(run4.find(r"\[CAT\] Download failed")),
              run4.lines(tag="CAT"))
    ctx.check("4: no restart and no reader", len(run4.boots()) == 1 and not run4.silent_restarts()
              and not run4.entered("EpubReader"), {"boots": len(run4.boots()), "activities": run4.activities()})
    ctx.check("4: no file, and state.json unchanged",
              card.inbox_files() == on_card and _state(card) == state_before,
              {"inbox": sorted(card.inbox_files()), "state": _state(card)})
    ctx.dump_tap(tap, "backend_exchanges")
