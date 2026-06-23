import socket

import pytest

from cf_bypasser.server import routes


def fake_getaddrinfo(ip):
    """Return a getaddrinfo stub resolving every host to the given IP."""
    def _inner(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    return _inner


def raising_getaddrinfo(*args, **kwargs):
    raise socket.gaierror("name resolution failed")


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    # default: non-literal hosts resolve to a public IP unless a test overrides
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))


def _client_with_capturing_mirror():
    """TestClient whose mirror dependency records the args it was handed."""
    from fastapi.testclient import TestClient
    from cf_bypasser.server.app import create_app
    from cf_bypasser.server.routes import get_mirror

    captured = {}

    class FakeMirror:
        async def mirror_request(self, **kwargs):
            captured.update(kwargs)
            return 200, {}, b"ok"

    app = create_app()
    app.dependency_overrides[get_mirror] = lambda: FakeMirror()
    return TestClient(app), captured


def _client_with_refreshing_bypasser(result):
    from fastapi.testclient import TestClient
    from cf_bypasser.server.app import create_app
    from cf_bypasser.server.routes import get_bypasser

    captured = {}

    class FakeBypasser:
        async def refresh_cookies(self, url, proxy=None):
            captured["url"] = url
            captured["proxy"] = proxy
            return result

    app = create_app()
    app.dependency_overrides[get_bypasser] = lambda: FakeBypasser()
    return TestClient(app), captured


def test_xhostname_full_url_is_normalized():
    client, captured = _client_with_capturing_mirror()
    resp = client.get("/foo/bar", headers={"x-hostname": "https://example.com/x"})
    assert resp.status_code == 200
    assert captured["hostname"] == "example.com"


def test_xhostname_bare_host_unchanged():
    client, captured = _client_with_capturing_mirror()
    resp = client.get("/foo", headers={"x-hostname": "example.com"})
    assert resp.status_code == 200
    assert captured["hostname"] == "example.com"


ALLOWED_HTTP_URLS = [
    "https://2130706433",
    "http://0x7f000001",
    "http://0177.0.0.1",
    "http://169.254.169.254",
    "https://127.0.0.2",
    "http://[::ffff:127.0.0.1]",
]


@pytest.mark.parametrize("url", ALLOWED_HTTP_URLS)
def test_http_urls_are_allowed_without_ssrf_filtering(url):
    assert routes.is_safe_url(url) is True


def test_public_ip_literal_allowed():
    assert routes.is_safe_url("https://93.184.216.34") is True


def test_public_hostname_allowed():
    assert routes.is_safe_url("https://challenge.sarper.me") is True


def test_hostname_resolution_is_not_used_for_safety(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("10.0.0.5"))
    assert routes.is_safe_url("https://evil.example.com") is True


def test_empty_host_rejected():
    assert routes.is_safe_url("https://") is False
    assert routes.is_safe_url("not a url") is False


def test_file_scheme_rejected():
    assert routes.is_safe_url("file:///etc/passwd") is False


def test_resolution_exception_does_not_block_http_url(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", raising_getaddrinfo)
    assert routes.is_safe_url("https://whatever.example.com") is True


def test_empty_resolution_does_not_block_http_url(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    assert routes.is_safe_url("https://whatever.example.com") is True


def test_classic_private_literals_allowed():
    for url in [
        "http://127.0.0.1",
        "http://10.1.2.3",
        "http://192.168.1.1",
        "http://172.16.5.5",
        "http://0.0.0.0",
        "http://[::1]",
    ]:
        assert routes.is_safe_url(url) is True


def test_localhost_name_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("127.0.0.1"))
    assert routes.is_safe_url("http://localhost") is True


def test_hostname_with_any_public_resolved_ip_allowed(monkeypatch):
    def multi(host, *a, **k):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]
    monkeypatch.setattr(socket, "getaddrinfo", multi)
    assert routes.is_safe_url("https://mixed.example.com") is True


def test_cache_refresh_uses_bypasser_and_returns_metadata():
    client, captured = _client_with_refreshing_bypasser({
        "cookies": {"cf_clearance": "ok", "session": "1"},
        "user_agent": "FakeUA/1.0",
    })

    resp = client.post(
        "/cache/refresh",
        params={"url": "https://example.com/path", "proxy": "http://proxy.example:8080"},
    )

    assert resp.status_code == 200
    assert captured == {
        "url": "https://example.com/path",
        "proxy": "http://proxy.example:8080",
    }
    body = resp.json()
    assert body["status"] == "success"
    assert body["hostname"] == "example.com"
    assert body["cookies_count"] == 2
    assert body["user_agent"] == "FakeUA/1.0"


def test_cache_refresh_allows_localhost_when_ssrf_filtering_is_disabled():
    client, captured = _client_with_refreshing_bypasser({
        "cookies": {"cf_clearance": "ok"},
        "user_agent": "FakeUA/1.0",
    })

    resp = client.post("/cache/refresh", params={"url": "http://127.0.0.1"})

    assert resp.status_code == 200
    assert captured == {"url": "http://127.0.0.1", "proxy": None}
