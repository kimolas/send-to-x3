"""W1: the device's own HTTP client (wire harness) against the backend.

The simulator replaces the device HTTP client with curl, so this scenario runs the
firmware's real PluginHttp / PluginEvents / HttpDownloader / SecureHttpClient code
over sockets (``e2e/wire``) to check the exact request bytes and that the backend's
responses frame correctly for that client: catalog browse, download of a row, the
"Get N new" row, sleep-time delivery (200, with the auto_delivery option on), then a drain
with nothing pending (404, line kept). A second card with the default plugin (no sleep
delivery) checks the confirmation path: the download queues ``book.downloaded``, and the
next drain POSTs the saved path to ``/device/v1/fetched``, which only then marks the items.
"""

from __future__ import annotations

import hashlib
import json
import time

from e2e.sim import sd as sdmod
from e2e.wire import wire

from .base import Context, SkipScenario, scenario, valid_epub


@scenario("W1", "wire harness: real device HTTP client against the backend", firmwares=("2.0.0rc", "3f387a9"))
def run(ctx: Context) -> None:
    try:
        binary = wire.find_binary(ctx.fw.ref)
    except FileNotFoundError as e:
        raise SkipScenario(str(e)) from None
    backend, tap = ctx.start_backend(extra_options={"auto_delivery": True})
    item = backend.seed(title="Wire check", text="Exact bytes on the wire.\n\nSecond paragraph.")
    card = wire.prepare_sd(ctx.out / "sd", ref=ctx.fw.ref,
                           bundle=backend.bundle_zip(source=ctx.bundle_source, auto_delivery=True))
    pdir = wire.plugin_dir(card.root)
    token = f"Bearer {backend.device_token}"

    cat = wire.run(binary, card.root, ["catalog", pdir])
    (ctx.out / "catalog.json").write_text(json.dumps(cat.to_json(), indent=2), encoding="utf-8")
    ctx.artifact("catalog", ctx.out / "catalog.json")
    ctx.check("catalog: browse 200 and parsed", cat.returncode == 0 and (cat.browse or {}).get("status") == 200,
              cat.browse)
    ctx.check("catalog: \"Get 1 new\", then the seeded row",
              [r["id"] for r in cat.rows][1:] == [item["id"]] and bool(cat.rows)
              and cat.rows[0]["id"].startswith("all-") and cat.rows[0]["title"].startswith("Get 1 new · "), cat.rows)
    reqs = cat.requests()
    ctx.check("catalog: one request", len(reqs) == 1, [r.start_line for r in reqs])
    if reqs:
        r = reqs[0]
        ctx.check("catalog: request line", r.start_line == "GET /device/v1/catalog?page=1&limit=9 HTTP/1.1",
                  r.start_line)
        names = [k for k, _ in r.headers]
        ctx.check("catalog: headers are Host, User-Agent, Connection, Authorization",
                  names == ["Host", "User-Agent", "Connection", "Authorization"], r.headers)
        ctx.check("catalog: User-Agent CrossPoint, keep-alive, bearer token",
                  r.header("User-Agent") == "CrossPoint" and r.header("Connection") == "keep-alive"
                  and r.header("Authorization") == token, r.headers)

    dl = wire.run(binary, card.root, ["download", pdir, item["id"]])
    d = dl.download or {}
    ctx.check("download: OK with HTTP 200", dl.returncode == 0 and d.get("result") == "OK" and d.get("http") == 200, d)
    dest = d.get("dest", "")
    ctx.check("download: dest is /Inbox/<title>.epub", dest == f"/Inbox/{item['title']}.epub", dest)
    file_reqs = [r for r in dl.requests() if "/device/v1/items/" in r.start_line]
    ctx.check("download: User-Agent CrossPoint-ESP32-<version>",
              bool(file_reqs) and file_reqs[0].header("User-Agent") == f"CrossPoint-ESP32-{ctx.fw.version}",
              [r.headers for r in file_reqs])
    if dest:
        served = [e for e in tap.find(f"/device/v1/items/{item['id']}.epub") if e.status == 200]
        on_card = card.path(dest).read_bytes() if card.path(dest).is_file() else b""
        ctx.check("download: file equals the served bytes and is an EPUB",
                  bool(served) and served[-1].response is not None
                  and hashlib.sha256(on_card).digest() == hashlib.sha256(served[-1].response.body).digest()
                  and valid_epub(on_card), {"card": len(on_card)})

    # "Get N new": a new item, then the first row downloads it (the firmware's own client).
    fresh = backend.seed(title="Wire fetch", text="Fetched with the first row.")
    cat2 = before_sleep = wire.run(binary, card.root, ["catalog", pdir])
    top = cat2.rows[0] if cat2.rows else {}
    ctx.check("fetch: first row is \"Get 1 new\"", str(top.get("id", "")).startswith("all-")
              and str(top.get("title", "")).startswith("Get 1 new · "), cat2.rows)
    if top:
        fdl = wire.run(binary, card.root, ["download", pdir, top["id"]])
        fd = fdl.download or {}
        ctx.check("fetch: OK with HTTP 200", fdl.returncode == 0 and fd.get("result") == "OK" and fd.get("http") == 200,
                  fd)
        fdest = fd.get("dest", "")
        ctx.check("fetch: dest is /Inbox/<row title>.epub", fdest == f"/Inbox/{top['title']}.epub", fdest)
        served = [e for e in tap.find(f"/device/v1/items/{top['id']}.epub") if e.status == 200]
        on_card = card.path(fdest).read_bytes() if fdest and card.path(fdest).is_file() else b""
        ctx.check("fetch: no confirm on a plugin with sleep delivery", not fdl.of("emit"), fdl.of("emit"))
        time.sleep(0.5)  # the X3 restarts after leaving the catalog; the app settles the download meanwhile
        # List before reading the item through the device API (that would mark it itself).
        cat3 = before_sleep = wire.run(binary, card.root, ["catalog", pdir])
        ctx.check("fetch: afterwards the first row is \"Nothing new right now\"",
                  bool(cat3.rows) and cat3.rows[0]["id"] == "none", cat3.rows[:1])
        ctx.check("fetch: file is the new item's EPUB, as served",
                  bool(served) and served[-1].response is not None and on_card == served[-1].response.body
                  and on_card == backend.item_bytes(fresh["id"], "epub"), {"card": len(on_card)})

    # A further item so the sleep delivery has something pending (the others were fetched).
    later = backend.seed(title="Wire sleep delivery", text="Delivered by pluginevents::drain.")
    sl = wire.run(binary, card.root, ["sleep"])
    reqs = [r for r in sl.requests() if "/device/v1/deliver" in r.start_line]
    ctx.check("sleep: one deliver request with ev and ts", len(reqs) == 1 and "ev=" in reqs[0].start_line
              and "&ts=" in reqs[0].start_line, [r.start_line for r in sl.requests()])
    ctx.check("sleep: deliver headers", bool(reqs) and reqs[0].header("Authorization") == token
              and reqs[0].header("User-Agent") == "CrossPoint", [r.headers for r in reqs])
    new = sdmod.diff(before_sleep.files, sl.files).added
    autos = [p for p in new if p.startswith("/Inbox/") and p.endswith(".epub") and p != dest]
    ctx.check("sleep: /Inbox/<ts>-<ev>.epub written", len(autos) == 1, sorted(new))
    outbox = sdmod.sd_path(card.root, pdir + "/events.jsonl")
    ctx.check("sleep: outbox drained", not outbox.exists() or not outbox.read_text().strip(),
              outbox.read_text() if outbox.exists() else "")
    st = backend.state()
    if st is not None:
        auto = {i["id"]: "auto" in i["delivered"] for i in st["items"]}
        ctx.check("sleep: backend delivered the pending item", auto.get(later["id"]) is True, auto)

    sl2 = wire.run(binary, card.root, ["sleep"])
    sl2_reqs = [r.start_line for r in sl2.requests() if "/device/v1/deliver" in r.start_line]
    st2 = tap.find("/device/v1/deliver")
    ctx.check("empty sleep: 404 with Content-Length", bool(st2) and st2[-1].status == 404
              and st2[-1].response is not None and st2[-1].response.header("Content-Length") is not None,
              [e.summary() for e in st2])
    ctx.check("empty sleep: no new file", not [p for p in sdmod.diff(sl.files, sl2.files).added
                                                if p.startswith("/Inbox/")], sorted(sdmod.diff(sl.files, sl2.files).added))
    lines = [ln for ln in outbox.read_text().splitlines() if ln.strip()] if outbox.exists() else []
    ctx.check("empty sleep: the line stays queued", len(lines) == 1, lines)

    dr = wire.run(binary, card.root, ["drain"])
    again = [r.start_line for r in dr.requests()]
    ctx.check("drain: retries the queued line with the same ev/ts", len(again) == 1 and again == sl2_reqs,
              {"drain": again, "queued_by": sl2_reqs})

    # The default plugin (no sleep delivery): the X3 confirms what it saved.
    card2 = wire.prepare_sd(ctx.out / "sd-confirm", ref=ctx.fw.ref, bundle=backend.bundle_zip(source=ctx.bundle_source))
    pdir2 = wire.plugin_dir(card2.root)
    conf = backend.seed(title="Wire confirm", text="Counted only once the X3 says it saved it.")
    c1 = wire.run(binary, card2.root, ["catalog", pdir2])
    row = c1.rows[0] if c1.rows else {}
    ctx.check("confirm: first row is \"Get 1 new\" with the new item", str(row.get("title", "")).startswith("Get 1 new · ")
              and conf["title"] in str(row.get("author", "")), c1.rows[:2])
    if row:
        d2 = wire.run(binary, card2.root, ["download", pdir2, row["id"]])
        dd = d2.download or {}
        url_ok = [r for r in d2.requests() if f"/device/v1/items/{row['id']}.epub?confirm=1" in r.start_line]
        ctx.check("confirm: the download asks with ?confirm=1 and lands", dd.get("result") == "OK" and bool(url_ok),
                  [r.start_line for r in d2.requests()])
        time.sleep(0.5)  # as above: the next list comes after the X3's restart
        emitted = d2.of("emit")
        ctx.check("confirm: book.downloaded queued with the saved path",
                  [e.get("path") for e in emitted] == [dd.get("dest")], emitted)
        c2 = wire.run(binary, card2.root, ["catalog", pdir2])
        ctx.check("confirm: not counted before the X3 reports it, and not offered again meanwhile",
                  bool(c2.rows) and c2.rows[0]["id"] == "none"
                  and "confirmed the next time" in c2.rows[0].get("author", c2.rows[0].get("subtitle", "")), c2.rows[:1])
        dr2 = wire.run(binary, card2.root, ["drain"])
        posts = [r for r in dr2.requests() if r.start_line.startswith("POST /device/v1/fetched?plugin=sendtox3 ")]
        ctx.check("confirm: the drain POSTs the saved path with the device token",
                  len(posts) == 1 and posts[0].body.decode("utf-8") == dd.get("dest")
                  and posts[0].header("Authorization") == token,
                  {"posts": [(r.start_line, r.body[:200], r.header("Authorization")) for r in dr2.requests()],
                   "dest": dd.get("dest"), "token": token})
        answered = [e for e in tap.find("/device/v1/fetched") if e.status == 200]
        ctx.check("confirm: the app answered 200", bool(answered), [e.summary() for e in tap.find("/device/v1/fetched")])
        out2 = sdmod.sd_path(card2.root, pdir2 + "/events.jsonl")
        ctx.check("confirm: outbox drained", not out2.exists() or not out2.read_text().strip(),
                  out2.read_text() if out2.exists() else "")
        c3 = wire.run(binary, card2.root, ["catalog", pdir2])
        ctx.check("confirm: then the first row is \"Nothing new right now\"",
                  bool(c3.rows) and c3.rows[0]["id"] == "none", c3.rows[:1])
        sleep2 = wire.run(binary, card2.root, ["sleep"])
        ctx.check("confirm: sleeping wants no Wi-Fi and sends nothing",
                  not sleep2.requests() and [e.get("wants_connect") for e in sleep2.of("emit")] == [False],
                  sleep2.of("emit"))
    ctx.dump_tap(tap, "backend_exchanges")
