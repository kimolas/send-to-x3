"""Round-2 regression test for extract/fetch.py (R2-B5).

Guarded fetches use IPv4 only. A host whose DNS has only IPv6 answers got "Could not
find the server" (the IPv4 lookup fails) or "local or private network address" (a
resolver that returns the IPv6 answers anyway), neither of which names the cause.
"""

from __future__ import annotations

import asyncio
import socket

import aiohttp
import pytest
from aiohttp.abc import AbstractResolver

from sendtox3.extract import fetch as fetch_mod
from sendtox3.extract.fetch import FetchError, fetch_url

IPV6_ONLY = "only has an IPv6 address; Send to X3 fetches over IPv4 only"
PUBLIC_V6 = "2a00:1450:4001:80b::200e"


class _DualStackResolver(AbstractResolver):
    """Separate IPv4 and IPv6 answers; a family without answers fails like DNS does."""

    def __init__(self, v4: dict[str, list[str]], v6: dict[str, list[str]]):
        self.v4, self.v6 = v4, v6
        self.asked: list[tuple[str, int]] = []

    async def resolve(self, host, port=0, family=socket.AF_INET):
        self.asked.append((host, family))
        table = self.v6 if family == socket.AF_INET6 else self.v4
        if not table.get(host):
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
        return [
            {"hostname": host, "host": ip, "port": port, "family": family, "proto": 0, "flags": socket.AI_NUMERICHOST}
            for ip in table[host]
        ]

    async def close(self):
        pass


@pytest.fixture
def dns(monkeypatch):
    resolver = _DualStackResolver({}, {})
    monkeypatch.setattr(fetch_mod, "_make_resolver", lambda: resolver)
    return resolver


async def test_ipv6_only_host_gets_a_clear_reason(dns):
    dns.v6["v6only.example"] = [PUBLIC_V6]
    with pytest.raises(FetchError, match=IPV6_ONLY):
        await fetch_url("http://v6only.example/article", timeout=5)
    assert dns.asked == [("v6only.example", socket.AF_INET), ("v6only.example", socket.AF_INET6)]


async def test_ipv6_only_reason_reaches_the_document(dns, opts):
    import dataclasses

    from sendtox3.extract.html import url_to_document

    dns.v6["v6only.example"] = [PUBLIC_V6]
    doc = await url_to_document("http://v6only.example/a", opts=dataclasses.replace(opts, allow_private_urls=False))
    assert doc.blocks == [] and any(IPV6_ONLY in n for n in doc.notes)


async def test_missing_host_still_says_not_found(dns):
    with pytest.raises(FetchError, match="Could not find the server nowhere.example"):
        await fetch_url("http://nowhere.example/", timeout=5)


async def test_private_ipv6_only_host_keeps_the_dns_failure(dns):
    """No *public* IPv6 address: the IPv6 explanation would be wrong."""
    dns.v6["nas.example"] = ["fd00::5"]
    with pytest.raises(FetchError, match="Could not find the server"):
        await fetch_url("http://nas.example/", timeout=5)


async def test_resolver_returning_only_public_ipv6_names_ipv6(dns):
    dns.v4["v6answers.example"] = [PUBLIC_V6]  # e.g. a resolver that ignores the family
    with pytest.raises(FetchError, match=IPV6_ONLY):
        await fetch_url("http://v6answers.example/", timeout=5)


@pytest.mark.parametrize("answers", [["fd00::5"], ["10.0.0.7", PUBLIC_V6], ["::1"]])
async def test_private_answers_keep_the_private_reason(dns, answers):
    dns.v4["lan.example"] = answers
    with pytest.raises(FetchError, match="private network address"):
        await fetch_url("http://lan.example/", timeout=5)


async def test_real_resolver_probe_ignores_ai_addrconfig(monkeypatch):
    """aiohttp's ThreadedResolver passes AI_ADDRCONFIG, which hides AAAA answers from a
    container without IPv6; the explanatory lookup must not depend on it."""
    loop = asyncio.get_running_loop()
    seen: list[tuple[int, int]] = []

    async def getaddrinfo(host, port, *, family=0, type=0, proto=0, flags=0):
        seen.append((family, flags))
        if host == "v6only.example" and family == socket.AF_INET6 and not flags & socket.AI_ADDRCONFIG:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (PUBLIC_V6, port, 0, 0))]
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(fetch_mod, "_make_resolver", lambda: aiohttp.ThreadedResolver())
    with pytest.raises(FetchError, match=IPV6_ONLY):
        await fetch_url("http://v6only.example/", timeout=5)
    assert seen[0][0] == socket.AF_INET and (socket.AF_INET6, 0) in seen

