import os
import sys

import pytest
import uvicorn
from fastapi.testclient import TestClient

import cf_bypasser.browser.pool as pool_mod
from cf_bypasser.core.bypasser import CFBypasser
from cf_bypasser.core.mirror import RequestMirror


class _States:
    is_alive = True


class FakeBrowser:
    process_id = None

    def __init__(self):
        self.states = _States()

    def quit(self, *a, **k):
        pass


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(pool_mod, "launch_chromium", FakeBrowser)
    monkeypatch.chdir(tmp_path)
    from cf_bypasser.server.app import create_app
    with TestClient(create_app()) as c:
        yield c


def test_browsers_endpoint(api):
    body = api.get("/browsers").json()
    assert body["total"] == pool_mod.BROWSER_COUNT
    assert body["available"] == body["total"] and body["busy"] == 0 and body["restarting"] == 0
    assert len(body["browsers"]) == body["total"]
    assert set(body["browsers"][0]) == {"index", "state", "jobs", "pid"}


def test_browsers_endpoint_not_mirrored(api):
    r = api.get("/browsers", headers={"x-hostname": "example.com"})
    assert r.status_code == 200 and "total" in r.json()


def test_browsers_path_reserved_in_mirror(api):
    assert api.get("/browsers/x", headers={"x-hostname": "example.com"}).status_code == 404


def test_openapi_paths_and_params_unchanged(api):
    spec = api.get("/openapi.json").json()
    assert {"/cookies", "/html", "/cache/clear", "/cache/stats", "/{path}"} <= set(spec["paths"])
    params = lambda p: {x["name"] for x in spec["paths"][p]["get"]["parameters"]}  # noqa: E731
    assert params("/cookies") == {"url", "retries", "proxy", "bypassCookieCache"}
    assert params("/html") == {"url", "retries", "proxy", "bypassCookieCache"}


def test_cookies_response_shape(api, monkeypatch):
    async def fake(self, url, proxy=None, bypass_cache=False):
        return {"cookies": {"cf_clearance": "x"}, "user_agent": "UA"}
    monkeypatch.setattr(CFBypasser, "get_or_generate_cookies", fake)
    r = api.get("/cookies", params={"url": "https://example.com", "retries": 3})
    assert r.status_code == 200
    assert r.json() == {"cookies": {"cf_clearance": "x"}, "user_agent": "UA"}


def test_cookies_bypass_cache_param_forwarded(api, monkeypatch):
    seen = {}

    async def fake(self, url, proxy=None, bypass_cache=False):
        seen["bypass_cache"] = bypass_cache
        return {"cookies": {"a": "1"}, "user_agent": "UA"}
    monkeypatch.setattr(CFBypasser, "get_or_generate_cookies", fake)
    api.get("/cookies", params={"url": "https://example.com", "bypassCookieCache": "true"})
    assert seen["bypass_cache"] is True
    api.get("/cookies", params={"url": "https://example.com"})
    assert seen["bypass_cache"] is False


def test_cookies_failure_detail_unchanged(api, monkeypatch):
    async def fake(self, url, proxy=None, bypass_cache=False):
        return None
    monkeypatch.setattr(CFBypasser, "get_or_generate_cookies", fake)
    r = api.get("/cookies", params={"url": "https://example.com"})
    assert r.status_code == 500 and r.json()["detail"] == "Failed to bypass Cloudflare protection"


def test_html_headers_unchanged(api, monkeypatch):
    async def fake(self, url, proxy=None, bypass_cache=False):
        return {"cookies": {"a": "1"}, "user_agent": "UA", "html": "<p>hi</p>", "url": url, "status_code": 200}
    monkeypatch.setattr(CFBypasser, "get_or_generate_html", fake)
    r = api.get("/html", params={"url": "https://example.com", "bypassCookieCache": "true"})
    assert r.status_code == 200 and r.text == "<p>hi</p>"
    assert r.headers["content-type"].startswith("text/html")
    for h in ("x-cf-bypasser-cookies", "x-cf-bypasser-user-agent", "x-cf-bypasser-final-url", "x-processing-time-ms"):
        assert h in r.headers


def test_mirror_headers_unchanged(api, monkeypatch):
    async def fake(self, **kw):
        return 201, {"content-type": "text/plain"}, b"mirrored"
    monkeypatch.setattr(RequestMirror, "mirror_request", fake)
    r = api.get("/some/path", headers={"x-hostname": "example.com", "x-bypass-cache": "true"})
    assert r.status_code == 201 and r.content == b"mirrored"
    assert r.headers["x-cache-bypassed"] == "true"
    assert "x-cf-bypasser-version" in r.headers and "x-processing-time-ms" in r.headers


def test_fetch_returns_raw_bytes(api, monkeypatch):
    async def fake(self, url, proxy=None, bypass_cache=False):
        return {"cookies": {}, "user_agent": "UA", "body": b"%PDF-1.7\x00\xff", "status_code": 200,
                "content_type": "application/pdf", "url": url}
    monkeypatch.setattr(CFBypasser, "fetch_bytes", fake)
    r = api.get("/fetch", params={"url": "https://example.com/a.pdf"})
    assert r.status_code == 200 and r.content == b"%PDF-1.7\x00\xff"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-cf-bypasser-final-url"] == "https://example.com/a.pdf"


def test_fetch_passes_upstream_status(api, monkeypatch):
    async def fake(self, url, proxy=None, bypass_cache=False):
        return {"cookies": {}, "user_agent": "UA", "body": b"nope", "status_code": 404,
                "content_type": "text/plain", "url": url}
    monkeypatch.setattr(CFBypasser, "fetch_bytes", fake)
    assert api.get("/fetch", params={"url": "https://example.com/x"}).status_code == 404


def test_fetch_rejects_unsafe_url(api):
    assert api.get("/fetch", params={"url": "http://127.0.0.1/x"}).status_code == 400


def test_fetch_failure_is_500(api, monkeypatch):
    async def fake(self, url, proxy=None, bypass_cache=False):
        return None
    monkeypatch.setattr(CFBypasser, "fetch_bytes", fake)
    assert api.get("/fetch", params={"url": "https://example.com/x"}).status_code == 500


def test_cache_endpoints_unchanged(api):
    assert set(api.get("/cache/stats").json()) == {"cached_entries", "total_hostnames", "hostnames"}
    assert set(api.post("/cache/clear").json()) == {"status", "message"}


def test_browsers_arg_sets_env(monkeypatch):
    import server
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["server.py", "--browsers", "5", "--headless"])
    monkeypatch.delenv("CF_BROWSERS", raising=False)
    monkeypatch.delenv("CF_HEADLESS", raising=False)
    server.main()
    assert os.environ["CF_BROWSERS"] == "5"
    assert os.environ["CF_HEADLESS"] == "true"


def test_browsers_arg_rejects_zero(monkeypatch):
    import server
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["server.py", "--browsers", "0"])
    with pytest.raises(SystemExit):
        server.main()
