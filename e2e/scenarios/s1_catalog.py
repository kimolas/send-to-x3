"""S1: Plugins > Send to X3: the first row, "Get 2 new", lands everything new as one
``/Inbox/<row title>.epub``; the X3 confirms it at the next Wi-Fi join (its
``book.downloaded`` event), and only then the row reads "Nothing new right now"; single
rows land ``/Inbox/<title>.epub``."""

from __future__ import annotations

import hashlib
import json

from e2e.sim import sd as sdmod
from e2e.sim.nav import Nav
from e2e.sim.runner import InputScript, run_sim

from .base import Context, epub_info, scenario, valid_epub

# Row 0 is "Get 2 new"; then newest first, so row 1 is the last one seeded.
SEED = [
    {"title": "Hello from the Pi", "text": "This is the first queued item.\n\nIt has two paragraphs."},
    # Unsafe for the firmware as-is: "..." is refused (PluginCatalogActivity.cpp:1075-1080),
    # "/" and ":" would be replaced; the backend must make it safe (§8).
    {"title": "Wait... what? A/B: café “quotes”", "text": "Title with dots, a slash, a colon and UTF-8."},
]


def _first_row(backend) -> dict:
    status, body, _ = backend.http("GET", "/device/v1/catalog?page=1&limit=9")
    return json.loads(body)["items"][0] if status == 200 else {}


