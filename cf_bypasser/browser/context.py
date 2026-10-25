import logging
import time
from typing import Any, Dict, Optional

_TAB_ATTACH_TIMEOUT_SECONDS = 5


class BrowserContextSession:
    """One disposable CDP browser context (own cookies, own proxy) with a single tab."""

    def __init__(self, browser: Any, proxy_server: Optional[str] = None):
        self.browser = browser
        self.proxy_server = proxy_server
        self.context_id: Optional[str] = None
        self.tab = None

    def __enter__(self) -> "BrowserContextSession":
        try:
            self._open()
        except BaseException:
            self._dispose()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._dispose()

    def _open(self) -> None:
        params: Dict[str, Any] = {"disposeOnDetach": True}
        if self.proxy_server:
            params["proxyServer"] = self.proxy_server
        self.context_id = self.browser._run_cdp("Target.createBrowserContext", **params)["browserContextId"]
        target_id = self.browser._run_cdp(
            "Target.createTarget", url="about:blank", browserContextId=self.context_id
        )["targetId"]
        deadline = time.monotonic() + _TAB_ATTACH_TIMEOUT_SECONDS
        while target_id not in self.browser.tab_ids:
            if time.monotonic() > deadline:
                raise RuntimeError(f"tab {target_id} never attached")
            time.sleep(0.02)
        self.tab = self.browser.get_tab(target_id)

    def _dispose(self) -> None:
        if self.context_id is None:
            return
        try:
            self.browser._run_cdp("Target.disposeBrowserContext", browserContextId=self.context_id)
        except Exception as e:
            logging.warning(f"Disposing browser context failed: {e}")
        self.context_id = None
        self.tab = None

    def set_cookies(self, cookies: Dict[str, str], url: str) -> None:
        params = [{"name": k, "value": v, "url": url} for k, v in cookies.items()]
        if params:
            self.browser._run_cdp("Storage.setCookies", cookies=params, browserContextId=self.context_id)

    def get_cookies(self) -> Dict[str, str]:
        cookies = self.browser._run_cdp("Storage.getCookies", browserContextId=self.context_id)["cookies"]
        return {c["name"]: c["value"] for c in cookies}

    def user_agent(self) -> str:
        return self.tab.run_js("return navigator.userAgent")
