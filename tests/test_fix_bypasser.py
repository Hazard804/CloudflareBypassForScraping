import asyncio
import time

import pytest

import cf_bypasser.core.bypasser as bp
from cf_bypasser.browser.pool import BrowserPool
from cf_bypasser.browser.relay import RelayRegistry
from cf_bypasser.core import challenge
from cf_bypasser.core.bypasser import CFBypasser
from cf_bypasser.utils.misc import cache_key

CLEAR_HTML = "<html><body>cloudflare challenged passed. You are now a free bot.</body></html>"
BLOCK_HTML = "<html><body>Sorry, you have been blocked. Cloudflare Ray ID: abc error 1020</body></html>"
NON_CF_HTML = "<html><body>just a normal site</body></html>"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(challenge, "sleep", lambda s: None)
    monkeypatch.setattr(challenge, "CHALLENGE_TIMEOUT_SECONDS", 0.05)
    bp._inflight_locks.clear()


class _Waiter:
    def doc_loaded(self, *a, **k):
        return True


class _LoadMode:
    def __init__(self):
        self.eager_called = False

    def eager(self):
        self.eager_called = True


class _Set:
    def __init__(self):
        self.load_mode = _LoadMode()


class FakeTab:
    def __init__(self, html=CLEAR_HTML, title="ok", html_raises=False, status=200):
        self._html = html
        self.title = title
        self._html_raises = html_raises
        self._status = status
        self.url = "https://example.com"
        self.wait = _Waiter()
        self.set = _Set()
        self.clicks = 0

    @property
    def html(self):
        if self._html_raises:
            raise RuntimeError("content read failed")
        return self._html

    def get(self, url, **k):
        return True

    def run_js(self, script):
        return self._status

    def ele(self, *a, **k):
        return None

    def eles(self, *a, **k):
        return []


class _States:
    is_alive = True


class FakeBrowser:
    process_id = None

    def __init__(self):
        self.states = _States()

    def quit(self, *a, **k):
        self.states = type("S", (), {"is_alive": False})()


def make_bypasser(tmp_path, size=2, **kw):
    pool = BrowserPool(size, launcher=FakeBrowser)
    return CFBypasser(log=False, cache_file=str(tmp_path / "cache.json"), pool=pool, **kw)


def fake_job(cookies, cf_detected=False, delay=0.0, tracker=None):
    def job(self, browser, url, proxy_server, cached_cookies, want_html):
        if tracker is not None:
            tracker.append((proxy_server, cached_cookies))
        if delay:
            time.sleep(delay)
        data = {"cookies": dict(cookies), "user_agent": "FakeUA/1.0"}
        if want_html:
            data.update(html=CLEAR_HTML, url=url, status_code=200)
        return data, cf_detected
    return job


def test_content_raises_returns_false():
    result = challenge.solve(FakeTab(html_raises=True), "https://x.com")
    assert result.success is False


def test_block_page_not_bypassed():
    tab = FakeTab(html=BLOCK_HTML, title="Access denied")
    assert challenge.is_bypassed(tab) is False
    success, cf_detected, _ = challenge.solve(tab, "https://x.com")
    assert cf_detected is True
    assert success is False


def test_clear_non_cf_page_succeeds():
    tab = FakeTab(html=NON_CF_HTML, title="home")
    assert challenge.is_bypassed(tab) is True
    success, cf_detected, _ = challenge.solve(tab, "https://x.com")
    assert success is True and cf_detected is False


def test_challenge_title_not_bypassed():
    assert challenge.is_bypassed(FakeTab(title="Just a moment...")) is False


def test_solve_never_uses_eager_load_mode():
    # eager stops loading at DOMContentLoaded and cancels Cloudflare's challenge script
    tab = FakeTab(html=NON_CF_HTML)
    challenge.solve(tab, "https://x.com")
    assert tab.set.load_mode.eager_called is False


