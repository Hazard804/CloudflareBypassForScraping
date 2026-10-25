import asyncio
import threading

import pytest

from cf_bypasser.browser.pool import BrowserPool

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _States:
    def __init__(self):
        self.is_alive = True


class FakeBrowser:
    def __init__(self):
        self.states = _States()
        self.process_id = None
        self.quit_calls = 0

    def quit(self, *a, **k):
        self.quit_calls += 1
        self.states.is_alive = False


class Launcher:
    def __init__(self, fail_on=None, gate: threading.Event = None):
        self.count = 0
        self.browsers = []
        self.fail_on = fail_on
        self.gate = gate

    def __call__(self):
        self.count += 1
        if self.fail_on == self.count:
            raise RuntimeError("launch failed")
        if self.gate is not None and self.count > 1:
            self.gate.wait(5)
        b = FakeBrowser()
        self.browsers.append(b)
        return b


async def _settle(pool):
    await asyncio.gather(*list(pool._bg), return_exceptions=True)


async def test_start_launches_size_browsers():
    launcher = Launcher()
    pool = BrowserPool(3, launcher=launcher)
    await pool.start()
    assert launcher.count == 3
    assert pool.size == 3
    await pool.stop()


async def test_lease_blocks_when_exhausted():
    pool = BrowserPool(1, launcher=Launcher())
    await pool.start()
    async with pool.lease():
        with pytest.raises(asyncio.TimeoutError):
            async with asyncio.timeout(0.1):
                async with pool.lease():
                    pass
    async with asyncio.timeout(1):
        async with pool.lease():
            pass
    await pool.stop()


async def test_dead_browser_relaunched_on_release():
    launcher = Launcher()
    pool = BrowserPool(1, launcher=launcher)
    await pool.start()
    async with pool.lease() as slot:
        old = slot.browser
        old.states.is_alive = False
    await _settle(pool)
    assert launcher.count == 2
    assert old.quit_calls == 1
    async with pool.lease() as slot:
        assert slot.browser is not old
    await pool.stop()


async def test_timeout_poisons_and_relaunches():
    launcher = Launcher()
    pool = BrowserPool(1, launcher=launcher)
    await pool.start()
    async with pool.lease() as slot:
        slot.poisoned = True
    await _settle(pool)
    assert launcher.count == 2
    async with pool.lease() as slot:
        assert slot.poisoned is False
    await pool.stop()


async def test_recycle_after_max_jobs():
    launcher = Launcher()
    pool = BrowserPool(1, launcher=launcher, max_jobs=2)
    await pool.start()
    for _ in range(2):
        async with pool.lease():
            pass
    await _settle(pool)
    assert launcher.count == 2
    await pool.stop()


async def test_stop_quits_all_even_when_leased():
    launcher = Launcher()
    pool = BrowserPool(2, launcher=launcher)
    await pool.start()
    async with pool.lease():
        await pool.stop()
    assert all(b.quit_calls >= 1 for b in launcher.browsers)


async def test_start_failure_quits_partial():
    launcher = Launcher(fail_on=3)
    pool = BrowserPool(3, launcher=launcher)
    with pytest.raises(RuntimeError):
        await pool.start()
    assert len(launcher.browsers) == 2
    assert all(b.quit_calls == 1 for b in launcher.browsers)


async def test_stats_tracks_states():
    pool = BrowserPool(3, launcher=Launcher())
    await pool.start()
    async with pool.lease():
        s = pool.stats()
        assert (s["total"], s["available"], s["busy"], s["restarting"]) == (3, 2, 1, 0)
        assert len(s["browsers"]) == 3
    assert pool.stats()["available"] == 3
    await pool.stop()


async def test_stats_shows_restarting():
    gate = threading.Event()
    pool = BrowserPool(1, launcher=Launcher(gate=gate))
    await pool.start()
    async with pool.lease() as slot:
        slot.poisoned = True
    await asyncio.sleep(0.05)
    s = pool.stats()
    assert s["restarting"] == 1 and s["available"] == 0
    assert s["browsers"][0]["pid"] is None and s["browsers"][0]["state"] == "restarting"
    gate.set()
    await _settle(pool)
    assert pool.stats()["available"] == 1
    await pool.stop()


async def test_relaunch_failure_retries_until_healthy(monkeypatch):
    import cf_bypasser.browser.pool as pool_mod
    monkeypatch.setattr(pool_mod, "_RELAUNCH_BACKOFF_SECONDS", (0.01, 0.01))
    launcher = Launcher(fail_on=2)
    pool = BrowserPool(1, launcher=launcher)
    await pool.start()
    async with pool.lease() as slot:
        slot.poisoned = True
    await _settle(pool)
    assert launcher.count == 3
    assert pool.stats()["available"] == 1
    await pool.stop()


def test_kill_tree_ignores_non_browser_pid():
    import os
    from cf_bypasser.browser.pool import _kill_tree
    _kill_tree(os.getpid())  # pytest's own process: must survive
    assert True


def test_find_brave_explicit_path(tmp_path):
    from cf_bypasser.browser.pool import find_brave
    exe = tmp_path / "brave"
    exe.write_text("")
    assert find_brave(str(exe)) == str(exe)


