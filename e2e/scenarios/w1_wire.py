"""W1: the device's own HTTP client (wire harness) against the backend.

The simulator replaces the device HTTP client with curl, so this scenario runs the
firmware's real PluginHttp / PluginEvents / HttpDownloader / SecureHttpClient code
over sockets (``e2e/wire``) to check the exact request bytes and that the backend's
responses frame correctly for that client: catalog browse, download of a row,
sleep-time delivery (200), then a drain with nothing pending (404, line kept).
"""

from __future__ import annotations

import hashlib
import json

from e2e.sim import sd as sdmod
from e2e.wire import wire

from .base import Context, SkipScenario, scenario, valid_epub


@scenario("W1", "wire harness: real device HTTP client against the backend", firmwares=("2.0.0rc", "3f387a9"))
def run(ctx: Context) -> None:
    try:
        binary = wire.find_binary(ctx.fw.ref)
    except FileNotFoundError as e:
        raise SkipScenario(str(e)) from None
    backend, tap = ctx.start_backend()
    item = backend.seed(title="Wire check", text="Exact bytes on the wire.\n\nSecond paragraph.")
    card = wire.prepare_sd(ctx.out / "sd", ref=ctx.fw.ref, bundle=backend.bundle_zip(source=ctx.bundle_source))
    pdir = wire.plugin_dir(card.root)
    token = f"Bearer {backend.device_token}"

    cat = wire.run(binary, card.root, ["catalog", pdir])
    (ctx.out / "catalog.json").write_text(json.dumps(cat.to_json(), indent=2), encoding="utf-8")
    ctx.artifact("catalog", ctx.out / "catalog.json")
    ctx.check("catalog: browse 200 and parsed", cat.returncode == 0 and (cat.browse or {}).get("status") == 200,
              cat.browse)
    ctx.check("catalog: the seeded row is listed", [r["id"] for r in cat.rows] == [item["id"]], cat.rows)
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

    # A second item so the sleep delivery has something pending (the first was fetched).
    later = backend.seed(title="Wire sleep delivery", text="Delivered by pluginevents::drain.")
    sl = wire.run(binary, card.root, ["sleep"])
    reqs = [r for r in sl.requests() if "/device/v1/deliver" in r.start_line]
    ctx.check("sleep: one deliver request with ev and ts", len(reqs) == 1 and "ev=" in reqs[0].start_line
              and "&ts=" in reqs[0].start_line, [r.start_line for r in sl.requests()])
    ctx.check("sleep: deliver headers", bool(reqs) and reqs[0].header("Authorization") == token
              and reqs[0].header("User-Agent") == "CrossPoint", [r.headers for r in reqs])
    new = sdmod.diff(cat.files, sl.files).added
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
    ctx.dump_tap(tap, "backend_exchanges")
