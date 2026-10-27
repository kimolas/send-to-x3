"""Safe fetcher (SSRF guard, redirects, caps, timeouts), URL cleaning, decoding, block detection.

Fetch tests run against a local aiohttp fixture server on 127.0.0.1. Tests that need
the SSRF guard *on* while still talking to that server treat 127.0.0.1 as "public" by
patching :func:`sendtox3.extract.fetch.is_blocked_ip`, and simulate DNS answers with a
fake resolver, so no real DNS or internet access is involved.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import re
import socket
import time
import zipfile
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver

from sendtox3.extract import fetch as fetch_mod
from sendtox3.extract.fetch import (
    FetchError,
    clean_url,
    decode_bytes,
    decode_html,
    detect_block,
    fetch_url,
    is_blocked_ip,
    normalize_charset,
    sniff_kind,
)

PAGES = Path(__file__).parent / "fixtures" / "pages"
ARTICLE = (PAGES / "article.html").read_bytes()
CHALLENGE = (PAGES / "cloudflare_challenge.html").read_bytes()
CF_BLOCK = (PAGES / "cloudflare_block.html").read_bytes()


def epub_bytes() -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        z.writestr(info, b"application/epub+zip")
        z.writestr("META-INF/container.xml", b"<container/>")
    return bio.getvalue()


# --------------------------------------------------------------------------------------
# Fixture server
# --------------------------------------------------------------------------------------


def make_app() -> web.Application:
    app = web.Application()
    app["hits"] = []

    @web.middleware
    async def count(request, handler):
        app["hits"].append(request.path_qs)
        return await handler(request)

    app.middlewares.append(count)

    async def article(request):
        return web.Response(body=ARTICLE, content_type="text/html", charset="utf-8")

    async def redirect_n(request):
        n = int(request.match_info["n"])
        raise web.HTTPFound("/article" if n <= 1 else f"/redirect/{n - 1}")

    async def goto(request):
        raise web.HTTPFound(request.query["to"])

    async def goto307(request):
        raise web.HTTPTemporaryRedirect(request.query["to"])

    async def loop(request):
        raise web.HTTPFound("/loop")

    async def tracking(request):
        raise web.HTTPMovedPermanently("/article?utm_source=feed&id=7&fbclid=abc")

    async def relative(request):
        raise web.HTTPFound("../article")

    async def big(request):
        return web.Response(body=b"a" * 2_000_000, content_type="text/plain")

    async def big_pdf(request):
        return web.Response(body=b"%PDF-1.4\n" + b"0" * 2_000_000, content_type="application/pdf")

    async def big_chunked(request):
        resp = web.StreamResponse()
        resp.content_type = "text/plain"
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for _ in range(40):
            await resp.write(b"a" * 65536)
        await resp.write_eof()
        return resp

    async def gzip_bomb(request):
        data = gzip.compress(b"\0" * 5_000_000)
        return web.Response(body=data, headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"})

    async def slow(request):
        await asyncio.sleep(5)
        return web.Response(text="late")

    async def slow_body(request):
        resp = web.StreamResponse()
        resp.content_type = "text/html"
        await resp.prepare(request)
        await resp.write(b"<html><body><p>start")
        await asyncio.sleep(5)
        await resp.write(b"</p></body></html>")
        return resp

    async def echo(request):
        return web.json_response({k.lower(): v for k, v in request.headers.items()})

    async def status(request):
        code = int(request.match_info["code"])
        return web.Response(status=code, body=b"<html><body><h1>Error</h1></body></html>", content_type="text/html")

    async def cf(request):
        return web.Response(status=503, body=CHALLENGE, content_type="text/html")

    async def cf_block(request):
        return web.Response(status=403, body=CF_BLOCK, content_type="text/html")

    async def octet_pdf(request):
        return web.Response(body=b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", content_type="application/octet-stream")

    async def epub(request):
        return web.Response(body=epub_bytes(), content_type="application/epub+zip")

    async def image(request):
        return web.Response(body=b"\x89PNG\r\n\x1a\n" + b"0" * 3_000_000, content_type="image/png")

    async def koi8(request):
        html = "<html><head><title>Каштанка</title></head><body><p>Молодая рыжая собака</p></body></html>"
        return web.Response(body=html.encode("koi8-r"), headers={"Content-Type": "text/html; charset=KOI8-R"})

    routes = {
        "/article": article,
        "/redirect/{n}": redirect_n,
        "/goto": goto,
        "/goto307": goto307,
        "/loop": loop,
        "/tracking": tracking,
        "/dir/relative": relative,
        "/big": big,
        "/big.pdf": big_pdf,
        "/big-chunked": big_chunked,
        "/gzip-bomb": gzip_bomb,
        "/slow": slow,
        "/slow-body": slow_body,
        "/echo": echo,
        "/status/{code}": status,
        "/cf": cf,
        "/cf-block": cf_block,
        "/octet-pdf": octet_pdf,
        "/book.epub": epub,
        "/image.png": image,
        "/koi8": koi8,
    }
    for path, handler in routes.items():
        app.router.add_get(path, handler)
    return app


@pytest.fixture
async def server(aiohttp_server):
    srv = await aiohttp_server(make_app(), host="127.0.0.1")
    return srv


def url(server, path: str) -> str:
    return str(server.make_url(path))


class FakeResolver(AbstractResolver):
    """Answers from a dict instead of DNS; unknown names fail like NXDOMAIN."""

    def __init__(self, answers: dict[str, list[str]]):
        self.answers = answers
        self.asked: list[str] = []

    async def resolve(self, host, port=0, family=socket.AF_INET):
        self.asked.append(host)
        if host not in self.answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [
            {
                "hostname": host,
                "host": ip,
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for ip in self.answers[host]
        ]

    async def close(self):
        pass


@pytest.fixture
def loopback_is_public(monkeypatch):
    """SSRF guard stays on, but the fixture server's 127.0.0.1 counts as public."""
    real = fetch_mod.is_blocked_ip
    monkeypatch.setattr(fetch_mod, "is_blocked_ip", lambda ip: False if ip == "127.0.0.1" else real(ip))