def test_status_is_from_page_after_challenge(monkeypatch):
    tab = FakeTab(html="<html>cloudflare challenge</html>", title="Just a moment...", status=403)

    def clear(_):
        tab.title, tab._html, tab._status = "home", "<html>welcome</html>", 200

    monkeypatch.setattr(challenge, "sleep", clear)
    result = challenge.solve(tab, "https://x.com")
    assert result.success is True and result.cf_detected is True
    assert result.status == 200


def test_polls_fast_and_clicks_with_cooldown(monkeypatch):
    clock = {"t": 0.0}
    clicks = []
    monkeypatch.setattr(challenge, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(challenge, "CHALLENGE_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(challenge, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(challenge, "click_turnstile", lambda tab, log: clicks.append(clock["t"]) or True)
    tab = FakeTab(html="<html>cloudflare challenge</html>", title="Just a moment...")
    result = challenge.solve(tab, "https://x.com")
    assert result.success is False
    assert clicks[0] == 0.0  # first click attempt immediately, no fixed settle wait
    assert all(b - a >= challenge.CLICK_COOLDOWN_SECONDS for a, b in zip(clicks, clicks[1:]))
    assert challenge.CHALLENGE_POLL_SECONDS <= 0.25


def test_retries_click_lookup_every_poll_until_found(monkeypatch):
    clock = {"t": 0.0}
    attempts = []
    monkeypatch.setattr(challenge, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(challenge, "CHALLENGE_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(challenge, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(challenge, "click_turnstile", lambda tab, log: attempts.append(clock["t"]) and False)
    challenge.solve(FakeTab(html="<html>cloudflare challenge</html>", title="Just a moment..."), "https://x.com")
    assert len(attempts) >= 8  # widget not found yet -> look again every poll, not every 3s


def test_nav_status_reported():
    assert challenge.solve(FakeTab(html=NON_CF_HTML, status=403), "https://x.com").status == 403


class _Owner:
    def __init__(self, root, name):
        self.root = root
        self.name = name

    def _run_cdp(self, cmd, **kw):
        assert cmd == "DOM.getDocument" and kw["pierce"] is True
        return {"root": self.root}


def _n(name, be, attrs=(), children=(), shadow=(), doc=None):
    node = {"nodeName": name, "backendNodeId": be, "attributes": list(attrs), "children": list(children)}
    if shadow:
        node["shadowRoots"] = list(shadow)
    if doc:
        node["contentDocument"] = doc
    return node


def _sr(kind, children):
    return {"nodeName": "#document-fragment", "backendNodeId": 0, "shadowRootType": kind, "children": children}


@pytest.fixture
def fake_dom(monkeypatch):
    frames = {}
    monkeypatch.setattr(challenge, "ChromiumElement", lambda owner, backend_id: (owner.name, backend_id))
    monkeypatch.setattr(challenge, "ChromiumFrame", lambda owner, ele: frames[ele[1]])
    return frames


def test_checkbox_in_closed_shadow_inside_cross_origin_iframe(fake_dom):
    # page > div > #closed > iframe(OOPIF) ; frame > body > #closed > input[checkbox]
    frame_root = _n("#document", 1, children=[_n("BODY", 2, shadow=[_sr("closed", [
        _n("INPUT", 38, ["type", "checkbox", "aria-label", "Verify you are human"])])])])
    fake_dom[17] = _Owner(frame_root, "frame")
    page_root = _n("#document", 1, children=[
        _n("IMG", 5, shadow=[_sr("user-agent", [_n("INPUT", 6, ["type", "checkbox"])])]),
        _n("DIV", 37, children=[
            _n("DIV", 26, shadow=[_sr("closed", [_n("IFRAME", 17, ["src", "https://challenges.cloudflare.com/x"])])]),
            _n("INPUT", 39, ["type", "hidden", "name", "cf-turnstile-response"]),
        ]),
    ])
    assert challenge.find_turnstile_checkbox(_Owner(page_root, "page")) == ("frame", 38)


def test_checkbox_in_same_origin_iframe_content_document(fake_dom):
    inner = _n("#document", 3, children=[_n("INPUT", 9, ["type", "checkbox"])])
    root = _n("#document", 1, children=[_n("IFRAME", 2, doc=inner)])
    assert challenge.find_turnstile_checkbox(_Owner(root, "page")) == ("page", 9)


def test_user_agent_shadow_roots_ignored(fake_dom):
    root = _n("#document", 1, children=[_n("INPUT", 4, shadow=[_sr("user-agent", [_n("INPUT", 5, ["type", "checkbox"])])])])
    assert challenge.find_turnstile_checkbox(_Owner(root, "page")) is None


def test_broken_iframe_skipped(fake_dom):
    root = _n("#document", 1, children=[_n("IFRAME", 99), _n("IFRAME", 17)])
    fake_dom[17] = _Owner(_n("#document", 1, children=[_n("INPUT", 7, ["type", "checkbox"])]), "f17")
    assert challenge.find_turnstile_checkbox(_Owner(root, "page")) == ("f17", 7)


async def test_cf_detected_no_clearance_not_cached(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path)
    await b.start()
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"foo": "bar"}, cf_detected=True))
    assert await b.get_or_generate_cookies("https://example.com") is None
    assert b.cookie_cache.get(cache_key("example.com", None)) is None
    await b.cleanup()


async def test_real_clearance_is_cached(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path)
    await b.start()
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"cf_clearance": "xyz"}, cf_detected=True))
    result = await b.get_or_generate_cookies("https://example.com")
    assert result["cookies"]["cf_clearance"] == "xyz"
    assert b.cookie_cache.get(cache_key("example.com", None)) is not None
    await b.cleanup()


