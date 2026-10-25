import asyncio
import logging
import os
import shutil
import socket
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, AsyncIterator, Callable, List, Optional, Set

import psutil
from DrissionPage import Chromium, ChromiumOptions

from cf_bypasser.utils.constants import (
    BROWSER_COUNT,
    BROWSER_MAX_JOBS,
    BROWSER_NO_SANDBOX,
    BROWSER_PATH,
    HEADLESS,
    LOCALE,
    accept_language,
)

_BRAVE_CANDIDATES = (
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/usr/bin/brave-browser",
    "/usr/bin/brave-browser-stable",
    "/opt/brave.com/brave/brave",
)

_RELAUNCH_BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30)

# auto_port picks a free port then launches; concurrent launches can grab the same one
_launch_lock = threading.Lock()


_headless_ua: Optional[str] = None


def find_brave(explicit: Optional[str] = BROWSER_PATH) -> str:
    if explicit:
        if not os.path.exists(explicit):
            raise FileNotFoundError(f"CF_BROWSER_PATH does not exist: {explicit}")
        return explicit
    for path in _BRAVE_CANDIDATES:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("Brave browser not found; install it or set CF_BROWSER_PATH")


def launch_chromium(headless: bool = HEADLESS, browser_path: Optional[str] = None) -> Chromium:
    global _headless_ua
    browser_path = browser_path or find_brave()
    browser = _launch(headless, browser_path, _headless_ua if headless else None)
    if headless and _headless_ua is None:
        ua = browser._run_cdp("Browser.getVersion")["userAgent"]
        if "HeadlessChrome" in ua:
            # set at launch so it covers every target, incl. the cross-origin Turnstile iframe
            _headless_ua = ua.replace("HeadlessChrome", "Chrome")
            _quit_browser(browser)
            browser = _launch(headless, browser_path, _headless_ua)
    return browser


def browser_args(locale: str) -> List[str]:
    return [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        # STUN over UDP bypasses HTTP/SOCKS proxies and would expose the real IP
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        f"--lang={locale}",
        # plain list: the browser adds the q= weights itself
        f"--accept-lang={accept_language(locale).replace(';q=0.9', '')}",
    ]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _launch(headless: bool, browser_path: str, user_agent: Optional[str]) -> Chromium:
    # our own unique profile dir: every Brave process of this launch carries it in --user-data-dir,
    # so we can always find and kill exactly them (failed launch, unknown pid, hung browser)
    profile = tempfile.mkdtemp(prefix="cfb-brave-")
    co = (ChromiumOptions(read_file=False).set_local_port(_free_port())
          .set_user_data_path(profile).headless(headless).set_browser_path(browser_path))
    if user_agent:
        co.set_user_agent(user_agent)
    for arg in browser_args(LOCALE):
        co.set_argument(arg)
    if BROWSER_NO_SANDBOX or (hasattr(os, "geteuid") and os.geteuid() == 0):
        co.set_argument("--no-sandbox")
    try:
        with _launch_lock:
            browser = Chromium(co)
    except BaseException:
        # DrissionPage leaves the spawned process running when it can't connect to it
        _kill_profile(profile)
        raise
    browser._cfb_profile = profile
    return browser


def _kill_profile(profile: Optional[str]) -> None:
    """Kill every process launched with this exact profile dir, then remove the dir."""
    if not profile:
        return
    marker = f"--user-data-dir={profile}"
    procs = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if marker in (proc.info["cmdline"] or []):
                procs.append(proc)
        except psutil.Error:
            continue
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)
    shutil.rmtree(profile, ignore_errors=True)


def _kill_tree(pid: Optional[int]) -> None:
    if not pid:
        return
    try:
        proc = psutil.Process(pid)
        # the pid may have been reused after the browser exited; only ever kill a browser
        name = proc.name().lower()
        if "brave" not in name and "chrom" not in name:
            return
        procs = proc.children(recursive=True) + [proc]
    except psutil.Error:
        return
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)


def _quit_browser(browser: Any) -> None:
    """Close the browser; never raises and never leaves its process tree behind."""
    pid = getattr(browser, "process_id", None)
    profile = getattr(browser, "_cfb_profile", None)
    try:
        browser.quit(timeout=5, force=True, del_data=True)
    except Exception as e:
        logging.warning(f"Browser quit failed: {e}")
    if profile:
        _kill_profile(profile)
    else:
        _kill_tree(pid)


class BrowserSlot:
    def __init__(self, index: int, browser: Any):
        self.index = index
        self.browser = browser
        self.jobs = 0
        self.poisoned = False
        self.state = "idle"

    def is_alive(self) -> bool:
        try:
            return bool(self.browser.states.is_alive)
        except Exception:
            return False