@pytest.fixture
def fake_dns(monkeypatch):
    resolver = FakeResolver({})
    monkeypatch.setattr(fetch_mod, "_make_resolver", lambda: resolver)
    return resolver


# --------------------------------------------------------------------------------------
# fetch_url
# --------------------------------------------------------------------------------------


async def test_fetch_html(server):
    res = await fetch_url(url(server, "/article"), allow_private=True)
    assert res.status == 200
    assert res.content_type == "text/html"
    assert res.charset == "utf-8"
    assert res.url == url(server, "/article")
    assert res.headers["content-type"].startswith("text/html")
    assert res.blocked is None
    assert res.body == ARTICLE
    assert res.kind == "html"


async def test_user_agent_and_extra_headers(server):
    res = await fetch_url(url(server, "/echo"), allow_private=True, headers={"Accept": "application/json"})
    import json

    sent = json.loads(res.body)
    assert "SendToX3/" in sent["user-agent"] and sent["user-agent"].startswith("Mozilla/5.0 (compatible;")
    assert sent["accept"] == "application/json"
    assert "accept-language" in sent


async def test_redirect_chain_is_followed(server):
    res = await fetch_url(url(server, "/redirect/3"), allow_private=True)
    assert res.status == 200 and res.url == url(server, "/article")


async def test_five_redirects_allowed_six_refused(server):
    ok = await fetch_url(url(server, "/redirect/5"), allow_private=True)
    assert ok.status == 200
    with pytest.raises(FetchError, match="Too many redirects"):
        await fetch_url(url(server, "/redirect/6"), allow_private=True)


