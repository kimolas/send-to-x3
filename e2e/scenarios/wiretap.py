"""A recording TCP relay between the device (simulator) and a server.

The device is pointed at the tap; the tap forwards bytes unchanged to the real
upstream and records both directions per connection. Because it never re-frames
anything, assertions see exactly what the firmware sent and what the server answered
(status, headers, framing, body), whichever backend is behind it.

Note: inside the simulator HTTP goes through host ``curl`` (crosspoint-simulator
``SimHttpFetch.h``), so request *headers* differ slightly from the device's own client
(curl adds ``Accept: */*``, no ``Connection: keep-alive``). The wire harness
(``e2e/wire``) covers the device's real request bytes.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HttpMessage:
    start_line: str
    headers: list[tuple[str, str]]
    body: bytes
    complete: bool

    def header(self, name: str) -> Optional[str]:
        name = name.lower()
        for k, v in self.headers:
            if k.lower() == name:
                return v
        return None

    def header_all(self, name: str) -> list[str]:
        name = name.lower()
        return [v for k, v in self.headers if k.lower() == name]


@dataclass
class Exchange:
    conn: int
    t: float
    method: str
    target: str
    request: HttpMessage
    response: Optional[HttpMessage]

    @property
    def path(self) -> str:
        return self.target.split("?", 1)[0]

    @property
    def status(self) -> Optional[int]:
        if not self.response:
            return None
        try:
            return int(self.response.start_line.split(" ", 2)[1])
        except (IndexError, ValueError):
            return None

    def summary(self) -> dict:
        r = self.response
        return {
            "conn": self.conn, "method": self.method, "target": self.target, "status": self.status,
            "request_headers": self.request.headers, "request_body_bytes": len(self.request.body),
            "response_headers": r.headers if r else None, "response_body_bytes": len(r.body) if r else None,
            "response_complete": r.complete if r else None,
        }


@dataclass
class _Conn:
    id: int
    t: float
    to_server: bytearray = field(default_factory=bytearray)
    to_client: bytearray = field(default_factory=bytearray)
    closed: bool = False


def _split_head(data: bytes, pos: int) -> Optional[tuple[str, list[tuple[str, str]], int]]:
    end = data.find(b"\r\n\r\n", pos)
    if end < 0:
        return None
    lines = data[pos:end].decode("iso-8859-1").split("\r\n")
    headers = []
    for ln in lines[1:]:
        k, sep, v = ln.partition(":")
        if sep:
            headers.append((k.strip(), v.strip()))
    return lines[0], headers, end + 4


def _body(data: bytes, pos: int, headers: list[tuple[str, str]], *, until_close: bool) -> tuple[bytes, int, bool]:
    """(body, next position, complete) for a message whose head ended at ``pos``."""
    h = {k.lower(): v for k, v in headers}
    if "chunked" in h.get("transfer-encoding", "").lower():
        out = bytearray()
        p = pos
        while True:
            eol = data.find(b"\r\n", p)
            if eol < 0:
                return bytes(out), len(data), False
            try:
                size = int(data[p:eol].split(b";")[0].strip() or b"0", 16)
            except ValueError:
                return bytes(out), len(data), False
            p = eol + 2
            if size == 0:
                trailer_end = data.find(b"\r\n", p)
                return bytes(out), (trailer_end + 2 if trailer_end >= 0 else len(data)), trailer_end >= 0
            if p + size > len(data):
                out += data[p:]
                return bytes(out), len(data), False
            out += data[p:p + size]
            p += size + 2
    if "content-length" in h:
        try:
            n = int(h["content-length"])
        except ValueError:
            n = 0
        chunk = data[pos:pos + n]
        return chunk, pos + len(chunk), len(chunk) == n
    if until_close:
        return data[pos:], len(data), True
    return b"", pos, True


def parse_requests(data: bytes) -> list[HttpMessage]:
    out: list[HttpMessage] = []
    pos = 0
    while pos < len(data):
        head = _split_head(data, pos)
        if head is None:
            break
        start, headers, pos = head
        body, pos, complete = _body(data, pos, headers, until_close=False)
        out.append(HttpMessage(start, headers, body, complete))
    return out


def parse_responses(data: bytes, methods: list[str], closed: bool) -> list[HttpMessage]:
    out: list[HttpMessage] = []
    pos = 0
    for method in methods:
        if pos >= len(data):
            break
        head = _split_head(data, pos)
        if head is None:
            break
        start, headers, pos = head
        try:
            status = int(start.split(" ", 2)[1])
        except (IndexError, ValueError):
            status = 0
        if method == "HEAD" or status in (204, 304) or 100 <= status < 200:
            out.append(HttpMessage(start, headers, b"", True))
            continue
        body, pos, complete = _body(data, pos, headers, until_close=True)
        if not closed and not any(k.lower() in ("content-length", "transfer-encoding") for k, _ in headers):
            complete = False
        out.append(HttpMessage(start, headers, body, complete))
    return out


class WireTap:
    """``WireTap(upstream_port).start()`` -> listens on ``127.0.0.1:<tap.port>``."""

    def __init__(self, upstream_port: int, upstream_host: str = "127.0.0.1", *, name: str = "tap") -> None:
        self.upstream = (upstream_host, upstream_port)
        self.name = name
        self._lock = threading.Lock()
        self._conns: list[_Conn] = []
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "WireTap":
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(16)
        s.settimeout(0.2)
        self._sock = s
        self.port = s.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True, name=f"{self.name}-accept").start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            self._sock.close()

    def __enter__(self) -> "WireTap":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def clear(self) -> None:
        with self._lock:
            self._conns.clear()

    def _accept(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            with self._lock:
                conn = _Conn(id=len(self._conns) + 1, t=time.time())
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(client, conn), daemon=True).start()

    def _serve(self, client: socket.socket, conn: _Conn) -> None:
        try:
            upstream = socket.create_connection(self.upstream, timeout=10)
        except OSError:
            client.close()
            conn.closed = True
            return
        upstream.settimeout(None)
        done = threading.Event()

        def pump(src: socket.socket, dst: socket.socket, buf: bytearray) -> None:
            try:
                while True:
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    with self._lock:
                        buf += chunk
                    dst.sendall(chunk)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                if done.is_set():
                    conn.closed = True
                done.set()

        t = threading.Thread(target=pump, args=(upstream, client, conn.to_client), daemon=True)
        t.start()
        pump(client, upstream, conn.to_server)
        t.join(timeout=120)
        client.close()
        upstream.close()
        conn.closed = True

    def exchanges(self) -> list[Exchange]:
        with self._lock:
            conns = [(c.id, c.t, bytes(c.to_server), bytes(c.to_client), c.closed) for c in self._conns]
        out: list[Exchange] = []
        for cid, t, up, down, closed in conns:
            reqs = parse_requests(up)
            methods = [r.start_line.split(" ", 1)[0] for r in reqs]
            resps = parse_responses(down, methods, closed)
            for i, req in enumerate(reqs):
                parts = req.start_line.split(" ")
                out.append(Exchange(conn=cid, t=t, method=parts[0], target=parts[1] if len(parts) > 1 else "",
                                    request=req, response=resps[i] if i < len(resps) else None))
        return out

    def find(self, path_prefix: str, method: Optional[str] = None) -> list[Exchange]:
        return [e for e in self.exchanges()
                if e.target.startswith(path_prefix) and (method is None or e.method == method)]

    def wait_for(self, path_prefix: str, timeout: float = 10.0) -> list[Exchange]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = [e for e in self.find(path_prefix) if e.response and e.response.complete]
            if got:
                return got
            time.sleep(0.05)
        return []
