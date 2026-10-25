import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from cf_bypasser.browser.context import BrowserContextSession
from cf_bypasser.browser.pool import BrowserPool
from cf_bypasser.browser.relay import RelayRegistry, needs_relay
from cf_bypasser.cache.cookie_cache import CookieCache
from cf_bypasser.core import challenge
from cf_bypasser.utils.constants import (
    DEFAULT_CACHE_FILE,
    FETCH_MAX_BYTES,
    IP_CHECK_ENABLED,
    JOB_TIMEOUT_SECONDS,
)
from cf_bypasser.utils.ipcheck import get_exit_ip
from cf_bypasser.utils.misc import cache_key, per_loop
from cf_bypasser.utils.security import redact_proxy

# One in-flight lock registry per event loop (multi-loop pytest safe).
_inflight_locks: dict = {}


def _inflight_lock(key: str) -> asyncio.Lock:
    registry = per_loop(_inflight_locks, dict)
    lock = registry.get(key)
    if lock is None:
        lock = asyncio.Lock()
        registry[key] = lock
    return lock


class CFBypasser:
    """Cloudflare bypasser on a pool of reused DrissionPage browsers, with cookie caching."""

    def __init__(
        self,
        log: bool = True,
        cache_file: str = DEFAULT_CACHE_FILE,
        pool: Optional[BrowserPool] = None,
        relays: Optional[RelayRegistry] = None,
    ):
        self.log = log
        self.cookie_cache = CookieCache(cache_file)
        self.pool = pool or BrowserPool()
        self.relays = relays or RelayRegistry()

    def log_message(self, message: str) -> None:
        if self.log:
            logging.info(message)

    async def start(self) -> None:
        if not self.pool.started:
            await self.pool.start()

    def parse_proxy(self, proxy: str) -> Optional[str]:
        """Normalize a proxy URL to scheme://[user:pass@]host:port; None if invalid."""
        try:
            parsed = urlparse(proxy)
            if not parsed.scheme or not parsed.hostname or not parsed.port:
                self.log_message(f"Invalid proxy format: {redact_proxy(proxy)}")
                return None
            return proxy
        except Exception as e:
            # urlparse's message can echo part of the password ("Port could not be cast ... 'pa'")
            self.log_message(f"Error parsing proxy {redact_proxy(proxy)}: {type(e).__name__}")
            return None

    async def _resolve_proxy(self, proxy: Optional[str]) -> Tuple[Optional[str], bool]:
        """Return (proxyServer for Chrome, whether a relay was acquired)."""
        if not proxy:
            return None, False
        if not self.parse_proxy(proxy):
            # never silently fall back to direct: that leaks the real IP
            raise ValueError(f"Invalid proxy, refusing to continue direct: {redact_proxy(proxy)}")
        if needs_relay(proxy):
            return await self.relays.acquire(proxy), True
        parsed = urlparse(proxy)
        self.log_message(f"Using proxy: {parsed.scheme}://{parsed.hostname}:{parsed.port}")
        return proxy, False

    @staticmethod
    def _is_trustworthy(cookies: Dict[str, str], cf_detected: bool) -> bool:
        """A CF-detected result is only trustworthy once a cf_clearance cookie exists."""
        if not cf_detected:
            return True
        return bool(cookies.get("cf_clearance"))

    async def _read_valid_cache(self, key: str, proxy: Optional[str]):
        """Return a still-valid cache entry, or None — invalidating it if the proxy exit IP rotated."""
        cached = self.cookie_cache.get(key)
        if not cached:
            return None
        if IP_CHECK_ENABLED and cached.exit_ip:
            current = await get_exit_ip(proxy)
            if current and current != cached.exit_ip:
                self.log_message(f"Proxy exit IP changed ({cached.exit_ip} -> {current}); invalidating cookies for {key}")
                self.cookie_cache.invalidate(key)
                return None
        return cached

    def _job(
        self,
        browser: Any,
        url: str,
        proxy_server: Optional[str],
        cached_cookies: Optional[Dict[str, str]],
        mode: str,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Runs in a worker thread: one disposable context, solve, extract. mode: cookies | html | fetch."""
        with BrowserContextSession(browser, proxy_server) as session:
            if cached_cookies:
                self.log_message("Restoring cached cookies...")
                session.set_cookies(cached_cookies, url)
            # a binary URL would trigger a download, so solve on the origin and fetch from the page
            target = challenge.origin_of(url) if mode == "fetch" else url
            result = challenge.solve(session.tab, target, self.log_message)
            if not result.success:
                return None, result.cf_detected
            data: Dict[str, Any] = {}
            if mode == "html":
                data["html"] = challenge.stable_html(session.tab)
                data["url"] = session.tab.url
                data["status_code"] = result.status
            elif mode == "fetch":
                data.update(challenge.fetch_in_page(session.tab, url, FETCH_MAX_BYTES))
            data["cookies"] = session.get_cookies()
            data["user_agent"] = session.user_agent()
            return data, result.cf_detected

    async def _run_in_browser(self, url, proxy, key, *, restore_cookies: bool, mode: str):
        """Lease a browser, solve in a fresh context, cache trustworthy cookies. Returns data dict or None."""
        cached_cookies = None
        if restore_cookies:
            cached = await self._read_valid_cache(key, proxy)
            if cached:
                cached_cookies = cached.cookies
                self.log_message(f"Found cached cookies for {url}")

        relayed = False
        try:
            proxy_server, relayed = await self._resolve_proxy(proxy)
            async with self.pool.lease() as slot:
                try:
                    data, cf_detected = await asyncio.wait_for(
                        self.pool.run(self._job, slot.browser, url, proxy_server, cached_cookies, mode),
                        timeout=JOB_TIMEOUT_SECONDS,
                    )
                except BaseException:
                    # the worker thread may still be driving this browser; never hand it out again
                    slot.poisoned = True
                    raise
            if data and self._is_trustworthy(data["cookies"], cf_detected):
                exit_ip = await get_exit_ip(proxy) if IP_CHECK_ENABLED else None
                self.cookie_cache.set(key, data["cookies"], data["user_agent"], exit_ip=exit_ip)
                return data
            if data:
                self.log_message("CF detected but no cf_clearance cookie -- not caching")
            return None
        except asyncio.TimeoutError:
            self.log_message(f"Browser job for {url} exceeded {JOB_TIMEOUT_SECONDS}s; restarting that browser")
            return None
        except Exception as e:
            self.log_message(f"Error running browser for {url}: {e}")
            return None
        finally:
            if relayed:
                await self.relays.release(proxy)

    async def get_or_generate_cookies(
        self, url: str, proxy: Optional[str] = None, bypass_cache: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Get cached cookies or generate new ones; bypass_cache ignores a still-valid entry."""
        key = cache_key(urlparse(url).netloc, proxy)

        if not bypass_cache:
            cached = await self._read_valid_cache(key, proxy)
            if cached:
                return {"cookies": cached.cookies, "user_agent": cached.user_agent}

        requested_at = datetime.now()
        async with _inflight_lock(key):
            # another waiter may have populated the cache while we queued; with bypass_cache,
            # only cookies generated after this request arrived count as fresh
            cached = await self._read_valid_cache(key, proxy)
            if cached and (not bypass_cache or cached.timestamp >= requested_at):
                return {"cookies": cached.cookies, "user_agent": cached.user_agent}

            self.log_message(f"Generating fresh cookies for {key}...")
            return await self._run_in_browser(url, proxy, key, restore_cookies=False, mode="cookies")

    async def get_or_generate_html(self, url: str, proxy: Optional[str] = None, bypass_cache: bool = False) -> Optional[Dict[str, Any]]:
        """Get HTML content along with cookies (cached or fresh)."""
        key = cache_key(urlparse(url).netloc, proxy)
        self.log_message(f"Getting HTML content for {url}...")
        # no in-flight lock: HTML is fetched fresh per request, bounded by the pool size
        return await self._run_in_browser(url, proxy, key, restore_cookies=not bypass_cache, mode="html")

    async def fetch_bytes(self, url: str, proxy: Optional[str] = None, bypass_cache: bool = False) -> Optional[Dict[str, Any]]:
        """Raw response bytes (PDFs, images, JSON...) fetched by the browser after clearing the challenge."""
        key = cache_key(urlparse(url).netloc, proxy)
        self.log_message(f"Fetching bytes for {url}...")
        return await self._run_in_browser(url, proxy, key, restore_cookies=not bypass_cache, mode="fetch")

    async def cleanup(self) -> None:
        """Quit all pooled browsers and close proxy relays."""
        await self.pool.stop()
        await self.relays.close_all()
