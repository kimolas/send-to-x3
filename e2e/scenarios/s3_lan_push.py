"""S3: LAN push into the firmware's own File Transfer web server (1.6.5 and 2.x).

Home > File Transfer > Join a Network auto-joins the saved network and starts the real
``CrossPointWebServer`` (the simulator serves it on 127.0.0.1:<port>). The backend's
LAN-push worker is pointed at a wire tap in front of that port; it must find the
device (``/api/status``), make sure ``/Inbox`` exists (``/mkdir``), upload one file per
request (``/upload?path=/Inbox``) and never trip over the firmware's refusal to
overwrite (``CrossPointWebServer.cpp`` /upload: 400 "File already exists"): a different
file with the first item's name is already on the card, so it must land as
``<title> (2).<ext>`` and the old file must survive.

Runs twice: ``delivery_format: epub`` (the app's default) and ``txt``.
"""

from __future__ import annotations

import hashlib
import json

from e2e.sim import runner, sd as sdmod
from e2e.sim.nav import Nav
from e2e.sim.runner import InputScript, run_sim

from .base import Context, epub_info, scenario
from .wiretap import WireTap

OLD_COPY = b"An older, different file with the same name, already on the card.\n"


def _push(ctx: Context, fmt: str) -> None:
    sim_port = runner.free_port(pair=True)
    dev_tap = WireTap(sim_port, name=f"device-tap-{fmt}").start()
    ctx.defer(dev_tap.stop)
    backend, _tap = ctx.start_backend(f"backend-{fmt}", device_host=f"127.0.0.1:{dev_tap.port}",
                                      push_poll_seconds=1, delivery_format=fmt)
    a = backend.seed(title="Notes from the bus", text="Pushed over the LAN.\n\nSecond paragraph, “quoted”.")
    b = backend.seed(title="A recipe: lentil soup", text="Lentils, onion, cumin.\n\nSimmer 30 minutes.")

    card = sdmod.build_sd(ctx.out / f"sd-{fmt}", ctx.fw, files={f"/Inbox/{a['title']}.{fmt}": OLD_COPY})
    expected = {f"{b['title']}.{fmt}", f"{a['title']} (2).{fmt}"}

    s = InputScript()
    nav = Nav(ctx.fw, s)
    nav.file_transfer_join()
    s.shot("file-transfer")
    s.wait(25000)
    s.quit()
    run_ = ctx.record_run(run_sim(
        ctx.program, card.root, s, ctx.out / f"run-{fmt}", name=f"file-transfer-{fmt}", http_port=sim_port,
        stop_when=lambda: expected <= set(card.inbox_files()), stop_grace_ms=2500))
    ctx.dump_tap(dev_tap, f"device_exchanges_{fmt}")

    p = f"{fmt}: "
    ctx.check(p + "Join a Network path ran", run_.entered("CrossPointWebServer") and run_.entered("NetworkModeSelection")
              and run_.entered("WifiSelection"), run_.activities())
    ctx.check(p + "firmware web server started", bool(run_.find(r"\[WEB\] Web server started on port")),
              run_.lines(tag="WEB")[-5:])
    status = [e for e in dev_tap.find("/api/status", "GET") if e.status == 200]
    ctx.check(p + "backend polled /api/status", bool(status), [e.target for e in dev_tap.exchanges()][:10])
    if status and status[0].response:
        try:
            st = json.loads(status[0].response.body)
        except ValueError:
            st = {}
        ctx.check(p + "device reports this firmware version", st.get("version") == ctx.fw.version, st)
    mk = dev_tap.find("/mkdir", "POST")
    listed = dev_tap.find("/api/files", "GET")
    ctx.check(p + "backend made sure /Inbox exists (/mkdir or /api/files)",
              (bool(mk) and all(e.status in (200, 400) for e in mk)) or any(e.status == 200 for e in listed),
              [(e.status, e.response.body[:80] if e.response else None) for e in mk])
    ups = dev_tap.find("/upload", "POST")
    ok_ups = [e for e in ups if e.status == 200]
    ctx.check(p + "two uploads accepted (one file per request)", len(ok_ups) == 2,
              [(e.target, e.status, e.response.body[:80] if e.response else None) for e in ups])

    inbox = card.inbox_files()
    ctx.check(p + "both pushed files are on the card", expected <= set(inbox), sorted(inbox))
    old = inbox.get(f"{a['title']}.{fmt}")
    ctx.check(p + "the existing file was not overwritten",
              old is not None and old.sha256 == hashlib.sha256(OLD_COPY).hexdigest())
    for item, name in ((b, f"{b['title']}.{fmt}"), (a, f"{a['title']} (2).{fmt}")):
        want = backend.item_bytes(item["id"], fmt)
        got = inbox.get(name)
        ok = got is not None and got.sha256 == hashlib.sha256(want).hexdigest()
        how = "sha256"
        if not ok and got is not None and fmt == "epub":
            # A backend may re-render EPUBs (fresh dcterms:modified): compare metadata instead.
            how = "epub title"
            ok = epub_info(card.path(f"/Inbox/{name}").read_bytes()).get("title") == epub_info(want).get("title")
        ctx.check(p + f"{name} matches the app's {fmt}", ok,
                  {"compared": how, "card": got.size if got else None, "app": len(want)})
    st = backend.state()
    if st is not None:
        pushed = {i["id"]: "push" in i["delivered"] for i in st["items"]}
        ctx.check(p + "backend marked both items delivered.push", all(pushed.values()) and len(pushed) == 2, pushed)


@scenario("S3", "LAN push into the File Transfer server (epub and txt)",
          firmwares=("2.0.0rc", "1.6.5", "develop", "fork"))
def run(ctx: Context) -> None:
    for fmt in ("epub", "txt"):
        _push(ctx, fmt)
