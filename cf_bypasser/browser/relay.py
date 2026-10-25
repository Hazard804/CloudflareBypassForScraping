import asyncio
import logging
from typing import Dict, Optional, Set, Tuple
from urllib.parse import urlsplit

from python_socks.async_.asyncio import Proxy

_HEAD_LIMIT = 64 * 1024
_CONNECT_TIMEOUT_SECONDS = 20


def needs_relay(proxy: str) -> bool:
    """Chrome can't authenticate per-context proxies or speak SOCKS auth, so those go through a relay."""
    parsed = urlsplit(proxy)
    return bool(parsed.username) or parsed.scheme.lower().startswith("socks")


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

        try:
            # never fall back to a direct connection: that would leak the real IP
            sock = await Proxy.from_url(self.upstream).connect(host, port, timeout=_CONNECT_TIMEOUT_SECONDS)
            up_reader, up_writer = await asyncio.open_connection(sock=sock)
        except Exception as e:
            logging.warning(f"Relay upstream connect to {host}:{port} failed: {e}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        if origin_line is None:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            headers = [h for h in lines[1:] if h and not h.lower().startswith("proxy-")]
            up_writer.write(("\r\n".join([origin_line, *headers]) + "\r\n\r\n").encode("latin-1"))
            await up_writer.drain()

        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))


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
