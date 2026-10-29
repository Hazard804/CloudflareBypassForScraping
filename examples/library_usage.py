# run from the repo root: python -m examples.library_usage
import asyncio
import os

from cf_bypasser.browser.pool import BrowserPool
from cf_bypasser.core.bypasser import CFBypasser
from cf_bypasser.core.mirror import RequestMirror

URL = "https://cloudflare.hackmap.win/"
PROXY = os.environ.get("CF_PROXY")  # optional, e.g. http://user:pass@host:port


async def main():
    # 2 reused Brave browsers; caching is on by default (pass cache_enabled=False to disable)
    bypasser = CFBypasser(pool=BrowserPool(2))
    await bypasser.start()
    mirror = RequestMirror(bypasser)
    try:
        # 1) clearance cookies + the user-agent they are bound to
        data = await bypasser.get_or_generate_cookies(URL, proxy=PROXY)
        print("cookies:", data["cookies"])
        print("user-agent:", data["user_agent"])

        # 2) rendered HTML after the challenge
        page = await bypasser.get_or_generate_html(URL, proxy=PROXY)
        print("html:", page["status_code"], len(page["html"]), "chars")

        # 3) raw bytes of any resource (PDF, image, JSON...)
        raw = await bypasser.fetch_bytes(URL, proxy=PROXY)
        print("fetch:", raw["status_code"], raw["content_type"], len(raw["body"]), "bytes")

        # 4) replay a request with the clearance cookies (curl_cffi, Chrome TLS fingerprint)
        status, headers, body = await mirror.mirror_request(
            method="GET", path="/", headers={}, hostname="cloudflare.hackmap.win", proxy=PROXY,
        )
        print("mirror:", status, headers.get("content-type"), len(body), "bytes")

        print("pool:", bypasser.pool.stats())
    finally:
        await mirror.cleanup()
        await bypasser.cleanup()  # quits the browsers


asyncio.run(main())
