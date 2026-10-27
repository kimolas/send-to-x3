"""S1: Plugins > Send to X3 catalog browse + download lands ``/Inbox/<title>.epub``."""

from __future__ import annotations

import hashlib

from e2e.sim import sd as sdmod
from e2e.sim.nav import Nav
from e2e.sim.runner import InputScript, run_sim

from .base import Context, scenario, valid_epub

# Newest first in the catalog: row 0 is the last one seeded.
SEED = [
    {"title": "Hello from the Pi", "text": "This is the first queued item.\n\nIt has two paragraphs."},
    # Unsafe for the firmware as-is: "..." is refused (PluginCatalogActivity.cpp:1075-1080),
    # "/" and ":" would be replaced; the backend must make it safe (§8).
    {"title": "Wait... what? A/B: café “quotes”", "text": "Title with dots, a slash, a colon and UTF-8."},
]


@scenario("S1", "plugin catalog browse + download", firmwares=("2.0.0rc",))
def run(ctx: Context) -> None:
    backend, tap = ctx.start_backend()
    seeded = [backend.seed(**s) for s in SEED]
    titles = [s["title"] for s in seeded]
    ctx.check("backend made titles firmware-safe", all(".." not in t and "/" not in t for t in titles), titles)

    card = sdmod.build_sd(ctx.out / "sd", ctx.fw, bundle=backend.bundle_zip(source=ctx.bundle_source))
    ctx.check("bundle installed a plugin", card.plugin_dir is not None, card.plugin_dir or "")

    s = InputScript()
    nav = Nav(ctx.fw, s)
    nav.open_plugin("Send to X3", plugin_titles=["Send to X3"])
    s.shot("catalog")
    nav.catalog_fetch(0)  # newest: seeded[1]
    s.shot("downloaded-row0")
    nav.dismiss()
    s.presses("DOWN", 1)
    s.press("ENTER", wait_ms=nav.w.download)
    s.shot("downloaded-row1")
    s.quit()
    expected = {f"{t}.epub" for t in titles}
    run_ = ctx.record_run(run_sim(ctx.program, card.root, s, ctx.out / "run", name="catalog",
                                  stop_when=lambda: expected <= set(card.inbox_files()), stop_grace_ms=1500))
    ctx.dump_tap(tap, "backend_exchanges")

    ctx.check("opened the plugin catalog", run_.entered("PluginCatalog"), run_.activities())
    browse = tap.find("/device/v1/catalog")
    ctx.check("browse request with page=1&limit=9", any("page=1&limit=9" in e.target for e in browse),
              [e.target for e in browse])
    ctx.check("browse sent the device token",
              all(e.request.header("Authorization") == f"Bearer {backend.device_token}" for e in browse) and browse)
    ctx.check("browse answered 200 with Content-Length",
              all(e.status == 200 and e.response and e.response.header("Content-Length") for e in browse) and browse)

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
    ctx.check("no firmware errors about the download",
              not [ln for ln in run_.errors() if "PCAT" in ln or "HTTP" in ln], run_.errors())
