"""Regression tests for SSRF-guard fixes in extract/fetch.py (review findings 4 and 45).

4: IP literals written with full-width digits or ideographic dots skipped the literal
   check (an ASCII-only regex) and aiohttp, which treats yarl's normalised host as an
   IP literal, never asked the guarded resolver: the fetch reached 127.0.0.1.
45: a LAN device can have a global IPv6 address, so guarded fetches are IPv4-only.
"""

from __future__ import annotations

import asyncio
import socket
import time

import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver

from sendtox3.extract import fetch as fetch_mod
from sendtox3.extract.fetch import FetchError, fetch_url


@pytest.fixture
async def server(aiohttp_server):
    app = web.Application()
    app["hits"] = []

    async def page(request):
        app["hits"].append(request.path_qs)
        return web.Response(text="<html><body><p>private page</p></body></html>", content_type="text/html")

    app.router.add_get("/private", page)
    return await aiohttp_server(app, host="127.0.0.1")


@pytest.mark.parametrize(
    "host",
    [
        "１２７.０.０.１",  # full-width digits
        "127。0。0。1",  # ideographic full stops
        "１２７。０。０。１",  # both
        "①②⑦.0.0.1",  # circled digits (NFKC -> 127)
    ],
)
async def test_unicode_ip_literal_to_loopback_is_blocked(server, host):
    target = f"http://{host}:{server.port}/private"
    with pytest.raises(FetchError, match="private network address"):
        await fetch_url(target, timeout=5)
    assert server.app["hits"] == []  # refused before connecting


async def test_unicode_ip_literal_in_redirect_is_blocked(monkeypatch):
    """A malicious page bounces the fetch to a LAN address written in full-width digits.

    A raw server is used because aiohttp's own server would normalise the Location
    header; a real attacker sends the UTF-8 bytes as they are.
    """
    hits: list[bytes] = []
    location = "http://\uff11\uff10.\uff10.\uff10.\uff11/admin".encode()  # １０.０.０.１ = 10.0.0.1

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        hits.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 302 Found\r\nLocation: " + location + b"\r\nContent-Length: 0\r\n"
                     b"Connection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    real = fetch_mod.is_blocked_ip
    # the redirecting server (127.0.0.1) counts as public for this test
    monkeypatch.setattr(fetch_mod, "is_blocked_ip", lambda ip: False if ip == "127.0.0.1" else real(ip))
    t0 = time.monotonic()
    try:
        with pytest.raises(FetchError, match="private network address"):
            await fetch_url(f"http://127.0.0.1:{port}/bounce", timeout=2)
    finally:
        srv.close()
        await srv.wait_closed()
    assert len(hits) == 1
    assert time.monotonic() - t0 < 1  # 10.0.0.1 was never dialled


async def test_connector_checks_ip_literals_even_if_the_hop_check_is_bypassed(server, monkeypatch):
    """Defence in depth: aiohttp resolves IP literals without the resolver, so the
    connector itself must check them (this also fails if aiohttp renames the hook)."""
    monkeypatch.setattr(fetch_mod, "_check_hop", lambda url, allow_private: None)
    with pytest.raises(FetchError, match="private network address"):
        await fetch_url(f"http://127.0.0.1:{server.port}/private", timeout=5)
    assert server.app["hits"] == []


async def test_allow_private_still_reaches_loopback(server):
    res = await fetch_url(f"http://127.0.0.1:{server.port}/private", allow_private=True, timeout=5)
    assert res.status == 200 and b"private page" in res.body


# --------------------------------------------------------------------------------------
# 45: global IPv6 addresses of LAN devices
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "http://[2a00:1450:4001:80b::200e]/",  # global unicast: could be the user's NAS
        "http://[2606:4700:4700::1111]:8080/admin",
        "http://[::ffff:8.8.8.8]/",
    ],
)
async def test_ipv6_literals_are_refused_when_guarded(target):
    t0 = time.monotonic()
    with pytest.raises(FetchError, match="IPv6 address"):
        await fetch_url(target, timeout=5)
    assert time.monotonic() - t0 < 1  # no connection attempt


async def test_private_ipv6_literal_keeps_the_private_reason():
    with pytest.raises(FetchError, match="private network address"):
        await fetch_url("http://[fd00::1]/", timeout=5)


class _RecordingResolver(AbstractResolver):
    def __init__(self, answers: dict[str, list[str]]):
        self.answers = answers
        self.families: list[int] = []

    async def resolve(self, host, port=0, family=socket.AF_INET):
        self.families.append(family)
        return [
            {
                "hostname": host,
                "host": ip,
                "port": port,
                "family": socket.AF_INET6 if ":" in ip else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for ip in self.answers.get(host, [])
        ]

    async def close(self):
        pass


async def test_guarded_dns_lookups_ask_for_ipv4_only(server, monkeypatch):
    resolver = _RecordingResolver({"public.test": ["127.0.0.1"]})
    monkeypatch.setattr(fetch_mod, "_make_resolver", lambda: resolver)
    real = fetch_mod.is_blocked_ip
    monkeypatch.setattr(fetch_mod, "is_blocked_ip", lambda ip: False if ip == "127.0.0.1" else real(ip))
    res = await fetch_url(f"http://public.test:{server.port}/private", timeout=5)
    assert res.status == 200
    assert resolver.families and set(resolver.families) == {socket.AF_INET}


async def test_ipv6_dns_answers_are_dropped_when_guarded(monkeypatch):
    resolver = _RecordingResolver({"nas.example": ["2a00:1450:4001:80b::200e"]})
    monkeypatch.setattr(fetch_mod, "_make_resolver", lambda: resolver)
    # dropped, and the reason names IPv6 (see test_fix2_B_fetch.py)
    with pytest.raises(FetchError, match="only has an IPv6 address"):
        await fetch_url("http://nas.example/", timeout=5)
