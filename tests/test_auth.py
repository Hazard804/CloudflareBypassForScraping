import os
import sys

import pytest
import uvicorn
from fastapi.testclient import TestClient

from cf_bypasser.core.bypasser import CFBypasser
from cf_bypasser.core.mirror import RequestMirror

TOKEN = "s3cret-token"
PROTECTED = [
    ("GET", "/cookies?url=https://example.com"),
    ("GET", "/html?url=https://example.com"),
    ("GET", "/fetch?url=https://example.com/a.pdf"),
    ("GET", "/browsers"),
    ("GET", "/cache/stats"),
    ("POST", "/cache/clear"),
    ("GET", "/anything/mirrored"),
    ("POST", "/anything/mirrored"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
]


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    async def fake_cookies(self, url, proxy=None, bypass_cache=False):
        return {"cookies": {"cf_clearance": "x"}, "user_agent": "UA"}

    async def fake_mirror(self, **kw):
        return 200, {"content-type": "text/plain"}, b"mirrored"

    monkeypatch.setattr(CFBypasser, "get_or_generate_cookies", fake_cookies)
    monkeypatch.setattr(RequestMirror, "mirror_request", fake_mirror)
    clients = []

    def make(token=None):
        if token is None:
            monkeypatch.delenv("CF_AUTH_TOKEN", raising=False)
        else:
            monkeypatch.setenv("CF_AUTH_TOKEN", token)
        from cf_bypasser.server.app import create_app
        c = TestClient(create_app())
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def test_disabled_by_default(make_client):
    c = make_client()
    assert c.get("/cookies", params={"url": "https://example.com"}).status_code == 200
    assert c.get("/browsers").status_code == 200


def test_empty_token_means_disabled(make_client):
    c = make_client("")
    assert c.get("/browsers").status_code == 200


@pytest.mark.parametrize("method,path", PROTECTED)
def test_missing_token_rejected_everywhere(make_client, method, path):
    c = make_client(TOKEN)
    r = c.request(method, path, headers={"x-hostname": "example.com"} if "mirrored" in path else {})
    assert r.status_code == 401
    assert "x-auth-token" in r.json()["detail"]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_wrong_token_rejected_everywhere(make_client, method, path):
    c = make_client(TOKEN)
    headers = {"x-auth-token": "wrong"}
    if "mirrored" in path:
        headers["x-hostname"] = "example.com"
    assert c.request(method, path, headers=headers).status_code == 401


def test_right_token_accepted(make_client):
    c = make_client(TOKEN)
    ok = {"x-auth-token": TOKEN}
    assert c.get("/cookies", params={"url": "https://example.com"}, headers=ok).json()["cookies"] == {"cf_clearance": "x"}
    assert c.get("/browsers", headers=ok).status_code == 200
    assert c.get("/x", headers={**ok, "x-hostname": "example.com"}).content == b"mirrored"


def test_cors_preflight_still_answered(make_client):
    c = make_client(TOKEN)
    r = c.options("/cookies", headers={"Origin": "https://app.example", "Access-Control-Request-Method": "GET",
                                       "Access-Control-Request-Headers": "x-auth-token"})
    assert r.status_code == 200


def test_mirror_never_forwards_auth_token():
    mirror = RequestMirror(bypasser=object())
    headers = mirror._prepare_request_headers(
        {"x-auth-token": TOKEN, "X-Auth-Token": TOKEN, "x-hostname": "example.com", "accept": "*/*"},
        {"cookies": {}, "user_agent": "UA"},
    )
    assert not any(k.lower() == "x-auth-token" for k in headers)
    assert TOKEN not in str(headers)


def test_auth_token_arg_sets_env(monkeypatch):
    import server
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["server.py", "--auth-token", TOKEN])
    monkeypatch.setenv("CF_AUTH_TOKEN", "")  # registered so monkeypatch restores it; main() writes os.environ
    server.main()
    assert os.environ["CF_AUTH_TOKEN"] == TOKEN


def test_enable_cache_arg_sets_env(monkeypatch):
    import server
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["server.py", "--enable-cache"])
    monkeypatch.setenv("CF_CACHE_ENABLED", "false")
    server.main()
    assert os.environ["CF_CACHE_ENABLED"] == "true"


def test_no_cache_arg_disables_caching(monkeypatch):
    import server
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["server.py", "--no-cache"])
    monkeypatch.setenv("CF_CACHE_ENABLED", "true")
    server.main()
    assert os.environ["CF_CACHE_ENABLED"] == "false"
