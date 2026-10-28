"""S2: sleep.enter zero-tap delivery (2.x).

The device appends one ``sleep.enter`` line to ``<plugin>/events.jsonl`` on every sleep,
then drains the outbox if it can reach the network (``main.cpp`` enterDeepSleep ->
deliverSleepPluginEvents). Lines are processed in order; a non-2xx leaves the line
queued and the next drain retries it with the same ``{event.id}``/``{event.ts}``. A
drain also runs on every successful Wi-Fi join (``WifiSelectionActivity.cpp:546``).

Three boots on one card:

a. two items pending, sleep -> ``GET /device/v1/deliver`` 200 -> ``/Inbox/<ts>-<ev>.epub``
   (a two-chapter digest);
b. nothing pending, sleep -> 404 -> no file, the line stays queued;
c. one new item, sleep -> the queued line is retried with the same ev/ts -> 200 ->
   ``/Inbox/<ts_b>-<ev_b>.epub``; the new line gets 404 and stays queued.

On 2.0.0rc a sleep straight after boot cannot join Wi-Fi (``Firmware.cold_sleep_delivery``),
so each boot first opens Plugins > Send to X3 (the Wi-Fi join drains the queue: that is
where the 200/retry happens) and then sleeps from the catalog with Wi-Fi up (the new
line gets its 404 there). Step a additionally proves the cold-boot sleep sends nothing.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse

from e2e.sim import sd as sdmod
from e2e.sim.nav import Nav
from e2e.sim.runner import InputScript, SimRun, run_sim

from .base import Context, epub_info, scenario, valid_epub

AUTO_NAME = re.compile(r"^(\d+)-([0-9a-f]{8}-\d+)\.epub$")


def _boot(ctx: Context, card: sdmod.SdCard, name: str, *, via_catalog: bool) -> SimRun:
    s = InputScript(start_ms=3000)
    nav = Nav(ctx.fw, s)
    if via_catalog:
        nav.open_plugin("Send to X3", plugin_titles=["Send to X3"])
        s.shot("catalog")
    nav.sleep()
    s.shot("asleep")
    s.quit()
    return ctx.record_run(run_sim(ctx.program, card.root, s, ctx.out / name, name=name))


def _delivers(tap, after: int) -> list[tuple[object, str, str]]:
    out = []
    for e in tap.find("/device/v1/deliver")[after:]:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(e.target).query)
        out.append((e, (q.get("ev") or [""])[0], (q.get("ts") or [""])[0]))
    return out


def _check_landed(ctx: Context, card: sdmod.SdCard, label: str, ex, ev: str, ts: str, *,
                  chapters: int, title: str | None = None) -> None:
    ctx.check(f"{label}: deliver carried ev and ts", bool(re.fullmatch(r"[0-9a-f]{8}-\d+", ev) and ts.isdigit()),
              ex.target)
    ctx.check(f"{label}: deliver answered 200", ex.status == 200, ex.status)
    name = f"{ts}-{ev}.epub"
    files = card.inbox_files()
    ctx.check(f"{label}: /Inbox/{name} written", name in files, sorted(files))
    if name in files and ex.response:
        body = ex.response.body
        ctx.check(f"{label}: file equals the served EPUB", files[name].sha256 == hashlib.sha256(body).hexdigest())
        info = epub_info(body)
        ok = valid_epub(body) and info.get("chapters") == chapters and (title is None or info.get("title") == title)
        ctx.check(f"{label}: valid EPUB, {chapters} chapter(s)" + (f", title {title!r}" if title else ""), ok, info)


@scenario("S2", "sleep auto-delivery (200 digest, 404 keeps queue, retry)",
          firmwares=("2.0.0rc", "3f387a9", "develop", "fork"))
def run(ctx: Context) -> None:
    backend, tap = ctx.start_backend(extra_options={"auto_delivery": True})  # off by default
    token = f"Bearer {backend.device_token}"
    for t, txt in (("Morning digest one", "First pending article.\n\nSecond paragraph."),
                   ("Morning digest two", "Second pending article.")):
        backend.seed(title=t, text=txt)
    card = sdmod.build_sd(ctx.out / "sd", ctx.fw, bundle=backend.bundle_zip(source=ctx.bundle_source,
                                                                           auto_delivery=True))
    cold = ctx.fw.cold_sleep_delivery
    seen = 0

    # a) two pending
    run_a = _boot(ctx, card, "a-cold-sleep", via_catalog=False)
    ctx.check("a: power hold reached enterDeepSleep",
              bool(run_a.find(r"\[MAIN\] Power button held \d+ms, sleeping")) and run_a.entered("Sleep"),
              run_a.activities())
    da = _delivers(tap, seen)
    seen += len(da)
    if cold:
        ctx.check("a: one deliver request", len(da) == 1, [e.target for e, _, _ in da])
        if da:
            ctx.check("a: deliver sent the device token", da[0][0].request.header("Authorization") == token)
            _check_landed(ctx, card, "a", *da[0], chapters=2)
        ctx.check("a: outbox drained", card.outbox() == [], card.outbox())
    else:
        ctx.check("a: cold-boot sleep sent nothing (2.0.0rc: wifi.json not loaded at sleep)", not da,
                  [e.target for e, _, _ in da])
        ctx.check("a: the sleep.enter line is queued", len(card.outbox()) == 1, card.outbox())
        ctx.check("a: nothing written to /Inbox", not card.inbox_files(), sorted(card.inbox_files()))
        ctx.note("2.0.0rc: sleep right after boot never joins Wi-Fi (fixed upstream by facaa87); "
                 "the queued event is delivered at the next Wi-Fi join instead")
        queued_a = card.outbox()

        # a') open the catalog: the join drains the queued line -> 200; then sleep -> 404
        run_a2 = _boot(ctx, card, "a-catalog-then-sleep", via_catalog=True)
        da2 = _delivers(tap, seen)
        seen += len(da2)
        ctx.check("a': two deliver requests (join drain, then sleep drain)", len(da2) == 2,
                  [e.target for e, _, _ in da2])
        if da2 and queued_a:
            e0, ev0, ts0 = da2[0]
            ctx.check("a': join drain sent the queued ev/ts", (ev0, ts0) == (queued_a[0].get("id"),
                                                                            str(queued_a[0].get("ts"))),
                      [(ev0, ts0), queued_a[0]])
            ctx.check("a': deliver sent the device token", e0.request.header("Authorization") == token)
            _check_landed(ctx, card, "a'", e0, ev0, ts0, chapters=2)
        if len(da2) == 2:
            ctx.check("a': sleep drain (nothing pending) got 404", da2[1][0].status == 404, da2[1][0].status)
        ctx.check("a': the new line stays queued", len(card.outbox()) == 1, card.outbox())
        ctx.check("a': drain logs", bool(run_a2.find(r"drained \(stalled=0\)")) and
                  bool(run_a2.find(r"drained \(stalled=1\)")), run_a2.lines(tag="PEVT"))
    files_a = card.inbox_files()

    # b) nothing pending -> 404, nothing written, line kept
    run_b = _boot(ctx, card, "b-empty", via_catalog=not cold)
    db = _delivers(tap, seen)
    seen += len(db)
    expected_b = 1 if cold else 2  # 2.0.0rc: join drain retries the queued line, then sleep adds one
    ctx.check(f"b: {expected_b} deliver request(s), all 404", len(db) == expected_b and all(
        e.status == 404 and e.response is not None and e.response.header("Content-Length") is not None
        for e, _, _ in db), [e.summary() for e, _, _ in db])
    ctx.check("b: no new file in /Inbox", card.inbox_files() == files_a, sorted(card.inbox_files()))
    out_b = card.outbox()
    ctx.check(f"b: {expected_b} line(s) queued", len(out_b) == expected_b, out_b)
    ctx.check("b: drain logged stalled=1", bool(run_b.find(r"\[PEVT\] sendtox3: drained \(stalled=1\)")))

    # c) a new item -> the oldest queued line is retried with its ev/ts -> 200
    third = backend.seed(title="Arrived while asleep", text="Delivered on the retry of a queued event.")
    _boot(ctx, card, "c-retry", via_catalog=not cold)
    dc = _delivers(tap, seen)
    if out_b and dc:
        e0, ev0, ts0 = dc[0]
        ctx.check("c: first request retried the oldest queued ev/ts",
                  (ev0, ts0) == (out_b[0].get("id"), str(out_b[0].get("ts"))), [(ev0, ts0), out_b[0]])
        _check_landed(ctx, card, "c", e0, ev0, ts0, chapters=1, title=third["title"])
        ctx.check("c: every later request got 404", all(e.status == 404 for e, _, _ in dc[1:]) and len(dc) >= 2,
                  [e.status for e, _, _ in dc])
    else:
        ctx.check("c: retry happened", False, {"queued": out_b, "requests": [e.target for e, _, _ in dc]})
    ctx.check("c: lines after the 200 stay queued", len(card.outbox()) >= 1, card.outbox())
    names = sorted(card.inbox_files())
    ctx.check("/Inbox holds exactly the two deliveries", len(names) == 2 and all(AUTO_NAME.match(n) for n in names),
              names)
    ctx.dump_tap(tap, "backend_exchanges")
    st = backend.state()
    if st is not None:
        ctx.check("backend marked every item delivered.auto", all("auto" in i["delivered"] for i in st["items"]),
                  [i["delivered"] for i in st["items"]])
