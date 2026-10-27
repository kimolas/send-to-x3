"""Fake Home Assistant websocket API, as an app sees it through the Supervisor proxy.

Implements the parts of https://developers.home-assistant.io/docs/api/websocket the
Send to X3 worker uses:

- ``auth_required`` -> ``auth`` -> ``auth_ok`` / ``auth_invalid`` (then close),
- ``subscribe_events`` / ``unsubscribe_events`` with ``result`` replies,
- ``ping`` -> ``pong``,
- ``config/device_registry/list``,
- ``{"id": <sub>, "type": "event", "event": {...}}`` pushes via :meth:`fire_event`,
- HA's rule that message ids must increase (``id_reuse`` error otherwise) and
  ``unknown_command`` for anything else.

Controls: ``token`` (change it to make auth fail/succeed), ``answer_pings``,
:meth:`drop_connections` (HA restart), :meth:`stop`/:meth:`start` on the same port,
and counters (``auth_attempts``, ``auth_failures``, ``connections_total``) plus
``protocol_errors`` (client mistakes, e.g. non-increasing ids).

Usage::

    async with FakeHomeAssistant(token="t") as ha:
        monkeypatch.setenv("SENDTOX3_HA_WS_URL", ha.url)
        await ha.wait_subscribed()
        await ha.fire_event("mobile_app.share", {"url": "https://example.org/a"})
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from aiohttp import WSMsgType, web


class FakeHomeAssistant:
    def __init__(
        self,
        *,
        token: str = "supervisor-test-token",
        ha_version: str = "2026.10.4",
        path: str = "/core/websocket",
        devices: Optional[list[dict[str, Any]]] = None,
        answer_pings: bool = True,
    ):
        self.token = token
        self.ha_version = ha_version
        self.path = path
        self.devices = devices if devices is not None else []
        self.answer_pings = answer_pings
        self.auth_attempts = 0
        self.auth_failures = 0
        self.connections_total = 0
        self.pings = 0
        self.protocol_errors: list[str] = []
        self.received: list[dict[str, Any]] = []
        self._subs: dict[web.WebSocketResponse, dict[int, str]] = {}
        self._open: set[web.WebSocketResponse] = set()
        self._sub_event = asyncio.Event()
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self.port = 0

    # ------------------------------------------------------------ lifecycle

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.path}"

    async def __aenter__(self) -> "FakeHomeAssistant":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self, port: Optional[int] = None) -> None:
        app = web.Application()
        app.router.add_get(self.path, self._handler)
        self._runner = web.AppRunner(app, handle_signals=False, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", port if port is not None else self.port,
                                 reuse_address=True)
        await self._site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        await self.drop_connections()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def drop_connections(self, code: int = 1001) -> None:
        """Close every websocket (what clients see when HA Core restarts)."""
        for ws in list(self._open):
            await ws.close(code=code, message=b"restarting")
        self._subs.clear()
        self._sub_event.clear()

    # -------------------------------------------------------------- helpers

    @property
    def subscriber_count(self) -> int:
        return sum(1 for subs in self._subs.values() for t in subs.values() if t == "mobile_app.share")

    async def wait_subscribed(self, timeout: float = 5.0, count: int = 1) -> None:
        async def _wait() -> None:
            while self.subscriber_count < count:
                self._sub_event.clear()
                await self._sub_event.wait()

        await asyncio.wait_for(_wait(), timeout)

    async def fire_event(
        self, event_type: str, data: dict[str, Any], *, user_id: Optional[str] = None, coalesce: bool = False
    ) -> int:
        """Push an event to every matching subscription; returns how many got it.

        ``coalesce`` sends it inside a JSON array, as HA does for clients that enabled
        the ``coalesce_messages`` feature.
        """
        event = {
            "event_type": event_type,
            "data": data,
            "origin": "REMOTE",
            "time_fired": datetime.now(timezone.utc).isoformat(),
            "context": {"id": secrets.token_hex(13).upper(), "parent_id": None, "user_id": user_id},
        }
        sent = 0
        for ws, subs in list(self._subs.items()):
            for sub_id, etype in subs.items():
                if etype in (event_type, "*") and not ws.closed:
                    msg: Any = {"id": sub_id, "type": "event", "event": event}
                    await ws.send_str(json.dumps([msg] if coalesce else msg))
                    sent += 1
        return sent

    # -------------------------------------------------------------- protocol

    async def _handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(autoping=True)
        await ws.prepare(request)
        self.connections_total += 1
        self._open.add(ws)
        try:
            await ws.send_json({"type": "auth_required", "ha_version": self.ha_version})
            if not await self._auth(ws):
                return ws
            self._subs[ws] = {}
            last_id = 0
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    self.protocol_errors.append("invalid json")
                    continue
                for m in data if isinstance(data, list) else [data]:
                    self.received.append(m)
                    mid = m.get("id")
                    if not isinstance(mid, int) or mid <= last_id:
                        self.protocol_errors.append(f"id_reuse: {mid!r} after {last_id}")
                        await ws.send_json({"id": mid, "type": "result", "success": False,
                                            "error": {"code": "id_reuse",
                                                      "message": "Identifier values have to increase."}})
                        continue
                    last_id = mid
                    await self._command(ws, mid, m)
        finally:
            self._open.discard(ws)
            self._subs.pop(ws, None)
        return ws

    async def _auth(self, ws: web.WebSocketResponse) -> bool:
        try:
            msg = await ws.receive(timeout=10)
        except asyncio.TimeoutError:
            await ws.close()
            return False
        if msg.type != WSMsgType.TEXT:
            return False
        self.auth_attempts += 1
        try:
            data = json.loads(msg.data)
        except ValueError:
            data = {}
        if data.get("type") != "auth" or data.get("access_token") != self.token:
            self.auth_failures += 1
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token or password"})
            await ws.close()
            return False
        await ws.send_json({"type": "auth_ok", "ha_version": self.ha_version})
        return True

    async def _command(self, ws: web.WebSocketResponse, mid: int, m: dict[str, Any]) -> None:
        mtype = m.get("type")
        if mtype == "subscribe_events":
            self._subs[ws][mid] = m.get("event_type") or "*"
            await ws.send_json({"id": mid, "type": "result", "success": True, "result": None})
            self._sub_event.set()
        elif mtype == "unsubscribe_events":
            sub = m.get("subscription")
            ok = isinstance(sub, int) and self._subs[ws].pop(sub, None) is not None
            await ws.send_json({"id": mid, "type": "result", "success": ok, "result": None})
        elif mtype == "ping":
            self.pings += 1
            if self.answer_pings:
                await ws.send_json({"id": mid, "type": "pong"})
        elif mtype == "config/device_registry/list":
            await ws.send_json({"id": mid, "type": "result", "success": True, "result": self.devices})
        else:
            await ws.send_json({"id": mid, "type": "result", "success": False,
                                "error": {"code": "unknown_command", "message": "Unknown command."}})
