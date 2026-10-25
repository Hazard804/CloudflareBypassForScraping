import logging

import pytest

from cf_bypasser.browser.pool import BrowserPool
from cf_bypasser.core.bypasser import CFBypasser
from cf_bypasser.utils.security import redact_proxy

SECRET = "pa/ss?w#rd"


def test_redact_proxy_hides_only_the_password():
    assert redact_proxy("http://user:p@ss@gw.example.com:823") == "http://user:***@gw.example.com:823"
    assert redact_proxy("socks5://user:pw@h:1080") == "socks5://user:***@h:1080"
    assert redact_proxy("http://gw.example.com:823") == "http://gw.example.com:823"
    assert redact_proxy("user:pw@h:1") == "user:***@h:1"
    assert redact_proxy(None) is None


@pytest.mark.parametrize("bad", [f"http://user:{SECRET}@gw.example.com", f"http://user:{SECRET}@gw.example.com:notaport"])
async def test_invalid_proxy_logs_no_password(bad, caplog, tmp_path):
    b = CFBypasser(cache_file=str(tmp_path / "c.json"), pool=BrowserPool(1))
    await b.start()
    with caplog.at_level(logging.INFO):
        assert await b.get_or_generate_cookies("https://x.com", proxy=bad) is None
    await b.cleanup()
    assert "pa/ss" not in caplog.text and SECRET not in caplog.text


def test_mirror_route_logs_no_password(caplog, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from cf_bypasser.core.mirror import RequestMirror
    from cf_bypasser.server.app import create_app

    async def fake(self, **kw):
        return 200, {}, b"ok"

    monkeypatch.setattr(RequestMirror, "mirror_request", fake)
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as c, caplog.at_level(logging.INFO):
        c.get("/", headers={"x-hostname": "example.com", "x-proxy": f"http://user:{SECRET}@gw.example.com:823"})
    assert SECRET not in caplog.text
    assert "user:***@gw.example.com:823" in caplog.text
