import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, AsyncIterator, Callable, List, Optional, Set

import psutil
from DrissionPage import Chromium, ChromiumOptions

from cf_bypasser.utils.constants import (
    BROWSER_COUNT,
    BROWSER_MAX_JOBS,
    CHROME_NO_SANDBOX,
    CHROME_PATH,
    HEADLESS,
)

_RELAUNCH_BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30)

# auto_port picks a free port then launches; concurrent launches can grab the same one
_launch_lock = threading.Lock()


def launch_chromium(headless: bool = HEADLESS, chrome_path: Optional[str] = CHROME_PATH) -> Chromium:
    co = ChromiumOptions(read_file=False).auto_port().headless(headless)
    if chrome_path:
        co.set_browser_path(chrome_path)
    for arg in (
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--lang=en-US",
        "--accept-lang=en-US",
    ):
        co.set_argument(arg)
    if CHROME_NO_SANDBOX or (hasattr(os, "geteuid") and os.geteuid() == 0):
        co.set_argument("--no-sandbox")
    with _launch_lock:
        return Chromium(co)


def _kill_tree(pid: Optional[int]) -> None:
    if not pid:
        return
    try:
        proc = psutil.Process(pid)
        # the pid may have been reused after Chrome exited; only ever kill a browser
        if "chrom" not in proc.name().lower():
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
    try:
        browser.quit(timeout=5, force=True, del_data=True)
    except Exception as e:
        logging.warning(f"Browser quit failed: {e}")
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

    @property
    def size(self) -> int:
        return self._size

    @property
    def started(self) -> bool:
        return self._idle is not None

    async def start(self) -> None:
        self._idle = asyncio.Queue()
        launched: List[Any] = []
        try:
            for _ in range(self._size):
                launched.append(await asyncio.to_thread(self._launcher))
        except BaseException:
            for browser in launched:
                await asyncio.to_thread(_quit_browser, browser)
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

    def _release(self, slot: BrowserSlot) -> None:
        if self._stopped:
            return
        slot.jobs += 1
        if slot.poisoned or slot.jobs >= self._max_jobs or not slot.is_alive():
            slot.state = "restarting"
            task = asyncio.get_running_loop().create_task(self._restart(slot))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
            return
        slot.state = "idle"
        self._idle.put_nowait(slot)

    async def _restart(self, slot: BrowserSlot) -> None:
        reason = "poisoned" if slot.poisoned else ("recycled" if slot.jobs >= self._max_jobs else "dead")
        logging.info(f"Restarting browser {slot.index} ({reason})")
        await asyncio.to_thread(_quit_browser, slot.browser)
        attempt = 0
        while not self._stopped:
            try:
                slot.browser = await asyncio.to_thread(self._launcher)
                break
            except Exception as e:
                delay = _RELAUNCH_BACKOFF_SECONDS[min(attempt, len(_RELAUNCH_BACKOFF_SECONDS) - 1)]
                logging.error(f"Relaunching browser {slot.index} failed: {e}; retrying in {delay}s")
                attempt += 1
                await asyncio.sleep(delay)
        if self._stopped:
            return
        slot.jobs = 0
        slot.poisoned = False
        slot.state = "idle"
        self._idle.put_nowait(slot)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for task in list(self._bg):
            task.cancel()
        await asyncio.gather(*self._bg, return_exceptions=True)
        await asyncio.gather(*(asyncio.to_thread(_quit_browser, s.browser) for s in self._slots))
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