async def test_non_cf_caches_without_clearance(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path)
    await b.start()
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"session": "1"}))
    assert await b.get_or_generate_cookies("https://example.com") is not None
    assert b.cookie_cache.get(cache_key("example.com", None)) is not None
    await b.cleanup()


async def test_html_returns_html_and_restores_cache(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path)
    await b.start()
    calls = []
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"cf_clearance": "c"}, cf_detected=True, tracker=calls))
    first = await b.get_or_generate_html("https://example.com")
    assert first["html"] == CLEAR_HTML and first["status_code"] == 200
    await b.get_or_generate_html("https://example.com")
    await b.get_or_generate_html("https://example.com", bypass_cache=True)
    assert [c[1] for c in calls] == [None, {"cf_clearance": "c"}, None]
    await b.cleanup()


async def test_concurrency_capped_by_pool_size(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path, size=3)
    await b.start()
    live = 0
    peak = 0
    import threading
    lock = threading.Lock()

    def job(self, browser, url, proxy_server, cached_cookies, want_html):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.1)
        with lock:
            live -= 1
        return {"cookies": {}, "user_agent": "ua", "html": "", "url": url, "status_code": 200}, False

    monkeypatch.setattr(CFBypasser, "_job", job)
    results = await asyncio.gather(*(b.get_or_generate_html(f"https://s{i}.com") for i in range(8)))
    assert all(results)
    assert peak == 3
    await b.cleanup()


async def test_bypass_cache_forces_fresh_cookies(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path)
    await b.start()
    calls = []
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"s": "1"}, tracker=calls))
    await b.get_or_generate_cookies("https://same.com")
    await b.get_or_generate_cookies("https://same.com")
    assert len(calls) == 1
    await b.get_or_generate_cookies("https://same.com", bypass_cache=True)
    assert len(calls) == 2
    await b.cleanup()


async def test_inflight_dedup(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path)
    await b.start()
    calls = []
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"s": "1"}, delay=0.05, tracker=calls))
    r1, r2 = await asyncio.gather(
        b.get_or_generate_cookies("https://same.com"),
        b.get_or_generate_cookies("https://same.com"),
    )
    assert r1 is not None and r2 is not None
    assert len(calls) == 1
    await b.cleanup()