async def test_redirect_loop_is_refused(server):
    with pytest.raises(FetchError, match="Too many redirects"):
        await fetch_url(url(server, "/loop"), allow_private=True)
    assert len(server.app["hits"]) == 6  # first request + 5 redirects, then stop


async def test_final_url_is_cleaned(server):
    res = await fetch_url(url(server, "/tracking"), allow_private=True)
    assert res.url == url(server, "/article") + "?id=7"


async def test_relative_redirect(server):
    res = await fetch_url(url(server, "/dir/relative"), allow_private=True)
    assert res.url == url(server, "/article")


@pytest.mark.parametrize("target", ["ftp://example.com/file", "file:///etc/passwd", "javascript:alert(1)"])
async def test_redirect_to_non_http_is_refused(server, target):
    with pytest.raises(FetchError, match="Only http and https"):
        await fetch_url(url(server, "/goto") + "?to=" + target, allow_private=True)


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "javascript:alert(1)", "http://", "not a url", "gopher://x/"])
async def test_invalid_urls(bad):
    with pytest.raises(FetchError):
        await fetch_url(bad)


async def test_loopback_literal_blocked_without_allow_private(server):
    with pytest.raises(FetchError, match="private network address"):
        await fetch_url(url(server, "/article"))
    assert server.app["hits"] == []  # refused before connecting


async def test_localhost_name_blocked(server):
    with pytest.raises(FetchError, match="private network address"):
        await fetch_url(f"http://localhost:{server.port}/article")
    assert server.app["hits"] == []


@pytest.mark.parametrize(
    "target",
    [
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://[::ffff:127.0.0.1]/",
        "http://127.1/",
        "http://192.168.1.1:8080/admin",
        "http://printer.local/",
        "http://router.home.arpa/",
    ],
)
async def test_private_destinations_blocked(target):
    t0 = time.monotonic()
    with pytest.raises(FetchError, match="private network address"):
        await fetch_url(target, timeout=5)
    assert time.monotonic() - t0 < 1  # no connection attempt


async def test_redirect_to_private_ip_is_blocked(server, loopback_is_public):
    with pytest.raises(FetchError, match="private network address"):
        await fetch_url(url(server, "/goto") + "?to=http://169.254.169.254/latest/meta-data/")
    with pytest.raises(FetchError, match="private network address"):
        await fetch_url(url(server, "/goto307") + "?to=http://[::1]:8123/api/")
    assert len(server.app["hits"]) == 2  # only the redirecting hops reached a server


async def test_dns_answers_are_checked_on_every_hop(server, loopback_is_public, fake_dns):
    fake_dns.answers = {"public.test": ["127.0.0.1"], "internal.test": ["10.0.0.7"]}
    ok = await fetch_url(f"http://public.test:{server.port}/article")
    assert ok.status == 200  # the connection went to the resolved, checked address
    hop = f"http://public.test:{server.port}/goto?to=http://internal.test:{server.port}/article"
    with pytest.raises(FetchError, match="private network address"):
        await fetch_url(hop)
    assert "internal.test" in fake_dns.asked


async def test_mixed_dns_answers_keep_only_public_addresses(server, loopback_is_public, fake_dns):
    fake_dns.answers = {"mixed.test": ["10.9.9.9", "127.0.0.1"]}
    res = await fetch_url(f"http://mixed.test:{server.port}/article")
    assert res.status == 200


async def test_unknown_host(fake_dns):
    with pytest.raises(FetchError, match=re.escape("Could not find the server nowhere.test.")):
        await fetch_url("http://nowhere.test/")


async def test_connection_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(FetchError, match="Could not connect"):
        await fetch_url(f"http://127.0.0.1:{port}/", allow_private=True)


async def test_size_cap_from_content_length(server):
    with pytest.raises(FetchError, match="too large"):
        await fetch_url(url(server, "/big"), allow_private=True, max_bytes=1_000_000)


async def test_size_cap_while_streaming(server):
    with pytest.raises(FetchError, match="too large"):
        await fetch_url(url(server, "/big-chunked"), allow_private=True, max_bytes=1_000_000)


