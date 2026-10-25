import asyncio
import base64
import socket

import pytest
import pytest_asyncio

from cf_bypasser.browser.relay import ProxyRelay, RelayRegistry, needs_relay

pytestmark = pytest.mark.asyncio(loop_scope="function")


async def _read_head(reader):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            break
        data += chunk
    return data


class _Server:
    def __init__(self, server):
        self.server = server
        self.port = server.sockets[0].getsockname()[1]
        self.saw_auth = False
        self.connects = []


@pytest_asyncio.fixture(loop_scope="function")
async def origin():
    async def handle(reader, writer):
        await _read_head(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\nConnection: close\r\n\r\norigin-ok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield _Server(server)
    server.close()


@pytest_asyncio.fixture(loop_scope="function")
async def upstream():
    """HTTP CONNECT proxy requiring u:p."""
    state = {}

    async def handle(reader, writer):
        first = await reader.read(1)
        if first == b"\x16":  # TLS ClientHello sent to a plain HTTP proxy
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        head = (first + await _read_head(reader)).decode("latin-1")
        line = head.split("\r\n", 1)[0]
        auth_ok = f"Proxy-Authorization: Basic {base64.b64encode(b'u:p').decode()}" in head
        if not auth_ok:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        state["srv"].saw_auth = True
        target = line.split(" ")[1]
        state["srv"].connects.append(target)
        host, port = target.rsplit(":", 1)
        try:
            r2, w2 = await asyncio.open_connection(host, int(port))
        except OSError:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        async def pipe(r, w):
            try:
                while data := await r.read(65536):
                    w.write(data)
                    await w.drain()
            finally:
                w.close()

        await asyncio.gather(pipe(reader, w2), pipe(r2, writer), return_exceptions=True)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state["srv"] = _Server(server)
    yield state["srv"]
    server.close()


def _port(url):
    return int(url.rsplit(":", 1)[1])


async def _connect_get(relay_url, target):
    reader, writer = await asyncio.open_connection("127.0.0.1", _port(relay_url))
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await writer.drain()
    status = await _read_head(reader)
    if not status.startswith(b"HTTP/1.1 200"):
        writer.close()
        return status, b""
    writer.write(f"GET / HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    body = await reader.read()
    writer.close()
    return status, body


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_connect_tunnel_through_authed_upstream(origin, upstream):
    relay = ProxyRelay(f"http://u:p@127.0.0.1:{upstream.port}")
    await relay.start()
    try:
        status, body = await _connect_get(relay.url, f"127.0.0.1:{origin.port}")
        assert status.startswith(b"HTTP/1.1 200")
        assert body.endswith(b"origin-ok")
        assert upstream.saw_auth
    finally:
        await relay.stop()


async def test_absolute_form_get(origin, upstream):
    relay = ProxyRelay(f"http://u:p@127.0.0.1:{upstream.port}")
    await relay.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", _port(relay.url))
        writer.write(
            f"GET http://127.0.0.1:{origin.port}/x HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Proxy-Connection: keep-alive\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        body = await reader.read()
        writer.close()
        assert body.startswith(b"HTTP/1.1 200")
        assert body.endswith(b"origin-ok")
        assert upstream.connects == [f"127.0.0.1:{origin.port}"]
    finally:
        await relay.stop()


async def test_wrong_credentials_returns_502(origin, upstream):
    relay = ProxyRelay(f"http://u:WRONG@127.0.0.1:{upstream.port}")
    await relay.start()
    try:
        status, _ = await _connect_get(relay.url, f"127.0.0.1:{origin.port}")
        assert status.startswith(b"HTTP/1.1 502")
    finally:
        await relay.stop()


async def test_relay_upstream_unreachable_closes_client(origin):
    relay = ProxyRelay(f"http://u:p@127.0.0.1:{_free_port()}")
    await relay.start()
    try:
        status, body = await _connect_get(relay.url, f"127.0.0.1:{origin.port}")
        assert status.startswith(b"HTTP/1.1 502")
        assert body == b""
    finally:
        await relay.stop()


async def test_relay_binds_loopback_only(upstream):
    relay = ProxyRelay(f"http://u:p@127.0.0.1:{upstream.port}")
    await relay.start()
    try:
        assert relay.url.startswith("http://127.0.0.1:")
    finally:
        await relay.stop()


async def test_registry_refcount(upstream):
    reg = RelayRegistry()
    up = f"http://u:p@127.0.0.1:{upstream.port}"
    a = await reg.acquire(up)
    b = await reg.acquire(up)
    assert a == b
    await reg.release(up)
    r, w = await asyncio.open_connection("127.0.0.1", _port(a))
    w.close()
    await reg.release(up)
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", _port(a))
    await reg.close_all()


def test_needs_relay():
    assert needs_relay("http://u:p@h:1")
    assert needs_relay("socks5://h:1")
    assert needs_relay("socks4://h:1")
    assert not needs_relay("http://h:1")
    assert needs_relay("https://h:1")  # TLS-or-plain detection lives in the relay


def _self_signed(tmp_path):
    import subprocess
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    return cert, key


@pytest_asyncio.fixture(loop_scope="function")
async def tls_upstream(upstream, tmp_path):
    """TLS-terminating front for the CONNECT proxy, like a real https:// proxy."""
    import ssl
    cert, key = _self_signed(tmp_path)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)

    async def handle(reader, writer):
        r2, w2 = await asyncio.open_connection("127.0.0.1", upstream.port)

        async def pipe(r, w):
            try:
                while data := await r.read(65536):
                    w.write(data)
                    await w.drain()
            except Exception:
                pass
            finally:
                w.close()

        await asyncio.gather(pipe(reader, w2), pipe(r2, writer))

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    srv = _Server(server)
    srv.cert = cert
    yield srv
    server.close()


async def test_https_upstream_speaks_tls(origin, upstream, tls_upstream, monkeypatch):
    import ssl
    import cf_bypasser.browser.relay as relay_mod
    monkeypatch.setattr(relay_mod, "_proxy_ssl_context", lambda: ssl.create_default_context(cafile=str(tls_upstream.cert)))
    relay = ProxyRelay(f"https://u:p@127.0.0.1:{tls_upstream.port}")
    await relay.start()
    try:
        status, body = await _connect_get(relay.url, f"127.0.0.1:{origin.port}")
        assert status.startswith(b"HTTP/1.1 200") and body.endswith(b"origin-ok")
        assert upstream.saw_auth
    finally:
        await relay.stop()


async def test_https_upstream_bad_cert_never_falls_back_to_plain(origin, upstream, tls_upstream):
    relay = ProxyRelay(f"https://u:p@127.0.0.1:{tls_upstream.port}")  # self-signed, not trusted
    await relay.start()
    try:
        status, _ = await _connect_get(relay.url, f"127.0.0.1:{origin.port}")
        assert status.startswith(b"HTTP/1.1 502")
        assert not upstream.saw_auth
    finally:
        await relay.stop()


async def test_https_scheme_on_plain_proxy_falls_back(origin, upstream):
    # providers often write https:// for a plain HTTP proxy (issue #115); it answers the ClientHello with HTTP 400
    relay = ProxyRelay(f"https://u:p@127.0.0.1:{upstream.port}")
    await relay.start()
    try:
        status, body = await _connect_get(relay.url, f"127.0.0.1:{origin.port}")
        assert status.startswith(b"HTTP/1.1 200") and body.endswith(b"origin-ok")
    finally:
        await relay.stop()


async def test_percent_encoded_credentials_decoded(origin, upstream):
    relay = ProxyRelay(f"http://%75:%70@127.0.0.1:{upstream.port}")  # "u":"p" percent-encoded
    await relay.start()
    try:
        status, _ = await _connect_get(relay.url, f"127.0.0.1:{origin.port}")
        assert status.startswith(b"HTTP/1.1 200")
    finally:
        await relay.stop()


def test_https_with_credentials_needs_relay():
    assert needs_relay("https://u:p@h:1")


async def test_silent_https_proxy_never_gets_cleartext_credentials(origin, monkeypatch):
    import cf_bypasser.browser.relay as relay_mod
    monkeypatch.setattr(relay_mod, "_TLS_HANDSHAKE_TIMEOUT_SECONDS", 0.3)
    received = []

    async def silent(reader, writer):  # e.g. an on-path attacker dropping the ClientHello
        received.append(await reader.read(65536))
        await asyncio.sleep(2)
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    relay = ProxyRelay(f"https://u:p@127.0.0.1:{port}")
    await relay.start()
    try:
        status, _ = await _connect_get(relay.url, f"127.0.0.1:{origin.port}")
        assert status.startswith(b"HTTP/1.1 502")
        assert not any(b"Proxy-Authorization" in r for r in received)
    finally:
        await relay.stop()
        server.close()


@pytest_asyncio.fixture(loop_scope="function")
async def keepalive_origin():
    """Origin that keeps the connection open and records every request line it receives."""
    seen = []

    async def handle(reader, writer):
        while True:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except Exception:
                break
            seen.append(head.decode("latin-1"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok")
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    srv = _Server(server)
    srv.seen = seen
    yield srv
    server.close()


async def test_absolute_form_keepalive_never_crosses_hosts(keepalive_origin, upstream):
    relay = ProxyRelay(f"http://u:p@127.0.0.1:{upstream.port}")
    await relay.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", _port(relay.url))
        writer.write(f"GET http://127.0.0.1:{keepalive_origin.port}/a HTTP/1.1\r\nHost: a\r\n"
                     f"Connection: keep-alive\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
        await writer.drain()
        first = await asyncio.wait_for(reader.read(65536), 5)
        assert first.startswith(b"HTTP/1.1 200")
        # Chrome reuses the proxy connection for another origin
        try:
            writer.write(b"GET http://other.example/b HTTP/1.1\r\nHost: other.example\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(reader.read(65536), 2)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        writer.close()
        await asyncio.sleep(0.1)
        assert len(keepalive_origin.seen) == 1
        assert "connection: close" in keepalive_origin.seen[0].lower()
        assert "proxy-connection" not in keepalive_origin.seen[0].lower()
    finally:
        await relay.stop()