@scenario("S1", "plugin catalog: \"Get N new\" (confirmed by the X3) + single downloads", firmwares=("2.0.0rc", "3f387a9"))
def run(ctx: Context) -> None:
    backend, tap = ctx.start_backend()
    seeded = [backend.seed(**s) for s in SEED]
    titles = [s["title"] for s in seeded]
    ctx.check("backend made titles firmware-safe", all(".." not in t and "/" not in t for t in titles), titles)

    card = sdmod.build_sd(ctx.out / "sd", ctx.fw, bundle=backend.bundle_zip(source=ctx.bundle_source))
    ctx.check("bundle installed a plugin", card.plugin_dir is not None, card.plugin_dir or "")
    manifest = json.loads(card.path(f"{card.plugin_dir}/device.json").read_text("utf-8"))
    ctx.check("bundle has no sleep delivery (auto_delivery is off by default), only the download report",
              list(manifest.get("events") or {}) == ["book.downloaded"], manifest.get("events"))
    # What the X3 will list (the same new items always give the same "Get N new" row).
    top = _first_row(backend)
    ctx.check("first row is \"Get 2 new\"", str(top.get("id", "")).startswith("all-")
              and str(top.get("title", "")).startswith("Get 2 new · "), top)
    fetch_name = f"{top.get('title', '?')}.epub"

    # Run 1: open the plugin and press Confirm on the first row.
    s = InputScript()
    nav = Nav(ctx.fw, s)
    nav.open_plugin("Send to X3", plugin_titles=["Send to X3"])
    s.shot("catalog")
    nav.catalog_fetch(0)  # "Get 2 new"
    s.shot("fetched-everything-new")
    s.quit()
    run1 = ctx.record_run(run_sim(ctx.program, card.root, s, ctx.out / "run1", name="fetch",
                                  stop_when=lambda: fetch_name in card.inbox_files(), stop_grace_ms=1500))
    ctx.check("opened the plugin catalog", run1.entered("PluginCatalog"), run1.activities())
    browse = tap.find("/device/v1/catalog")
    ctx.check("browse request with page=1&limit=9", any("page=1&limit=9" in e.target for e in browse),
              [e.target for e in browse])
    ctx.check("browse sent the device token",
              all(e.request.header("Authorization") == f"Bearer {backend.device_token}" for e in browse) and browse)
    ctx.check("browse answered 200 with Content-Length",
              all(e.status == 200 and e.response and e.response.header("Content-Length") for e in browse) and browse)

    inbox = card.inbox_files()
    got = [e for e in tap.find(f"/device/v1/items/{top.get('id')}.epub") if e.status == 200]
    ctx.check("fetched \"Get 2 new\" with ?confirm=1 and the device token",
              bool(got) and all("confirm=1" in e.target
                                and e.request.header("Authorization") == f"Bearer {backend.device_token}" for e in got),
              [e.target for e in tap.exchanges()])
    on_card = inbox.get(fetch_name)
    ctx.check(f"/Inbox/{fetch_name} exists", on_card is not None, sorted(inbox))
    if got and on_card and got[-1].response:
        body = got[-1].response.body
        ctx.check(f"/Inbox/{fetch_name} matches served bytes",
                  on_card.sha256 == hashlib.sha256(body).hexdigest() and on_card.size == len(body))
        info = epub_info(card.path(f"/Inbox/{fetch_name}").read_bytes())
        ctx.check("\"Get 2 new\" is one valid EPUB with both items, oldest first",
                  valid_epub(body) and info.get("chapters") == 2 and str(info.get("title", "")).startswith("Inbox: 2"),
                  info)
    queued = card.outbox()
    ctx.check("the X3 queued book.downloaded with the saved path",
              [(q.get("e"), (q.get("vars") or {}).get("path")) for q in queued]
              == [("book.downloaded", f"/Inbox/{fetch_name}")], queued)
    waiting = _first_row(backend)
    ctx.check("not counted yet, and not offered again meanwhile: \"Nothing new right now\" (awaiting the report)",
              waiting.get("id") == "none" and "confirmed the next time" in waiting.get("subtitle", ""), waiting)

    # Run 2 (after the restart that leaving the catalog causes): opening the plugin joins
    # Wi-Fi, which sends the report before the list loads; then fetch the single rows.
    s = InputScript()
    nav = Nav(ctx.fw, s)
    nav.open_plugin("Send to X3", plugin_titles=["Send to X3"])
    s.shot("catalog-after-confirm")
    s.presses("DOWN", 1)
    s.press("ENTER", wait_ms=nav.w.download)  # newest: seeded[1]
    s.shot("downloaded-row1")
    nav.dismiss()
    s.presses("DOWN", 1)
    s.press("ENTER", wait_ms=nav.w.download)  # seeded[0]
    s.shot("downloaded-row2")
    s.quit()
    expected = {f"{t}.epub" for t in titles} | {fetch_name}
    n_browse = len(tap.find("/device/v1/catalog"))
    run2 = ctx.record_run(run_sim(ctx.program, card.root, s, ctx.out / "run2", name="confirm-and-singles",
                                  stop_when=lambda: expected <= set(card.inbox_files()), stop_grace_ms=1500))
    ctx.dump_tap(tap, "backend_exchanges")

    order = [e.path for e in tap.exchanges()]
    posts = [e for e in tap.find("/device/v1/fetched")]
    ctx.check("the X3 reported the saved file (POST /device/v1/fetched, body = path, 200)",
              len(posts) >= 1 and posts[0].request.body.decode("utf-8") == f"/Inbox/{fetch_name}"
              and posts[0].status == 200 and posts[0].request.header("Authorization") == f"Bearer {backend.device_token}",
              [(e.request.body[:120], e.status) for e in posts])
    later_browse = [i for i, p in enumerate(order) if p == "/device/v1/catalog"][n_browse:]
    first_post = order.index("/device/v1/fetched") if "/device/v1/fetched" in order else 10**9
    ctx.check("the report went out before the list loaded", bool(later_browse) and first_post < later_browse[0],
              order)
    lists = [e for e in tap.find("/device/v1/catalog")][n_browse:]
    rows = json.loads(lists[0].response.body)["items"] if lists and lists[0].response else []
    ctx.check("after the report the first row is \"Nothing new right now\"",
              bool(rows) and rows[0]["id"] == "none", rows[:1])

    inbox = card.inbox_files()
    for item in seeded:
        name = f"{item['title']}.epub"
        dl = [e for e in tap.find(f"/device/v1/items/{item['id']}.epub") if e.status == 200]
        ctx.check(f"downloaded {item['id']}.epub", bool(dl), [e.target for e in tap.exchanges()])
        on_card = inbox.get(name)
        ctx.check(f"/Inbox/{name} exists", on_card is not None, sorted(inbox))
        if dl and on_card and dl[-1].response:
            body = dl[-1].response.body
            ctx.check(f"/Inbox/{name} matches served bytes",
                      on_card.sha256 == hashlib.sha256(body).hexdigest() and on_card.size == len(body),
                      {"card": on_card.size, "served": len(body)})
            ctx.check(f"{name} is a well-formed EPUB container", valid_epub(card.path(f"/Inbox/{name}").read_bytes()))
    ctx.check("no unexpected files in /Inbox", set(inbox) == expected, sorted(inbox))
    errs = [ln for ln in run1.errors() + run2.errors() if "PCAT" in ln or "HTTP" in ln or "PEVT" in ln]
    ctx.check("no firmware errors about the downloads or the report", not errs, errs)
