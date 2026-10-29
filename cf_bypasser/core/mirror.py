import asyncio
import logging
import os
import re
import traceback
from collections import OrderedDict
from typing import Dict, Any, Optional, Tuple
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

from cf_bypasser.core.bypasser import CFBypasser
from cf_bypasser.utils.config import BrowserConfig
from cf_bypasser.utils.misc import cache_key
from cf_bypasser.utils.constants import (
    CF_COOKIE_PREFIXES,
    CHROME_HEADERS,
    MAX_SESSIONS,
    MIRROR_MAX_RETRIES,
    MIRROR_RETRY_BACKOFF_SECONDS,
    SESSION_TIMEOUT_SECONDS,
)

# Transport-level headers belong to the client library, not to the caller: forwarding
# "Connection: keep-alive" or a python-requests "Accept-Encoding: gzip, deflate"
# alongside a Chrome user-agent is a fingerprint mismatch Cloudflare flags.
HOP_BY_HOP_HEADERS = frozenset({
    "connection", "proxy-connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
})

# Headers a real Chrome derives from its own navigation; always rebuilt for the target UA.
CLIENT_HINT_HEADERS = frozenset({
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest", "sec-fetch-mode",
    "sec-fetch-site", "sec-fetch-user",
})

_CHROME_VERSION_RE = re.compile(r"Chrome/(\d+)")


def _platform_hint(user_agent: str) -> Tuple[str, str]:
    """sec-ch-ua-platform / sec-ch-ua-mobile derived from the user agent."""
    ua = user_agent.lower()
    if "android" in ua:
        return '"Android"', "?1"
    if "iphone" in ua or "ipad" in ua:
        return '"iOS"', "?1"
    if "macintosh" in ua or "mac os x" in ua:
        return '"macOS"', "?0"
    if "linux" in ua:
        return '"Linux"', "?0"
    return '"Windows"', "?0"


def client_hint_headers(user_agent: str, document: bool) -> Dict[str, str]:
    """Chromium client hints + fetch metadata matching the UA the bypass minted."""
    match = _CHROME_VERSION_RE.search(user_agent or "")
    major = match.group(1) if match else "124"
    platform, mobile = _platform_hint(user_agent or "")

    headers = {
        "sec-ch-ua": f'"Chromium";v="{major}", "Not:A-Brand";v="24", "Google Chrome";v="{major}"',
        "sec-ch-ua-mobile": mobile,
        "sec-ch-ua-platform": platform,
    }
    if document:
        headers.update({
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        })
    else:
        headers.update({
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        })
    return headers


