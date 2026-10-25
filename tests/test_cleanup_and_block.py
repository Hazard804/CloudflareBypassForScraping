import asyncio

import pytest

from cf_bypasser.browser.context import BrowserContextSession


class CdpBrowser:
    """Records CDP calls; contexts listed by getBrowserContexts can be made sticky to simulate a failed dispose."""

    def __init__(self, sticky=False, fail_clear=False):
        self.calls = []
        self.contexts = []
        self.sticky = sticky
        self.fail_clear = fail_clear
        self.tab_ids = []

    def _run_cdp(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd == "Target.createBrowserContext":
            self.contexts.append("CTX")
            return {"browserContextId": "CTX"}
        if cmd == "Target.createTarget":
            self.tab_ids.append("T1")
            return {"targetId": "T1"}
        if cmd == "Storage.clearCookies" and self.fail_clear:
            raise RuntimeError("cdp gone")
        if cmd == "Target.disposeBrowserContext" and not self.sticky:
            self.contexts.remove(kw["browserContextId"])
        if cmd == "Target.getBrowserContexts":
            return {"browserContextIds": list(self.contexts)}
        return {}

    def get_tab(self, target_id):
        return object()


def test_cookies_cleared_before_context_disposed():
    b = CdpBrowser()
    with BrowserContextSession(b):
        pass
    cmds = [c for c, _ in b.calls]
    assert cmds.index("Storage.clearCookies") < cmds.index("Target.disposeBrowserContext")
    assert dict(b.calls)["Storage.clearCookies"] == {"browserContextId": "CTX"}
    assert not getattr(b, "_cfb_dirty", False)


def test_leftover_context_marks_browser_dirty():
    b = CdpBrowser(sticky=True)  # dispose "succeeds" but the context is still there
    with BrowserContextSession(b):
        pass
    assert b._cfb_dirty is True


def test_failed_cookie_clear_marks_browser_dirty():
    b = CdpBrowser(fail_clear=True)
    with BrowserContextSession(b):
        pass
    assert b._cfb_dirty is True


def test_brave_hosts_blocked_by_resolver_rules():
    from cf_bypasser.browser.pool import browser_args
    rules = [a for a in browser_args("en-US", block_brave=True) if a.startswith("--host-resolver-rules=")]
    assert len(rules) == 1
    for host in ("brave.com", "*.brave.com", "*.bravesoftware.com", "*.brave.software"):
        assert f"MAP {host} ~NOTFOUND" in rules[0]
    assert not any(a.startswith("--host-resolver-rules=") for a in browser_args("en-US", block_brave=False))


@pytest.mark.parametrize("host,blocked", [
    ("p3a.brave.com", True), ("brave.com", True), ("laptop-updates.brave.com", True),
    ("go-updater.bravesoftware.com", True), ("x.brave.software", True),
    ("notbrave.com", False), ("brave.com.example.org", False), ("example.com", False),
])
def test_relay_blocklist_matching(host, blocked):
    from cf_bypasser.browser.relay import is_blocked_host
    assert is_blocked_host(host, ("brave.com", "bravesoftware.com", "brave.software")) is blocked


@pytest.mark.asyncio(loop_scope="function")
async def test_relay_refuses_blocked_host(monkeypatch):
    import cf_bypasser.browser.relay as relay_mod
    monkeypatch.setattr(relay_mod, "BLOCKED_DOMAINS", ("brave.com",))
    opened = []

    async def never(*a, **k):
        opened.append(a)
        raise AssertionError("must not open an upstream tunnel for a blocked host")

    monkeypatch.setattr(relay_mod, "open_upstream", never)
    relay = relay_mod.ProxyRelay("http://u:p@127.0.0.1:1")
    await relay.start()
    try:
        port = int(relay.url.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT p3a.brave.com:443 HTTP/1.1\r\nHost: p3a.brave.com:443\r\n\r\n")
        await writer.drain()
        status = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        assert status.startswith(b"HTTP/1.1 403") and opened == []
    finally:
        await relay.stop()
