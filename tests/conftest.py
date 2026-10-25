import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import os
from urllib.parse import urlparse

# public Cloudflare-protected test site; override with CF_TEST_URL / CF_TEST_EXPECTED_TEXT
TEST_URL = os.environ.get("CF_TEST_URL", "https://cloudflare.hackmap.win/")
TEST_HOST = urlparse(TEST_URL).netloc
EXPECTED_SUCCESS_TEXT = os.environ.get("CF_TEST_EXPECTED_TEXT", "OK YOU PASSED IT")
SERVER_URL = "http://localhost:8000"
CF_COOKIE_PREFIXES = ("cf_", "__cf")
MIN_HTML_LENGTH = 50


@pytest.fixture
def test_url():
    """Provide the test URL with Cloudflare challenge."""
    return TEST_URL


@pytest.fixture
def expected_text():
    """Text the page shows once past the challenge ("" = only check the bypass itself)."""
    return EXPECTED_SUCCESS_TEXT


@pytest.fixture
def test_host():
    """Bare host of the test site, for x-hostname mirroring."""
    return TEST_HOST


@pytest.fixture
def server_url():
    """Provide the running server URL for live/network tests."""
    return SERVER_URL


@pytest.fixture
def cf_cookie_prefixes():
    """Cookie-name prefixes that mark a Cloudflare clearance cookie."""
    return CF_COOKIE_PREFIXES


@pytest.fixture
def min_html_length():
    """Minimum plausible length for a real bypassed HTML body."""
    return MIN_HTML_LENGTH


@pytest.fixture
def client():
    """FastAPI TestClient against a freshly created app (runs lifespan so the singletons exist)."""
    from cf_bypasser.server.app import create_app
    with TestClient(create_app()) as c:
        yield c


@pytest_asyncio.fixture
async def bypasser():
    """Started CFBypasser backed by a real browser pool of one."""
    from cf_bypasser.browser.pool import BrowserPool
    from cf_bypasser.core.bypasser import CFBypasser
    instance = CFBypasser(log=True, pool=BrowserPool(1), cache_enabled=True)
    await instance.start()
    yield instance
    await instance.cleanup()


# One real cookie-gen / HTML fetch shared across the whole session so the live
# suite launches a browser once instead of per test.
@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def shared_cookies():
    from cf_bypasser.browser.pool import BrowserPool
    from cf_bypasser.core.bypasser import CFBypasser
    instance = CFBypasser(log=True, pool=BrowserPool(1))
    await instance.start()
    try:
        return await instance.get_or_generate_cookies(TEST_URL)
    finally:
        await instance.cleanup()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def shared_html():
    from cf_bypasser.browser.pool import BrowserPool
    from cf_bypasser.core.bypasser import CFBypasser
    instance = CFBypasser(log=True, pool=BrowserPool(1))
    await instance.start()
    try:
        return await instance.get_or_generate_html(TEST_URL)
    finally:
        await instance.cleanup()


class _FakeStates:
    is_alive = True


class _FakeBrowser:
    process_id = None

    def __init__(self, *a, **k):
        self.states = _FakeStates()

    def quit(self, *a, **k):
        self.states = type("S", (), {"is_alive": False})()


@pytest.fixture(autouse=True)
def _no_real_browsers(request, monkeypatch):
    """Only tests marked live may launch a real browser."""
    if request.node.get_closest_marker("live"):
        return
    import cf_bypasser.browser.pool as pool_mod
    monkeypatch.setattr(pool_mod, "launch_chromium", _FakeBrowser)