class RequestMirror:
    """Handles dynamic request mirroring with Cloudflare bypass."""

    def __init__(self, bypasser: Optional[CFBypasser] = None):
        self.bypasser: CFBypasser = bypasser or CFBypasser()
        self.session_cache: "OrderedDict[str, AsyncSession]" = OrderedDict()  # LRU per host:proxy
        self.max_sessions: int = int(os.environ.get("CF_MAX_SESSIONS", str(MAX_SESSIONS)))

    def strip_mirror_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Remove the bypasser's own control headers (incl. x-auth-token) before forwarding."""
        cleaned_headers = {}
        for key, value in headers.items():
            key_lower = key.lower()
            if key_lower not in ['x-hostname', 'x-proxy', 'x-bypass-cache', 'x-auth-token']:
                cleaned_headers[key] = value
        return cleaned_headers

    def merge_cookies(self, incoming_cookies: str, cf_cookies: Dict[str, str]) -> str:
        """Merge incoming cookies with Cloudflare clearance cookies."""
        try:
            incoming_dict = {}
            if incoming_cookies:
                for cookie in incoming_cookies.split(';'):
                    cookie = cookie.strip()
                    if '=' in cookie:
                        name, value = cookie.split('=', 1)
                        incoming_dict[name.strip()] = value.strip()

            # Only Cloudflare's own cookies are authoritative. The bypass browser also
            # carries the site's anonymous session (e.g. __Host-..._session); letting those
            # overwrite the caller's cookies would silently drop the caller's login.
            merged_cookies = dict(incoming_dict)
            for name, value in cf_cookies.items():
                if str(name).lower().startswith(CF_COOKIE_PREFIXES):
                    merged_cookies[name] = value

            cookie_pairs = [f"{name}={value}" for name, value in merged_cookies.items()]
            return '; '.join(cookie_pairs)
        except Exception as e:
            logging.error(f"Error merging cookies: {e}")
            # Fallback to CF cookies only
            return '; '.join([f"{name}={value}" for name, value in cf_cookies.items()])

    def build_target_url(self, hostname: str, path: str, query_string: Optional[str] = None) -> str:
        if not hostname.startswith(('http://', 'https://')):
            hostname = f"https://{hostname}"

        parsed = urlparse(hostname)
        base = f"{parsed.scheme}://{parsed.netloc}"

        # Collapse network-path / leading slashes so the client path can't swap the host.
        safe_path = "/" + (path or "").lstrip("/")

        url = base + safe_path
        if query_string:
            url += f"?{query_string}"

        return url

    def _new_session(self, proxy: Optional[str]) -> AsyncSession:
        return AsyncSession(
            impersonate="chrome",  # match the pooled Chrome's fingerprint
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=SESSION_TIMEOUT_SECONDS,
        )

    async def get_session(self, hostname: str, proxy: Optional[str] = None) -> AsyncSession:
        session_key = f"{hostname}:{proxy or 'no-proxy'}"

        if session_key in self.session_cache:
            self.session_cache.move_to_end(session_key)
            return self.session_cache[session_key]

        session = self._new_session(proxy)
        self.session_cache[session_key] = session
        self.session_cache.move_to_end(session_key)

        while len(self.session_cache) > self.max_sessions:
            _, evicted = self.session_cache.popitem(last=False)
            try:
                await evicted.close()
            except Exception as e:
                logging.error(f"Error closing evicted session: {e}")

        return session

    def _prepare_request_headers(self, headers: Dict[str, str], cf_data: Dict[str, Any]) -> Dict[str, str]:
        """Rebuild the request so it looks like the browser that minted the clearance.

        Everything the caller sent is mirrored, except the headers that give away a
        non-browser HTTP client: hop-by-hop transport headers, a library-default
        `Accept: */*` / `Accept-Encoding: gzip, deflate`, and the Chromium client hints,
        which are regenerated from the bypassed user agent.
        """
        clean_headers = self.strip_mirror_headers(headers)

        # Override User-Agent with the one used for CF bypass
        user_agent = cf_data['user_agent']
        clean_headers['user-agent'] = user_agent
        clean_headers.pop("host", None)

        # rebuilt below (or owned by curl_cffi) -- never forward the caller's copy
        rebuilt = HOP_BY_HOP_HEADERS | CLIENT_HINT_HEADERS | {"accept-encoding", "host"}
        for key in [k for k in clean_headers if k.lower() in rebuilt]:
            del clean_headers[key]

        incoming_cookies = ''
        cookie_header_key = None
        for key in clean_headers:
            if key.lower() == 'cookie':
                incoming_cookies = clean_headers[key]
                cookie_header_key = key
                break

        merged_cookies = self.merge_cookies(incoming_cookies, cf_data['cookies'])

        if cookie_header_key:
            del clean_headers[cookie_header_key]
        clean_headers['Cookie'] = merged_cookies

        # a library default ("*/*") is a bot tell; a real Accept is the caller's intent, so keep it
        accept_key = next((k for k in clean_headers if k.lower() == 'accept'), None)
        accept = (clean_headers.get(accept_key) or '').strip()
        if not accept or accept == '*/*':
            if accept_key:
                del clean_headers[accept_key]
            accept = CHROME_HEADERS['Accept']
            clean_headers['Accept'] = accept

        document = 'text/html' in accept.lower()
        clean_headers['Accept-Encoding'] = CHROME_HEADERS['Accept-Encoding']

        # client hints must agree with the UA, otherwise Cloudflare sees Chrome 156 claiming
        # headers from a curl/requests client
        for key, value in client_hint_headers(user_agent, document).items():
            clean_headers[key] = value

        # remaining Chrome defaults for better impersonation (never hop-by-hop ones)
        present = {k.lower() for k in clean_headers}
        for key, value in BrowserConfig.get_chrome_headers().items():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            if key.lower() not in present:
                clean_headers[key] = value

        return clean_headers

    def _rewrite_response_headers(self, response: Any) -> Dict[str, str]:
        """Neutralize Content-Encoding and fix Content-Length to match the decoded body."""
        final_headers = {}
        for k, v in dict(response.headers).items():
            k_lower = k.lower()
            if k_lower == "content-encoding":
                final_headers[k] = "identity"
            elif k_lower == "content-length":
                final_headers[k] = str(len(response.content))
            else:
                final_headers[k] = v
        return final_headers

    async def mirror_request(
        self,
        method: str,
        path: str,
        query_string: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        hostname: Optional[str] = None,
        proxy: Optional[str] = None,
        bypass_cache: bool = False,
        body: Optional[bytes] = None,
        max_retries: int = MIRROR_MAX_RETRIES
    ) -> Tuple[int, Dict[str, str], bytes]:
        """Mirror the request to the target hostname (parsed by the caller) with CF bypass."""

        if not hostname:
            raise ValueError("x-hostname header is required")

        for attempt in range(max_retries + 1):
            try:
                logging.info(f"Mirroring {method} request to {hostname}{path} (attempt {attempt + 1}/{max_retries + 1})")

                target_url = self.build_target_url(hostname, path, query_string)

                # If bypass_cache is True, invalidate existing cache first
                if bypass_cache:
                    parsed_hostname = urlparse(target_url).netloc
                    self.bypasser.cookie_cache.invalidate(cache_key(parsed_hostname, proxy))

                cf_data = await self.bypasser.get_or_generate_cookies(target_url, proxy)

                if not cf_data:
                    raise Exception("Failed to get Cloudflare clearance cookies")

                clean_headers = self._prepare_request_headers(headers, cf_data)

                reuse = getattr(self.bypasser, "cache_enabled", True)
                # without caching, a fresh session per request: no target cookies carried between clients
                session = await self.get_session(hostname, proxy) if reuse else self._new_session(proxy)

                try:
                    response = await session.request(
                        method=method,
                        url=target_url,
                        headers=clean_headers,
                        data=body,
                        allow_redirects=False  # Let the client handle redirects
                    )
                finally:
                    if not reuse:
                        await session.close()

                status_code = response.status_code

                if status_code == 403 and attempt < max_retries:
                    logging.warning(f"Got 403 Forbidden from {hostname}, invalidating cache and retrying...")

                    parsed_hostname = urlparse(target_url).netloc
                    self.bypasser.cookie_cache.invalidate(cache_key(parsed_hostname, proxy))

                    await asyncio.sleep(MIRROR_RETRY_BACKOFF_SECONDS)
                    continue

                final_headers = self._rewrite_response_headers(response)

                logging.info(f"Request to {hostname} completed with status {status_code}")
                return status_code, final_headers, response.content

            except (KeyError, TypeError, ValueError):
                # Deterministic programming errors — retrying won't help.
                raise
            except Exception as e:
                if attempt < max_retries:
                    logging.warning(f"Request attempt {attempt + 1} failed: {e}, retrying...")
                    await asyncio.sleep(MIRROR_RETRY_BACKOFF_SECONDS)
                    continue
                else:
                    logging.error(f"Error mirroring request after {max_retries + 1} attempts: {e}")
                    logging.error(traceback.format_exc())
                    raise

    async def cleanup(self):
        """Close mirror HTTP sessions. The bypasser's browser pool is shut down by its owner, not here."""
        for session in self.session_cache.values():
            try:
                await session.close()
            except Exception as e:
                logging.error(f"Error closing session: {e}")
        self.session_cache.clear()