async def test_size_cap_applies_after_decompression(server):
    with pytest.raises(FetchError, match="too large"):
        await fetch_url(url(server, "/gzip-bomb"), allow_private=True, max_bytes=1_000_000)


async def test_pdf_cap_can_be_raised(server):
    with pytest.raises(FetchError, match="too large"):
        await fetch_url(url(server, "/big.pdf"), allow_private=True, max_bytes=1_000_000)
    res = await fetch_url(url(server, "/big.pdf"), allow_private=True, max_bytes=1_000_000, max_pdf_bytes=3_000_000)
    assert res.kind == "pdf" and len(res.body) > 2_000_000


async def test_timeout_waiting_for_headers(server):
    t0 = time.monotonic()
    with pytest.raises(FetchError, match=re.escape("did not answer within 0.5 seconds.")):
        await fetch_url(url(server, "/slow"), allow_private=True, timeout=0.5)
    assert time.monotonic() - t0 < 2


async def test_timeout_while_reading_body(server):
    with pytest.raises(FetchError, match="did not answer"):
        await fetch_url(url(server, "/slow-body"), allow_private=True, timeout=0.5)


async def test_http_errors_are_returned_not_raised(server):
    res = await fetch_url(url(server, "/status/404"), allow_private=True)
    assert res.status == 404 and res.blocked == "HTTP 404"


async def test_bot_walls_are_flagged(server):
    assert (await fetch_url(url(server, "/cf"), allow_private=True)).blocked == "Cloudflare challenge"
    assert (await fetch_url(url(server, "/cf-block"), allow_private=True)).blocked == "Cloudflare block"


async def test_media_bodies_are_not_downloaded(server):
    res = await fetch_url(url(server, "/image.png"), allow_private=True, max_bytes=1000)
    assert res.body == b"" and res.kind == "image" and res.status == 200


async def test_kinds_by_magic_bytes(server):
    assert (await fetch_url(url(server, "/octet-pdf"), allow_private=True)).kind == "pdf"
    assert (await fetch_url(url(server, "/book.epub"), allow_private=True)).kind == "epub"


async def test_header_charset_is_reported_and_decoded(server):
    res = await fetch_url(url(server, "/koi8"), allow_private=True)
    assert res.charset == "koi8-r"
    assert "Молодая рыжая собака" in decode_html(res.body, res.charset)


async def test_input_url_is_cleaned_before_fetching(server):
    wrapped = "https://www.google.com/url?q=" + url(server, "/article") + "&sa=D"
    res = await fetch_url(wrapped, allow_private=True)
    assert server.app["hits"] == ["/article"]
    assert res.status == 200


# --------------------------------------------------------------------------------------
# SSRF address classification
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
        "0.0.0.0", "224.0.0.1", "239.255.255.250", "255.255.255.255", "192.0.2.1", "198.18.0.1",
        "::1", "::", "fe80::1", "fe80::1%en0", "fc00::1", "fd12:3456::1", "ff02::1",
        "::ffff:127.0.0.1", "::ffff:10.0.0.1", "64:ff9b::a00:1", "2002:a00:1::", "not-an-ip", "",
    ],
)  # fmt: skip
def test_blocked_addresses(addr):
    assert is_blocked_ip(addr)


@pytest.mark.parametrize(
    "addr", ["8.8.8.8", "1.1.1.1", "93.184.215.14", "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"]
)
def test_public_addresses(addr):
    assert not is_blocked_ip(addr)


