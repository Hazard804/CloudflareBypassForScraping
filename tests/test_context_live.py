import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

import pytest

from cf_bypasser.browser.context import BrowserContextSession
from cf_bypasser.browser.pool import _quit_browser, launch_chromium

pytestmark = pytest.mark.live


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        head, rest = data.split(b"\r\n\r\n", 1)
        line = head.split(b"\r\n", 1)[0].decode()
        method, target, _ = line.split(" ", 2)
        if method == "CONNECT":
            host, port = target.rsplit(":", 1)
        else:
            u = urlsplit(target)
            host, port = u.hostname, u.port or 80
        self.server.hits.append(host)
        up = socket.create_connection((host, int(port)), timeout=10)
        if method == "CONNECT":
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            up.sendall(data)
        socks = [self.request, up]
        while True:
            r, _, _ = select.select(socks, [], [], 10)
            if not r:
                break
            for s in r:
                buf = s.recv(65536)
                if not buf:
                    up.close()
                    return
                (up if s is self.request else self.request).sendall(buf)


@pytest.fixture
def make_proxy():
    servers = []

    def make():
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _ProxyHandler)
        srv.daemon_threads = True
        srv.hits = []
        srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.shutdown()


@pytest.fixture(scope="module")
def browser():
    b = launch_chromium(headless=True)
    yield b
    _quit_browser(b)


def test_contexts_isolated_cookies(browser):
    with BrowserContextSession(browser) as a:
        a.set_cookies({"probe": "1"}, "https://example.com")
        assert a.get_cookies()["probe"] == "1"
    with BrowserContextSession(browser) as b:
        assert "probe" not in b.get_cookies()


def test_proxy_changes_without_restart(browser, make_proxy):
    pa, pb = make_proxy(), make_proxy()
    pid = browser.process_id
    with BrowserContextSession(browser, pa.url) as s:
        s.tab.get("http://example.com")
    with BrowserContextSession(browser, pb.url) as s:
        s.tab.get("http://example.com")
    assert "example.com" in pa.hits and "example.com" in pb.hits
    assert browser.process_id == pid


def test_no_proxy_context_bypasses_previous_proxy(browser, make_proxy):
    p = make_proxy()
    with BrowserContextSession(browser, p.url) as s:
        s.tab.get("http://example.com")
    hits = len(p.hits)
    with BrowserContextSession(browser) as s:
        s.tab.get("http://example.com")
    assert len(p.hits) == hits


def test_dispose_leaves_no_contexts(browser):
    before = browser._run_cdp("Target.getBrowserContexts")["browserContextIds"]
    with BrowserContextSession(browser):
        pass
    assert browser._run_cdp("Target.getBrowserContexts")["browserContextIds"] == before


def test_headless_ua_has_no_headless_token(browser):
    with BrowserContextSession(browser) as s:
        assert "Headless" not in s.user_agent()
