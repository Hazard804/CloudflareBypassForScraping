# run from the repo root: python -m examples.download_pdf
import asyncio

from cf_bypasser.browser.pool import BrowserPool
from cf_bypasser.core.bypasser import CFBypasser

# open-access paper behind Cloudflare plus the publisher's own JS redirect to a signed PDF URL
URL = "https://www.sciencedirect.com/science/article/pii/S2376060524001354/pdfft?md5=0c6a44e73fcff00426c996364b4ecf43&pid=1-s2.0-S2376060524001354-main.pdf"


async def main():
    bypasser = CFBypasser(pool=BrowserPool(1))
    await bypasser.start()
    try:
        result = await bypasser.fetch_bytes(URL)
    finally:
        await bypasser.cleanup()

    if not result or not result["body"].startswith(b"%PDF"):
        raise SystemExit(f"no PDF: {result and result['content_type']}")
    with open("paper.pdf", "wb") as f:
        f.write(result["body"])
    print(f"saved paper.pdf ({len(result['body'])} bytes)")


asyncio.run(main())
