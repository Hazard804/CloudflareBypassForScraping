"""Page-state classification and the trust decision built on it.

Regression cover for the 2026-11-04 fix: classification used to be `"cloudflare" in html`, so any
page carrying Cloudflare's jsd/insights injection looked like a challenge, and a site without an
active challenge (which never issues cf_clearance) was reported as a failed bypass.
"""

import pytest

import cf_bypasser.browser.pool as pool_mod
import cf_bypasser.core.bypasser as bp
from cf_bypasser.browser.pool import BrowserPool
from cf_bypasser.core import challenge as ch
from cf_bypasser.core.bypasser import CFBypasser

# A realistic site-side age gate: it carries Cloudflare's beacon and jsd script (so the old
# keyword check matched) but there is nothing to solve.
BEACON_ONLY_HTML = """<!DOCTYPE html><html><head><title>DMN12.VIP</title>
<meta charset="utf-8"></head><body>
<script>var safeid='abc123';</script>
<a href="./" class="enter-btn">enter</a>
<script src="https://static.cloudflareinsights.com/beacon.min.js"></script>
<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js?ray=abc"></script>
</body></html>"""

# A challenge variant whose body never spells "cloudflare"; only the widget identifies it.
WIDGET_ONLY_HTML = """<html><head><title>请稍候...</title></head><body>
<div id="cf-chl-widget-7fa2"></div></body></html>"""

BLOCK_HTML = """<html><head><title>Access denied</title></head>
<body><h1>Sorry, you have been blocked</h1></body></html>"""

PLAIN_HTML = "<html><head><title>home</title></head><body>hello</body></html>"


class FakeTab:
    def __init__(self, html=PLAIN_HTML, title="home", status=200):
        self._html = html
        self.title = title
        self._status = status
        self.url = "https://example.com/"

    @property
    def html(self):
        return self._html

    def get(self, url, **kw):
        return True

    def run_js(self, script):
        return self._status


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(ch, "sleep", lambda s: None)
    monkeypatch.setattr(ch, "CHALLENGE_TIMEOUT_SECONDS", 0.05)
    bp._inflight_locks.clear()


def test_beacon_only_page_is_not_a_challenge():
    tab = FakeTab(html=BEACON_ONLY_HTML, title="DMN12.VIP")
    # the old predicate ("cloudflare" in html) matched this page
    assert "cloudflare" in BEACON_ONLY_HTML.lower()

    assert ch.detect_state(BEACON_ONLY_HTML, tab.title) == ch.STATE_NONE
    result = ch.solve(tab, "https://example.com/")
    assert result.success is True
    assert result.cf_detected is False
    assert result.state == ch.STATE_NONE


def test_normal_page_referencing_cloudflare_assets_is_not_a_challenge():
    html = (
        "<html><head><title>板块 - Discuz!</title></head><body>"
        '<script src="https://static.cloudflareinsights.com/beacon.min.js"></script></body></html>'
    )
    assert ch.detect_state(html, "板块 - Discuz!") == ch.STATE_NONE


def test_widget_only_challenge_is_detected_without_the_cloudflare_word(monkeypatch):
    assert "cloudflare" not in WIDGET_ONLY_HTML.lower()
    assert ch.detect_state(WIDGET_ONLY_HTML, "请稍候...") == ch.STATE_INTERACTIVE

    tab = FakeTab(html=WIDGET_ONLY_HTML, title="请稍候...")

    def clear(_):
        tab.title, tab._html = "home", PLAIN_HTML

    monkeypatch.setattr(ch, "sleep", clear)
    result = ch.solve(tab, "https://example.com/")
    assert result.success is True
    assert result.cf_detected is True
    assert result.state == ch.STATE_INTERACTIVE


def test_block_page_fails_without_polling(monkeypatch):
    def boom(_):
        raise AssertionError("a block page must not be polled")

    monkeypatch.setattr(ch, "sleep", boom)
    monkeypatch.setattr(ch, "click_turnstile", lambda tab, log: pytest.fail("must not click"))

    result = ch.solve(FakeTab(html=BLOCK_HTML, title="Access denied"), "https://example.com/")
    assert result.success is False
    assert result.cf_detected is True
    assert result.state == ch.STATE_BLOCK