# --------------------------------------------------------------------------------------
# clean_url
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://example.com/a?utm_source=x&utm_medium=y&id=5", "https://example.com/a?id=5"),
        ("https://example.com/a?id=5&fbclid=abc#:~:text=hello", "https://example.com/a?id=5"),
        ("https://example.com/a#section:~:text=hello", "https://example.com/a#section"),
        ("https://example.com/a#xtor=RSS-3208", "https://example.com/a"),
        ("https://example.com/a#comments", "https://example.com/a#comments"),
        ("https://github.com/o/r/blob/main/x.py?ref=main", "https://github.com/o/r/blob/main/x.py?ref=main"),
        ("https://example.com/?ref=newsletter", "https://example.com/?ref=newsletter"),
        ("HTTPS://Example.COM:443/Path?Q=1", "https://example.com/Path?Q=1"),
        ("http://example.com:80/", "http://example.com/"),
        ("http://example.com:8080/x", "http://example.com:8080/x"),
        ("https://www.google.com/url?q=https://example.com/a%3Fid%3D1%26utm_source%3Dx&sa=D", "https://example.com/a?id=1"),
        ("https://www.google.co.uk/url?sa=t&url=https%3A%2F%2Fexample.com%2Fb", "https://example.com/b"),
        ("https://www.google.com/amp/s/example.com/amp/story", "https://example.com/amp/story"),
        ("https://example-com.cdn.ampproject.org/c/s/example.com/amp/story", "https://example.com/amp/story"),
        ("https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.com%2Fp%3Ffbclid%3D1&h=AT0", "https://example.com/p"),
        ("https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2Fexample.com%2Fdoc&data=05%7C01&reserved=0", "https://example.com/doc"),
        ("https://out.reddit.com/t3_abc?url=https%3A%2F%2Fexample.com%2Fx&token=t", "https://example.com/x"),
        ("https://urldefense.proofpoint.com/v2/url?u=https-3A__example.com_path_page-3Fa-3D1&d=DwMF", "https://example.com/path/page?a=1"),
        ("https://urldefense.com/v3/__https://example.com/a/b__;!!abc$", "https://example.com/a/b"),
        ("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F&rut=x", "https://example.com/"),
        ("https://www.youtube.com/redirect?event=video&q=https%3A%2F%2Fexample.com%2Fv", "https://example.com/v"),
        ("https://x.com/user/status/1?s=20&t=abc", "https://x.com/user/status/1"),
        ("https://www.youtube.com/watch?v=abc&si=xyz&t=42", "https://www.youtube.com/watch?v=abc&t=42"),
        ("https://youtu.be/abc?si=xyz", "https://youtu.be/abc"),
        ("https://medium.com/@a/post-fc5ad8f0f8c6?source=rss------programming-5", "https://medium.com/@a/post-fc5ad8f0f8c6"),
        ("https://foo.substack.com/p/bar?r=1abc&utm_campaign=post&showWelcomeOnShare=true", "https://foo.substack.com/p/bar"),
        ("https://www.nytimes.com/2026/x.html?unlocked_article_code=abc&smid=url-share", "https://www.nytimes.com/2026/x.html?unlocked_article_code=abc"),
        ("https://www.theguardian.com/a?CMP=share_btn_url", "https://www.theguardian.com/a"),
        ("https://www.bbc.co.uk/news/articles/x?at_medium=RSS&at_campaign=rss", "https://www.bbc.co.uk/news/articles/x"),
        ("https://example.com/a?gclid=1&msclkid=2&mc_cid=3&mc_eid=4&_hsenc=5&igshid=6", "https://example.com/a"),
        ("https://example.com/search?q=a+b&page=2", "https://example.com/search?q=a+b&page=2"),
        ("https://example.com/a?x=%2F%7E&utm_term=z", "https://example.com/a?x=%2F%7E"),
        ("https://example.com/a?id=1&amp;utm_source=x", "https://example.com/a?id=1"),
        ("https://example.com/a?flag&utm_source=x", "https://example.com/a?flag"),
        ("example.com/article", "https://example.com/article"),
        ("www.example.com", "https://www.example.com"),
        ("example.com:8443/x", "https://example.com:8443/x"),
        ("  <https://example.com/a>  ", "https://example.com/a"),
        ("https://example.com/path with space", "https://example.com/path%20with%20space"),
        ("https://user:pw@Example.com/x", "https://user:pw@example.com/x"),
        ("mailto:someone@example.com", "mailto:someone@example.com"),
        ("just words", "just words"),
        ("", ""),
    ],
)  # fmt: skip
def test_clean_url(raw, expected):
    assert clean_url(raw) == expected