async def test_job_timeout_poisons_slot(tmp_path, monkeypatch):
    launches = []

    def launcher():
        launches.append(1)
        return FakeBrowser()

    b = CFBypasser(log=False, cache_file=str(tmp_path / "c.json"), pool=BrowserPool(1, launcher=launcher))
    await b.start()
    monkeypatch.setattr(bp, "JOB_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"s": "1"}, delay=0.5))
    assert await b.get_or_generate_cookies("https://slow.com") is None
    await asyncio.gather(*list(b.pool._bg))
    assert len(launches) == 2
    await b.cleanup()


async def test_job_exception_returns_none_and_slot_reusable(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path, size=1)
    await b.start()

    def boom(self, *a):
        raise RuntimeError("cdp died")

    monkeypatch.setattr(CFBypasser, "_job", boom)
    assert await b.get_or_generate_cookies("https://x.com") is None
    await asyncio.gather(*list(b.pool._bg))
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"s": "1"}))
    async with asyncio.timeout(2):
        assert await b.get_or_generate_cookies("https://y.com") is not None
    await b.cleanup()


async def test_cancelled_request_poisons_slot(tmp_path, monkeypatch):
    launches = []

    def launcher():
        launches.append(1)
        return FakeBrowser()

    b = CFBypasser(log=False, cache_file=str(tmp_path / "c.json"), pool=BrowserPool(1, launcher=launcher))
    await b.start()
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"s": "1"}, delay=0.3))
    task = asyncio.create_task(b.get_or_generate_html("https://x.com"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.gather(*list(b.pool._bg))
    assert len(launches) == 2
    await b.cleanup()


async def test_authed_proxy_uses_relay(tmp_path, monkeypatch):
    relays = RelayRegistry()
    b = make_bypasser(tmp_path, relays=relays)
    await b.start()
    calls = []
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"s": "1"}, tracker=calls))
    await b.get_or_generate_cookies("https://x.com", proxy="http://u:p@1.2.3.4:8080")
    assert calls[0][0].startswith("http://127.0.0.1:")
    assert relays._relays == {}
    await b.cleanup()


async def test_plain_proxy_passed_through(tmp_path, monkeypatch):
    relays = RelayRegistry()
    b = make_bypasser(tmp_path, relays=relays)
    await b.start()
    calls = []
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"s": "1"}, tracker=calls))
    await b.get_or_generate_cookies("https://x.com", proxy="http://1.2.3.4:8080")
    assert calls[0][0] == "http://1.2.3.4:8080"
    assert relays._refs == {}
    await b.cleanup()


async def test_invalid_proxy_never_runs_direct(tmp_path, monkeypatch):
    b = make_bypasser(tmp_path)
    await b.start()
    calls = []
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"s": "1"}, tracker=calls))
    assert await b.get_or_generate_cookies("https://x.com", proxy="not-a-valid-proxy") is None
    assert calls == []
    await b.cleanup()


def test_cache_key_no_collision():
    assert cache_key("foo.com", "bar") != cache_key("foo.comb", "ar")


def _ip_check_setup(monkeypatch, ip_sequence):
    monkeypatch.setattr(bp, "IP_CHECK_ENABLED", True)
    ips = iter(ip_sequence)

    async def fake_get_exit_ip(proxy=None):
        return next(ips)

    monkeypatch.setattr(bp, "get_exit_ip", fake_get_exit_ip)
    calls = []
    monkeypatch.setattr(CFBypasser, "_job", fake_job({"cf_clearance": "x"}, tracker=calls))
    return calls


async def test_ip_check_invalidates_on_rotation(tmp_path, monkeypatch):
    calls = _ip_check_setup(monkeypatch, ["1.1.1.1", "2.2.2.2", "2.2.2.2"])
    b = make_bypasser(tmp_path)
    await b.start()
    assert await b.get_or_generate_cookies("https://site.com") is not None
    assert await b.get_or_generate_cookies("https://site.com") is not None
    assert len(calls) == 2
    await b.cleanup()


async def test_ip_check_keeps_cache_when_ip_stable(tmp_path, monkeypatch):
    calls = _ip_check_setup(monkeypatch, ["9.9.9.9", "9.9.9.9"])
    b = make_bypasser(tmp_path)
    await b.start()
    assert await b.get_or_generate_cookies("https://site.com") is not None
    assert await b.get_or_generate_cookies("https://site.com") is not None
    assert len(calls) == 1
    await b.cleanup()