def test_find_brave_missing_explicit_path_fails_loudly(tmp_path):
    from cf_bypasser.browser.pool import find_brave
    with pytest.raises(FileNotFoundError):
        find_brave(str(tmp_path / "nope"))


def test_find_brave_autodetects(monkeypatch, tmp_path):
    import cf_bypasser.browser.pool as pool_mod
    exe = tmp_path / "brave-browser"
    exe.write_text("")
    monkeypatch.setattr(pool_mod, "_BRAVE_CANDIDATES", (str(tmp_path / "missing"), str(exe)))
    assert pool_mod.find_brave(None) == str(exe)


def test_find_brave_none_installed(monkeypatch, tmp_path):
    import cf_bypasser.browser.pool as pool_mod
    monkeypatch.setattr(pool_mod, "_BRAVE_CANDIDATES", (str(tmp_path / "missing"),))
    with pytest.raises(FileNotFoundError, match="Brave"):
        pool_mod.find_brave(None)


def test_non_live_tests_cannot_launch_real_browsers():
    import cf_bypasser.browser.pool as pool_mod
    assert pool_mod.launch_chromium.__name__ == "_FakeBrowser"


def test_browser_args_use_locale():
    from cf_bypasser.browser.pool import browser_args
    args = browser_args("de-DE")
    assert "--lang=de-DE" in args
    assert "--accept-lang=de-DE,de" in args


def test_env_settings(monkeypatch):
    import importlib
    import cf_bypasser.utils.constants as c
    monkeypatch.setenv("CF_LOCALE", "tr-TR")
    monkeypatch.setenv("CF_MIRROR_RETRY_DELAY", "2.5")
    monkeypatch.setenv("CF_MIRROR_MAX_RETRIES", "4")
    try:
        importlib.reload(c)
        assert c.LOCALE == "tr-TR"
        assert c.CHROME_HEADERS["Accept-Language"] == "tr-TR,tr;q=0.9"
        assert c.MIRROR_RETRY_BACKOFF_SECONDS == 2.5
        assert c.MIRROR_MAX_RETRIES == 4
    finally:
        monkeypatch.undo()
        importlib.reload(c)


def test_pool_has_own_executor_sized_for_browsers():
    pool = BrowserPool(8, launcher=Launcher())
    assert pool.executor._max_workers >= 8 + 2


async def test_dirty_browser_restarted():
    launcher = Launcher()
    pool = BrowserPool(1, launcher=launcher)
    await pool.start()
    async with pool.lease() as slot:
        slot.browser._cfb_dirty = True  # e.g. a context could not be disposed
    await _settle(pool)
    assert launcher.count == 2
    await pool.stop()


async def test_stop_quits_browser_from_inflight_relaunch():
    gate = threading.Event()
    launcher = Launcher(gate=gate)
    pool = BrowserPool(1, launcher=launcher)
    await pool.start()
    async with pool.lease() as slot:
        slot.poisoned = True
    await asyncio.sleep(0.05)  # relaunch is now blocked inside the launcher thread
    stopper = asyncio.create_task(pool.stop())
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(stopper, 5)
    assert len(launcher.browsers) == 2
    assert launcher.browsers[1].quit_calls >= 1  # the browser that finished launching after stop()


async def test_stop_interrupts_relaunch_backoff(monkeypatch):
    import cf_bypasser.browser.pool as pool_mod
    monkeypatch.setattr(pool_mod, "_RELAUNCH_BACKOFF_SECONDS", (30,))
    launcher = Launcher(fail_on=2)
    pool = BrowserPool(1, launcher=launcher)
    await pool.start()
    async with pool.lease() as slot:
        slot.poisoned = True
    await asyncio.sleep(0.1)
    await asyncio.wait_for(pool.stop(), 3)  # must not wait out the 30s backoff


def test_kill_profile_kills_only_matching_processes(tmp_path):
    import subprocess
    import sys
    from cf_bypasser.browser.pool import _kill_profile
    profile = str(tmp_path / "cfb-brave-x")
    mine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", f"--user-data-dir={profile}"])
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", f"--user-data-dir={profile}-other"])
    try:
        _kill_profile(profile)
        assert mine.wait(5) is not None
        assert other.poll() is None
    finally:
        for p in (mine, other):
            p.kill()


def test_failed_launch_kills_what_it_spawned(monkeypatch):
    import subprocess
    import sys
    import os
    import cf_bypasser.browser.pool as pool_mod
    spawned = {}

    def fake_chromium(co):
        spawned["proc"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                            f"--user-data-dir={co.user_data_path}"])
        spawned["dir"] = co.user_data_path
        raise RuntimeError("BrowserConnectError: could not connect")

    monkeypatch.setattr(pool_mod, "Chromium", fake_chromium)
    with pytest.raises(RuntimeError):
        pool_mod._launch(True, "/bin/true", None)
    assert spawned["proc"].wait(5) is not None
    assert not os.path.exists(spawned["dir"])


def test_browser_args_block_webrtc_ip_leak():
    from cf_bypasser.browser.pool import browser_args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in browser_args("en-US")