def test_clean_url_is_idempotent():
    u = "https://www.google.com/url?q=https://example.com/a%3Fid%3D1%26utm_source%3Dx&sa=D"
    assert clean_url(clean_url(u)) == clean_url(u)


def test_clean_url_unwraps_nested_wrappers():
    inner = "https%3A%2F%2Fl.facebook.com%2Fl.php%3Fu%3Dhttps%253A%252F%252Fexample.com%252Fz"
    assert clean_url("https://www.google.com/url?q=" + inner) == "https://example.com/z"


# --------------------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------------------

RU = "Молодая рыжая собака - помесь таксы с дворняжкой - бегала взад и вперёд по тротуару."
JA = "銀河鉄道の夜。ジョバンニは①番目の席に座りました。"  # ① exists in cp932, not in strict Shift_JIS


def test_koi8r_from_header_only():
    html = f"<html><head><title>Каштанка</title></head><body><p>{RU}</p></body></html>"
    assert decode_html(html.encode("koi8-r"), "koi8-r") == html


def test_shift_jis_from_meta_and_xml_declaration():
    html = (
        '<?xml version="1.0" encoding="Shift_JIS"?>\n<html><head>'
        '<meta http-equiv="Content-Type" content="text/html;charset=Shift_JIS" />'
        f"</head><body><p>{JA}</p></body></html>"
    )
    assert decode_html(html.encode("cp932"), None) == html


def test_meta_charset_lying_latin1_on_utf8_bytes():
    html = '<html><head><meta charset="iso-8859-1"></head><body><p>Café — “quotes” déjà vu</p></body></html>'
    assert decode_html(html.encode("utf-8"), None) == html


def test_header_lying_latin1_on_utf8_bytes():
    html = "<p>Crème brûlée • naïve</p>"
    assert decode_html(html.encode("utf-8"), "iso-8859-1") == html


def test_real_latin1_label_decodes_as_cp1252():
    html = '<meta charset="iso-8859-1"><p>Caf\xe9 \x93quoted\x94</p>'
    assert decode_html(html.encode("latin-1"), None) == '<meta charset="iso-8859-1"><p>Café “quoted”</p>'


def test_short_unlabelled_western_text_is_cp1252():
    raw = "Café crème brûlée, s’il vous plaît".encode("cp1252")
    assert decode_bytes(raw, None, html=False) == "Café crème brûlée, s’il vous plaît"


def test_long_unlabelled_cyrillic_is_detected():
    raw = (RU + " ") * 80
    assert "рыжая собака" in decode_bytes(raw.encode("koi8-r"), None, html=False)


def test_http_charset_wins_over_meta():
    html = '<meta charset="koi8-r"><p>Привет, мир</p>'
    assert decode_html(html.encode("utf-8"), "utf-8") == html


def test_text_mode_ignores_meta():
    raw = b"<meta charset=koi8-r> Caf\xe9"
    assert decode_bytes(raw, None, html=False).endswith("Café")
    assert not decode_bytes(raw, None, html=True).endswith("Café")


@pytest.mark.parametrize("enc", ["utf-8-sig", "utf-16"])
def test_bom(enc):
    text = "<p>Grüße 👋</p>"
    assert decode_html(text.encode(enc), "iso-8859-1") == text


def test_declared_utf8_with_a_few_broken_bytes():
    raw = ("<p>" + "ok " * 1000 + "</p>").encode() + b"\xff"
    out = decode_html(raw, "utf-8")
    assert out.startswith("<p>ok ok") and out.count("�") == 1