def test_solve_result_still_unpacks_as_a_triple():
    success, cf_detected, status = ch.solve(FakeTab(), "https://example.com/")
    assert (success, cf_detected) == (True, False)
    assert status == 200


@pytest.mark.parametrize(
    "state,cookies,expected",
    [
        (ch.STATE_NONE, {}, True),
        (ch.STATE_NONE, {"session": "1"}, True),
        (ch.STATE_INTERACTIVE, {}, False),
        (ch.STATE_INTERACTIVE, {"cf_clearance": "x"}, True),
        (ch.STATE_BLOCK, {"cf_clearance": "x"}, False),
        (ch.STATE_ERROR, {"cf_clearance": "x"}, False),
        # legacy boolean callers keep the old "clearance required when CF detected" rule
        (True, {}, False),
        (True, {"cf_clearance": "x"}, True),
        (False, {}, True),
        # unknown state (a hand-built ChallengeResult) must not silently become trustworthy
        (None, {}, False),
        (None, {"cf_clearance": "x"}, True),
    ],
)
def test_is_trustworthy_by_state(state, cookies, expected):
    assert CFBypasser._is_trustworthy(cookies, state) is expected


class _States:
    is_alive = True


class FakeBrowser:
    process_id = None

    def __init__(self):
        self.states = _States()

    def quit(self, *a, **k):
        self.states = type("S", (), {"is_alive": False})()


def make_bypasser(tmp_path):
    return CFBypasser(
        log=False,
        cache_file=str(tmp_path / "cache.json"),
        pool=BrowserPool(1, launcher=FakeBrowser),
        cache_enabled=True,
    )


def job_with(state, cookies):
    def job(self, browser, url, proxy_server, cached_cookies, mode):
        return {"cookies": dict(cookies), "user_agent": "UA"}, state
    return job


async def test_detailed_reports_missing_clearance_for_interactive(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path)
    await b.start()
    monkeypatch.setattr(CFBypasser, "_job", job_with(ch.STATE_INTERACTIVE, {"session": "1"}))
    data, reason = await b.get_cookies_detailed("https://example.com")
    assert data is None and reason == "cookie_missing_clearance"
    await b.cleanup()


async def test_detailed_succeeds_and_caches_without_clearance_for_none_state(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path)
    await b.start()
    monkeypatch.setattr(CFBypasser, "_job", job_with(ch.STATE_NONE, {"session": "1"}))
    data, reason = await b.get_cookies_detailed("https://example.com")
    assert reason == ""
    assert data["cookies"] == {"session": "1"}
    await b.cleanup()


@pytest.mark.parametrize(
    "state,reason",
    [
        (ch.STATE_BLOCK, "blocked"),
        (ch.STATE_INTERACTIVE, "challenge_timeout"),
        (ch.STATE_ERROR, "navigation_failed"),
    ],
)
async def test_detailed_maps_failure_state_to_reason(tmp_path, monkeypatch, state, reason):
    b = make_bypasser(tmp_path)
    await b.start()

    def failed(self, browser, url, proxy_server, cached_cookies, mode):
        return None, state

    monkeypatch.setattr(CFBypasser, "_job", failed)
    data, got = await b.get_cookies_detailed("https://example.com")
    assert data is None and got == reason
    await b.cleanup()


def test_error_code_surfaced_without_changing_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(pool_mod, "launch_chromium", FakeBrowser)
    monkeypatch.chdir(tmp_path)
    from fastapi.testclient import TestClient

    from cf_bypasser.server.app import create_app

    async def fake(self, url, proxy=None, bypass_cache=False):
        return None, "blocked"

    monkeypatch.setattr(CFBypasser, "get_cookies_detailed", fake)
    with TestClient(create_app()) as client:
        r = client.get("/cookies", params={"url": "https://example.com"})
    assert r.status_code == 500
    assert r.json()["detail"] == "Failed to bypass Cloudflare protection"
    assert r.json()["error_code"] == "blocked"
