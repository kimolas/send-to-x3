"""S4: OPDS browse + download (1.6.5 and 2.x).

The card carries one OPDS server (``/.crosspoint/opds.json``: the tap's ``/opds``, user
``x3``, password = device token) and ``opdsDownloadFolder=/Inbox``. 1.6.5 opens it
from Home > OPDS Browser; 2.x from Home > Plugins > OPDS Browser (single server ->
straight into the feed), then a publication page whose Confirm downloads. The device
must send HTTP Basic, parse our Atom feed, and save the first entry, "Get 2 new"
(everything new as one EPUB, no author), as ``/Inbox/<title>.epub`` (default
``opdsFilenameFormat`` 0: ``Author - Title``, here without an author).
"""

from __future__ import annotations

import base64
import hashlib
import re

from e2e.sim import sd as sdmod
from e2e.sim.nav import Nav
from e2e.sim.runner import InputScript, run_sim

from .base import Context, epub_info, scenario, valid_epub


@scenario("S4", "OPDS browse + download", firmwares=("2.0.0rc", "1.6.5", "develop", "fork"))
def run(ctx: Context) -> None:
    backend, tap = ctx.start_backend()
    older = backend.seed(title="Older OPDS entry", text="Second in the feed.")
    newest = backend.seed(title="How e-ink works", text="First in the feed (newest).\n\nTwo paragraphs.")

    server = sdmod.OpdsServer(name="Send to X3", url=f"{tap.url}/opds", username="x3", password=backend.device_token)
    bundle = backend.bundle_zip(source=ctx.bundle_source) if ctx.fw.plugins else None
    card = sdmod.build_sd(ctx.out / "sd", ctx.fw, opds_servers=[server], bundle=bundle)

    s = InputScript()
    nav = Nav(ctx.fw, s, has_opds_servers=True)
    nav.open_opds()
    s.shot("feed")
    nav.opds_download(0)  # "Get 2 new"
    s.shot("downloaded")
    s.quit()
    run_ = ctx.record_run(run_sim(ctx.program, card.root, s, ctx.out / "run", name="opds",
                                  stop_when=lambda: any(n.endswith(".epub") for n in card.inbox_files()),
                                  stop_grace_ms=1500))
    ctx.dump_tap(tap, "backend_exchanges")

    ctx.check("opened the OPDS browser", run_.entered("OpdsBookBrowser"), run_.activities())
    feeds = [e for e in tap.find("/opds") if e.path == "/opds"]
    want_auth = "Basic " + base64.b64encode(f"x3:{backend.device_token}".encode()).decode()
    auths = [(e.request.header("Authorization"), e.status) for e in feeds]
    if ctx.fw.version.startswith("1."):
        # 1.6.5 sends Basic pre-emptively on every request (HttpDownloader.cpp:84-88).
        ctx.check("feed requested with HTTP Basic x3:<device token> up front",
                  bool(feeds) and all(a == want_auth for a, _ in auths), auths)
    else:
        # 2.x (freeink-sdk OpdsClient.cpp:64-110,164-250) asks without credentials, re-probes
        # the 401 for an auth document, then latches Basic for the session.
        ctx.check("unauthenticated feed requests got 401, then Basic was sent",
                  bool(feeds) and all((a is None and st == 401) or (a == want_auth and st == 200) for a, st in auths)
                  and any(a == want_auth for a, _ in auths), auths)
    ok_feeds = [e for e in feeds if e.status == 200]
    ctx.check("feed answered 200 with Content-Length",
              bool(ok_feeds) and all(e.response and e.response.header("Content-Length") for e in ok_feeds),
              [e.status for e in feeds])
    feeds = ok_feeds
    fetch_title = fetch_href = None
    if feeds and feeds[0].response:
        m = re.search(rb'<entry><title>([^<]*)</title><id>urn:sendtox3:(all-[a-z2-7]{10})</id>', feeds[0].response.body)
        fetch_title, fetch_href = (m.group(1).decode(), f"/opds/items/{m.group(2).decode()}.epub") if m else (None, None)
    ctx.check("first feed entry is \"Get 2 new\" without an author",
              bool(fetch_title and fetch_title.startswith("Get 2 new · ")), fetch_title)

    dls = [e for e in tap.find(fetch_href) if e.status == 200] if fetch_href else []
    ctx.check("downloaded \"Get 2 new\"", bool(dls), [e.target for e in tap.exchanges()])
    ctx.check("download sent HTTP Basic too", bool(dls) and dls[0].request.header("Authorization") == want_auth)
    ctx.check("no single entry was downloaded", not tap.find(f"/opds/items/{older['id']}.epub")
              and not tap.find(f"/opds/items/{newest['id']}.epub"))
    inbox = card.inbox_files()
    epubs = {n: i for n, i in inbox.items() if n.endswith(".epub")}
    ctx.check("exactly one EPUB landed in /Inbox", len(epubs) == 1, sorted(inbox))
    if epubs and dls and dls[0].response:
        name, info = next(iter(epubs.items()))
        expected = f"{fetch_title}.epub"
        ctx.check("file name is '<title>.epub'", name == expected, {"name": name, "expected": expected})
        body = dls[0].response.body
        ctx.check("file equals the served EPUB", info.sha256 == hashlib.sha256(body).hexdigest())
        book = card.path(f"/Inbox/{name}").read_bytes()
        meta = epub_info(book)
        ctx.check("it is one well-formed EPUB with both entries", valid_epub(book) and meta.get("chapters") == 2,
                  meta)
        ctx.artifact("downloaded_file", card.path(f"/Inbox/{name}"))
    ctx.check("no OPDS errors in the firmware log", not [ln for ln in run_.errors() if "OPDS" in ln or "HTTP" in ln],
              run_.errors())