def test_declared_legacy_charset_with_a_stray_undefined_byte():
    # cp1252 leaves 0x81 undefined; a browser shows nothing there, it does not re-guess the page
    raw = ("<p>" + "Caf\xe9 cr\xe8me " * 400 + "</p>").encode("latin-1") + b"\x81"
    out = decode_html(raw, "windows-1252")
    assert out.startswith("<p>Café crème") and out.endswith("</p>")


def test_wrong_label_with_many_errors_falls_back_to_detection():
    raw = ("Привет, мир. " * 200).encode("utf-8")[:-1]  # cut mid-character: not strict UTF-8
    assert "Привет, мир." in decode_html(raw, "utf-8")


def test_empty_body():
    assert decode_html(b"", "utf-8") == ""


@pytest.mark.parametrize(
    "label, codec",
    [
        ("ISO-8859-1", "cp1252"), ("latin1", "cp1252"), ("us-ascii", "cp1252"), ("Shift_JIS", "cp932"),
        ("gb2312", "gb18030"), ("GBK", "gb18030"), ("big5", "big5hkscs"), ("euc-kr", "cp949"),
        ("utf-16", "utf-8"), ('"UTF-8"', "utf-8"), ("koi8-r", "koi8-r"), ("windows-1251", "cp1251"),
        ("no-such-charset", None), (None, None), ("", None),
    ],
)  # fmt: skip
def test_normalize_charset(label, codec):
    assert normalize_charset(label) == codec


# --------------------------------------------------------------------------------------
# Block detection and content kinds
# --------------------------------------------------------------------------------------

U = "https://www.example.org/story"


@pytest.mark.parametrize(
    "status, headers, page_url, body, expected",
    [
        (403, {}, U, CF_BLOCK, "Cloudflare block"),
        (503, {}, U, CHALLENGE, "Cloudflare challenge"),
        (200, {"cf-mitigated": "challenge"}, U, b"<html></html>", "Cloudflare challenge"),
        (403, {"x-datadome": "protected"}, U, b"<html>blocked</html>", "DataDome bot check"),
        (403, {}, U, b"<p>Please enable JS and disable any ad blocker</p>", "DataDome bot check"),
        (200, {}, "https://consent.google.com/ml?continue=x", b"<html>consent</html>", "consent page"),
        (404, {}, U, b"<html>Not found</html>", "HTTP 404"),
        (451, {}, U, b"", "HTTP 451"),
        (200, {}, U, ARTICLE, None),
        (200, {}, U, b"<title>Just a moment...</title>" + b"x" * 70_000, None),  # big page: not a wall
    ],
)
def test_detect_block(status, headers, page_url, body, expected):
    assert detect_block(status, headers, page_url, body) == expected


@pytest.mark.parametrize(
    "ctype, body, expected",
    [
        ("text/html", b"<html>", "html"),
        ("application/xhtml+xml", b"<?xml version='1.0'?><html/>", "html"),
        ("application/pdf", b"%PDF-1.7", "pdf"),
        ("application/octet-stream", b"%PDF-1.4 ...", "pdf"),
        ("application/octet-stream", b"\r\n  junk before %PDF-1.4", "pdf"),
        ("text/html", b"<html><body><p>The %PDF- header starts every file</p>", "html"),
        ("", epub_bytes(), "epub"),
        ("application/epub+zip", b"PK", "epub"),
        ("image/png", b"", "image"),
        ("audio/mpeg", b"", "media"),
        ("video/mp4", b"", "media"),
        ("text/plain", b"hello", "text"),
        ("text/markdown", b"# hi", "text"),
        ("text/plain", b"<!DOCTYPE html><html><body>x</body></html>", "html"),
        ("", b"  <html lang='en'><body>", "html"),
        ("application/json", b"{}", "other"),
        ("text/css", b"body{}", "other"),
        ("application/octet-stream", b"\x00\x01binary", "other"),
        ("", b"plain words", "text"),
    ],
)
def test_sniff_kind(ctype, body, expected):
    assert sniff_kind(ctype, body) == expected
