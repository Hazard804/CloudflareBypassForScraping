import asyncio
import base64
import logging
import ssl
from typing import Dict, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit

from python_socks.async_.asyncio import Proxy

from cf_bypasser.utils.constants import BRAVE_DOMAINS

# with a proxy, Chrome sends hostnames to the proxy and skips --host-resolver-rules, so block here too
BLOCKED_DOMAINS = BRAVE_DOMAINS

_HEAD_LIMIT = 64 * 1024
_CONNECT_TIMEOUT_SECONDS = 20
_TLS_HANDSHAKE_TIMEOUT_SECONDS = 5


def _proxy_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _CONNECT_TIMEOUT_SECONDS)


async def _http_connect(upstream: str, host: str, port: int, use_tls: bool):
    """CONNECT host:port through an http(s) proxy; returns (reader, writer) of the tunnel."""
    parsed = urlsplit(upstream)
    if use_tls:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                parsed.hostname, parsed.port, ssl=_proxy_ssl_context(), server_hostname=parsed.hostname,
                ssl_handshake_timeout=_TLS_HANDSHAKE_TIMEOUT_SECONDS,
            ),
            _CONNECT_TIMEOUT_SECONDS,
        )
    else:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, parsed.port), _CONNECT_TIMEOUT_SECONDS
        )
    try:
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        if parsed.username is not None:
            creds = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}"
            lines.append("Proxy-Authorization: Basic " + base64.b64encode(creds.encode()).decode())
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        status = (await _read_head(reader)).split(b"\r\n", 1)[0]
        if b" 200" not in status:
            raise ConnectionError(status.decode("latin-1", "replace"))
        return reader, writer
    except BaseException:
        writer.close()
        raise


def _peer_spoke_plaintext(err: ssl.SSLError) -> bool:
    reason = f"{getattr(err, 'reason', '')} {err}".upper()
    return any(sig in reason for sig in ("WRONG_VERSION_NUMBER", "HTTP_REQUEST", "RECORD_LAYER_FAILURE",
                                         "PACKET_LENGTH_TOO_LONG", "WRONG VERSION NUMBER", "RECORD LAYER FAILURE"))


async def open_upstream(upstream: str, host: str, port: int):
    """Tunnel to host:port through the upstream proxy. Never connects directly."""
    scheme = urlsplit(upstream).scheme.lower()
    if scheme == "http":
        return await _http_connect(upstream, host, port, use_tls=False)
    if scheme == "https":
        try:
            return await _http_connect(upstream, host, port, use_tls=True)
        except ssl.SSLError as e:
            # fall back to plain only when the proxy definitely isn't TLS: it answered the ClientHello
            # with an HTTP error. Timeouts, silence and certificate errors fail closed, so credentials
            # never go out in cleartext to something that might have been a TLS proxy.
            if isinstance(e, ssl.SSLCertVerificationError) or not _peer_spoke_plaintext(e):
                raise
            return await _http_connect(upstream, host, port, use_tls=False)
    sock = await Proxy.from_url(upstream).connect(host, port, timeout=_CONNECT_TIMEOUT_SECONDS)
    return await asyncio.open_connection(sock=sock)


def is_blocked_host(host: str, domains) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def needs_relay(proxy: str) -> bool:
    """Credentials, SOCKS and https:// (TLS-or-plain detection) go through the local relay."""
    parsed = urlsplit(proxy)
    return bool(parsed.username) or parsed.scheme.lower() in ("https", "socks4", "socks5")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _parse_target(line: str) -> Tuple[str, str, int, Optional[str]]:
    """Return (method, host, port, origin_form_request_line or None for CONNECT)."""
    method, target, version = line.split(" ", 2)
    if method.upper() == "CONNECT":
        host, _, port = target.rpartition(":")
        return method, host.strip("[]"), int(port), None
    parsed = urlsplit(target)
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        raise ValueError(f"unsupported request target: {target}")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return method, parsed.hostname, parsed.port or 80, f"{method} {path} {version}"


class ProxyRelay:
    """Unauthenticated local HTTP proxy that tunnels every connection through one upstream proxy."""

    def __init__(self, upstream: str):
        self.upstream = upstream
        self._server: Optional[asyncio.base_events.Server] = None
        self._tasks: Set[asyncio.Task] = set()

    @property
    def url(self) -> str:
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._server.wait_closed()
        self._server = None

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            await self._handle(reader, writer)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.debug(f"Relay client error: {e}")
        finally:
            self._tasks.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        if len(head) > _HEAD_LIMIT:
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, host, port, origin_line = _parse_target(lines[0])
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return

        if is_blocked_host(host, BLOCKED_DOMAINS):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        try:
            # never fall back to a direct connection: that would leak the real IP
            up_reader, up_writer = await open_upstream(self.upstream, host, port)
        except Exception as e:
            logging.warning(f"Relay upstream connect to {host}:{port} failed: {e}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        try:
            if origin_line is None:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
                return
            # plain-HTTP request: Chrome reuses keep-alive proxy connections across origins, so serve
            # exactly this one request on this tunnel and close; the next one gets a fresh connection
            headers = [h for h in lines[1:] if h and h.split(":", 1)[0].strip().lower() not in _HOP_HEADERS]
            up_writer.write(("\r\n".join([origin_line, *headers, "Connection: close"]) + "\r\n\r\n").encode("latin-1"))
            body_len = _content_length(headers)
            if body_len:
                up_writer.write(await reader.readexactly(body_len))
            await up_writer.drain()
            await _pipe(up_reader, writer)
        finally:
            up_writer.close()


_HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate", "te", "upgrade"}


def _content_length(headers) -> int:
    for h in headers:
        name, _, value = h.partition(":")
        if name.strip().lower() == "content-length":
            try:
                return max(0, int(value.strip()))
            except ValueError:
                return 0
    return 0


class RelayRegistry:
    """One refcounted relay per upstream proxy URL."""

    def __init__(self):
        self._relays: Dict[str, ProxyRelay] = {}
        self._refs: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, upstream: str) -> str:
        async with self._lock:
            relay = self._relays.get(upstream)
            if relay is None:
                relay = ProxyRelay(upstream)
                await relay.start()
                self._relays[upstream] = relay
            self._refs[upstream] = self._refs.get(upstream, 0) + 1
            return relay.url

    async def release(self, upstream: str) -> None:
        async with self._lock:
            if upstream not in self._refs:
                return
            self._refs[upstream] -= 1
            if self._refs[upstream] <= 0:
                del self._refs[upstream]
                await self._relays.pop(upstream).stop()

    async def close_all(self) -> None:
        async with self._lock:
            for relay in self._relays.values():
                await relay.stop()
            self._relays.clear()
            self._refs.clear()