class BrowserPool:
    """N long-lived browsers; each job leases one exclusively."""

    def __init__(
        self,
        size: int = BROWSER_COUNT,
        launcher: Optional[Callable[[], Any]] = None,
        max_jobs: int = BROWSER_MAX_JOBS,
    ):
        if size < 1:
            raise ValueError("browser pool size must be >= 1")
        self._size = size
        self._launcher = launcher or partial(launch_chromium)
        self._max_jobs = max_jobs
        self._slots: List[BrowserSlot] = []
        self._idle: Optional[asyncio.Queue] = None
        self._bg: Set[asyncio.Task] = set()
        self._stopped = False
        self._stop_event: Optional[asyncio.Event] = None
        # own threads: a leased browser must never wait behind launches/quits for a worker thread
        self.executor = ThreadPoolExecutor(max_workers=size + 4, thread_name_prefix="cfb-browser")

    @property
    def size(self) -> int:
        return self._size

    @property
    def started(self) -> bool:
        return self._idle is not None

    async def run(self, fn: Callable, *args: Any) -> Any:
        """Run blocking DrissionPage work on the pool's own threads."""
        return await asyncio.get_running_loop().run_in_executor(self.executor, fn, *args)

    async def start(self) -> None:
        self._idle = asyncio.Queue()
        self._stop_event = asyncio.Event()
        launched: List[Any] = []
        try:
            for _ in range(self._size):
                launched.append(await self.run(self._launcher))
        except BaseException:
            for browser in launched:
                await self.run(_quit_browser, browser)
            self._idle = None
            raise
        for i, browser in enumerate(launched):
            slot = BrowserSlot(i, browser)
            self._slots.append(slot)
            self._idle.put_nowait(slot)
        logging.info(f"Browser pool started with {self._size} browsers")

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[BrowserSlot]:
        if self._idle is None:
            raise RuntimeError("browser pool not started")
        slot = await self._idle.get()
        slot.state = "busy"
        try:
            yield slot
        finally:
            self._release(slot)

    def _needs_restart(self, slot: BrowserSlot) -> bool:
        return (slot.poisoned or slot.jobs >= self._max_jobs or not slot.is_alive()
                or getattr(slot.browser, "_cfb_dirty", False))

    def _release(self, slot: BrowserSlot) -> None:
        if self._stopped:
            return
        slot.jobs += 1
        if self._needs_restart(slot):
            slot.state = "restarting"
            task = asyncio.get_running_loop().create_task(self._restart(slot))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
            return
        slot.state = "idle"
        self._idle.put_nowait(slot)

    async def _restart(self, slot: BrowserSlot) -> None:
        reason = "poisoned" if slot.poisoned else ("recycled" if slot.jobs >= self._max_jobs else "dead/dirty")
        logging.info(f"Restarting browser {slot.index} ({reason})")
        await self.run(_quit_browser, slot.browser)
        attempt = 0
        while not self._stopped:
            try:
                # not cancelled by stop(): a browser launched here is always either pooled or quit
                browser = await self.run(self._launcher)
            except Exception as e:
                delay = _RELAUNCH_BACKOFF_SECONDS[min(attempt, len(_RELAUNCH_BACKOFF_SECONDS) - 1)]
                logging.error(f"Relaunching browser {slot.index} failed: {e}; retrying in {delay}s")
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop_event.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                continue
            slot.browser = browser
            if self._stopped:
                await self.run(_quit_browser, browser)
                return
            slot.jobs = 0
            slot.poisoned = False
            slot.state = "idle"
            self._idle.put_nowait(slot)
            return

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._stop_event is not None:
            self._stop_event.set()
        # let in-flight relaunches finish so their browser gets quit instead of orphaned
        await asyncio.gather(*list(self._bg), return_exceptions=True)
        await asyncio.gather(*(self.run(_quit_browser, s.browser) for s in self._slots))
        self.executor.shutdown(wait=False)
        logging.info("Browser pool stopped")

    def stats(self) -> dict:
        browsers = []
        for s in self._slots:
            pid = None if s.state == "restarting" else getattr(s.browser, "process_id", None)
            browsers.append({"index": s.index, "state": s.state, "jobs": s.jobs, "pid": pid})
        count = lambda state: sum(1 for b in browsers if b["state"] == state)  # noqa: E731
        return {
            "total": self._size,
            "available": count("idle"),
            "busy": count("busy"),
            "restarting": count("restarting"),
            "browsers": browsers,
        }
