"""Entry point: ``python -m sendtox3`` (the HA app's run.sh execs this).

Loads options, opens the store, resolves tokens, starts the web server on
``0.0.0.0:<port>`` and the enabled background workers (each crash-isolated and
restarted with backoff), redoes shares a crash or restart left unfinished, keeps
retrying the server-address detection while it is only a guess, prunes old items
daily, and shuts down cleanly on SIGTERM.

``python -m sendtox3 --show-tokens`` prints the tokens (for plain-docker setups
without Home Assistant ingress) and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import signal
import sys
import time
from typing import Awaitable, Callable, Optional

from aiohttp import web

from . import __version__
from .app_state import AppState, resolve_secrets
from .config import Options, load_options
from .ingest import Processor
from .plugin_bundle import (INVALID_SERVER_HOST, device_token_problem, looks_container_internal, resolve_base_url,
                            under_supervisor)
from .store import Store
from .web import ingest_api
from .web.app import AccessLogger, create_app

log = logging.getLogger("sendtox3")

_LEVELS = {
    "trace": logging.DEBUG, "debug": logging.DEBUG, "info": logging.INFO, "notice": logging.INFO,
    "warning": logging.WARNING, "error": logging.ERROR, "fatal": logging.CRITICAL, "critical": logging.CRITICAL,
}

BACKOFF_MIN = 5.0
BACKOFF_MAX = 300.0
#: A worker that ran this long before failing is considered healthy again (backoff resets).
HEALTHY_RUN_SECONDS = 600.0
PRUNE_FIRST_DELAY = 60.0
PRUNE_INTERVAL = 86_400.0
SHUTDOWN_GRACE = 10.0
#: How long shares still being processed may finish at shutdown (the rest are redone at
#: the next start from the processor's spool). Runs alongside the worker shutdown.
BACKGROUND_GRACE = 5.0
#: Retry delays of the server-address detection while it is only a guess.
BASE_URL_RETRY_MIN = 5.0
BASE_URL_RETRY_MAX = 300.0


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=_LEVELS.get((level or "info").lower(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def worker_plan(opts: Options) -> list[tuple[str, str]]:
    """(status name, module) of the workers to start for these options."""
    plan = []
    if opts.imap_enabled:
        plan.append(("imap", "sendtox3.workers.imap"))
    if opts.ha_share_enabled and (os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")):
        plan.append(("ha_share", "sendtox3.workers.ha_share"))
    if opts.device_host:
        plan.append(("lan_push", "sendtox3.workers.lan_push"))
    return plan


async def supervise(name: str, run: Callable[[AppState], Awaitable[None]], state: AppState, *,
                    backoff_min: float = BACKOFF_MIN, backoff_max: float = BACKOFF_MAX,
                    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Run a worker forever: log crashes, restart with exponential backoff."""
    delay = backoff_min
    while True:
        started = time.monotonic()
        try:
            await run(state)
            st = state.status.get(name)
            if isinstance(st, dict) and st.get("enabled") is False:
                # Workers return on purpose when they have nothing to do (e.g. not running
                # under Home Assistant); restarting them would only spam the log.
                log.info("worker %s is disabled%s", name, f": {st['reason']}" if st.get("reason") else "")
                return
            log.warning("worker %s returned; restarting in %.0f s", name, delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - crash isolation
            log.exception("worker %s crashed; restarting in %.0f s", name, delay)
            state.set_status(name, "error", f"crashed: {type(exc).__name__}: {exc}"[:300])
        if time.monotonic() - started >= HEALTHY_RUN_SECONDS:
            delay = backoff_min
        await sleep(delay)
        delay = min(backoff_max, delay * 2)


async def run_worker_module(name: str, module: str, state: AppState) -> None:
    try:
        mod = importlib.import_module(module)
        run = getattr(mod, "run")
    except (ImportError, AttributeError) as exc:
        log.error("worker %s is unavailable: %s", name, exc)
        state.set_status(name, "error", f"not available: {exc}")
        return
    state.set_status(name, "starting", "")
    await supervise(name, run, state)


async def prune_daily(state: AppState, *, first_delay: float = PRUNE_FIRST_DELAY,
                      interval: float = PRUNE_INTERVAL) -> None:
    await asyncio.sleep(first_delay)
    while True:
        try:
            n = await asyncio.to_thread(state.store.prune, state.opts.retention_days)
            if n:
                log.info("pruned %d item(s) older than %d days", n, state.opts.retention_days)
        except Exception:  # noqa: BLE001
            log.exception("pruning failed")
        await asyncio.sleep(interval)


def base_url_is_a_guess(state: AppState) -> bool:
    """True while the address written into the X3's bundle may be wrong and a later
    detection could do better: nothing found yet, or (under Home Assistant) only the
    container's own address because the Supervisor did not answer at start."""
    if state.base_url_source in ("options", "supervisor", INVALID_SERVER_HOST):
        return False
    if not state.base_url:
        return True
    return state.base_url_source == "local address" and under_supervisor()


async def refresh_base_url(state: AppState, *, delay_min: Optional[float] = None,
                           delay_max: Optional[float] = None,
                           sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Retry :func:`resolve_base_url` with backoff until the address is no longer a guess."""
    delay = BASE_URL_RETRY_MIN if delay_min is None else delay_min
    cap = BASE_URL_RETRY_MAX if delay_max is None else delay_max
    while base_url_is_a_guess(state):
        await sleep(delay)
        delay = min(cap, delay * 2)
        try:
            url, how = await resolve_base_url(state.opts)
        except Exception:  # noqa: BLE001 - keep retrying
            log.exception("detecting the server address failed")
            continue
        if url and (how != state.base_url_source or not state.base_url):
            state.base_url, state.base_url_source = url, how
            log.info("server address for the X3 and phones is now %s (%s)", url, how)


async def recover_spool(state: AppState) -> None:
    """Redo shares that a crash or restart interrupted (``Processor.recover_pending``)."""
    recover = getattr(state.processor, "recover_pending", None)
    if recover is None:
        return
    try:
        n = await recover()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the spool is kept and retried at the next start
        log.exception("could not redo the shares left over from the last run")
        return
    if n:
        log.info("redid %d share(s) left over from the last run", n)


def _log_startup(state: AppState, configured: dict[str, bool], host: str, port: int) -> None:
    opts = state.opts
    log.info("Send to X3 %s listening on %s:%d (data in %s, %d items)", __version__, host, port,
             state.store.dir, state.store.count())
    log.info("tokens: %s (values are on the admin Setup page; never logged)",
             ", ".join(f"{n.removesuffix('_token')}={'configured' if c else 'generated'}"
                       for n, c in configured.items()))
    if configured.get("device_token"):
        problem = device_token_problem(opts.device_token)
        if problem:
            log.warning("the device_token option cannot be used by the X3 plugin (%s); change it in the options",
                        problem)
    base = state.base_url
    if not base:
        if state.base_url_source == INVALID_SERVER_HOST:
            log.warning("the server_host option is invalid; correct it (for example 192.168.1.50 or "
                        "192.168.1.50:8765) and restart")
        else:
            log.warning("could not detect this server's LAN address; set server_host in the options")
        base = f"http://<server>:{opts.port}"
    else:
        log.info("server address for the X3 and phones: %s (%s)", base, state.base_url_source)
        if state.base_url_source == "local address" and looks_container_internal(base):
            if under_supervisor():
                log.warning("%s is an address inside Docker that the X3 cannot reach; set server_host", base)
            else:
                log.warning("%s is in 172.16.0.0/12: if this app runs in a Docker container that is an "
                            "address the X3 cannot reach, so set server_host (ignore this if your LAN "
                            "really uses it)", base)
    log.info("share endpoint: POST %s/api/v1/ingest (Authorization: Bearer <ingest token>)", base)
    log.info("CrossPoint 2.x plugin: download the SD-card bundle on the admin Setup page "
             "(catalog %s/device/v1/catalog)", base)
    log.info("CrossPoint 1.6.5 OPDS: %s/opds (user x3, password = device token)", base)
    log.info("admin UI: Home Assistant sidebar (ingress) or %s/admin?token=<admin token>", base)


async def serve(opts: Options, *, host: Optional[str] = None, stop: Optional[asyncio.Event] = None,
                started: Optional[Callable[[AppState, int], None]] = None,
                install_signals: bool = True) -> None:
    """Run until SIGTERM/SIGINT (or ``stop`` is set). ``started(state, port)`` is called
    once the site is listening (tests bind 127.0.0.1 with ``opts.port = 0``)."""
    host = host or os.environ.get("SENDTOX3_BIND", "0.0.0.0")
    stop = stop or asyncio.Event()
    store = Store(opts.data_dir)
    configured = resolve_secrets(opts, store)
    processor = Processor(store, opts)
    base_url, how = await resolve_base_url(opts)
    state = AppState(opts=opts, store=store, processor=processor, base_url=base_url, base_url_source=how,
                     configured_secrets=configured)
    app = create_app(opts, store, processor, state=state)
    runner = web.AppRunner(app, access_log_class=AccessLogger, handle_signals=False, shutdown_timeout=5.0)
    tasks: list[asyncio.Task] = []
    loop = asyncio.get_running_loop()
    signals_installed: list[int] = []
    try:
        await runner.setup()
        site = web.TCPSite(runner, host, opts.port)
        await site.start()
        port = opts.port
        for addr in runner.addresses:  # the real port when opts.port is 0 (tests)
            if isinstance(addr, tuple) and len(addr) >= 2:
                port = int(addr[1])
                break
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, stop.set)
                    signals_installed.append(sig)
                except (NotImplementedError, RuntimeError):
                    pass
        _log_startup(state, configured, host, port)
        for name, label in (("imap", opts.imap_enabled), ("ha_share", opts.ha_share_enabled),
                            ("lan_push", bool(opts.device_host))):
            if not label:
                state.set_status(name, "disabled", "")
        for name, module in worker_plan(opts):
            tasks.append(asyncio.create_task(run_worker_module(name, module, state), name=f"worker-{name}"))
        tasks.append(asyncio.create_task(prune_daily(state), name="prune"))
        tasks.append(asyncio.create_task(recover_spool(state), name="recover"))
        if base_url_is_a_guess(state):
            tasks.append(asyncio.create_task(refresh_base_url(state), name="base-url"))
        if started is not None:
            started(state, port)
        await stop.wait()
        log.info("shutting down")
    finally:
        for sig in signals_installed:
            loop.remove_signal_handler(sig)
        for t in tasks:
            t.cancel()

        async def stop_tasks() -> None:
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE)
                for t in pending:
                    log.warning("task %s did not stop within %.0f s", t.get_name(), SHUTDOWN_GRACE)

        await asyncio.gather(stop_tasks(), ingest_api.drain_background(BACKGROUND_GRACE))
        await runner.cleanup()
        store.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="sendtox3", description="Send to X3 server")
    parser.add_argument("--options", help="path to options.json (default: $SENDTOX3_DATA_DIR/options.json)")
    parser.add_argument("--show-tokens", action="store_true", help="print the tokens and exit")
    args = parser.parse_args(argv)
    opts = load_options(args.options)
    setup_logging(opts.log_level)
    if args.show_tokens:
        store = Store(opts.data_dir)
        try:
            resolve_secrets(opts, store)
        finally:
            store.close()
        for name in ("ingest_token", "device_token", "admin_token"):
            print(f"{name}: {getattr(opts, name)}")
        return 0
    try:
        asyncio.run(serve(opts))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
