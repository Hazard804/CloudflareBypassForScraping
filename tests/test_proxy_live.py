import asyncio
import os

import pytest
from curl_cffi.requests import AsyncSession

from cf_bypasser.browser.context import BrowserContextSession
from cf_bypasser.browser.pool import BrowserPool
from cf_bypasser.core.bypasser import CFBypasser

# comma-separated http://user:pass@host:port; never commit real credentials
PROXIES = [p.strip() for p in os.environ.get("CF_TEST_PROXY", "").split(",") if p.strip()]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not PROXIES, reason="set CF_TEST_PROXY to run proxy tests"),
]


async def _curl_exit_ip(proxy):
    async with AsyncSession() as s:
        r = await s.get("https://api.ipify.org", proxies={"http": proxy, "https": proxy} if proxy else None, timeout=20)
        return r.text.strip()


def _browser_exit_ip(browser, proxy_server):
    with BrowserContextSession(browser, proxy_server) as s:
        s.tab.get("https://api.ipify.org", retry=0, timeout=30)
        return s.tab.ele("tag:body").text.strip()


@pytest.fixture
async def bypasser(tmp_path):
    b = CFBypasser(log=False, cache_file=str(tmp_path / "cache.json"), pool=BrowserPool(1))
    await b.start()
    yield b
    await b.cleanup()


async def test_browser_exits_through_each_proxy_without_restart(bypasser):
    pids = set()
    for proxy in PROXIES + [None]:
        expected = await _curl_exit_ip(proxy)
        proxy_server, relayed = await bypasser._resolve_proxy(proxy)
        try:
            async with bypasser.pool.lease() as slot:
                pids.add(slot.browser.process_id)
                got = await asyncio.to_thread(_browser_exit_ip, slot.browser, proxy_server)
        finally:
            if relayed:
                await bypasser.relays.release(proxy)
        assert got == expected
    assert len(pids) == 1
    assert bypasser.relays._relays == {}


async def test_cookies_through_proxy(bypasser, test_url):
    data = await bypasser.get_or_generate_cookies(test_url, proxy=PROXIES[0])
    assert data is not None and data["cookies"]
