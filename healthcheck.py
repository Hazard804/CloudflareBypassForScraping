import os
import sys
import urllib.request

req = urllib.request.Request(f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/browsers")
if os.environ.get("CF_AUTH_TOKEN"):
    req.add_header("x-auth-token", os.environ["CF_AUTH_TOKEN"])
try:
    sys.exit(0 if b'"total"' in urllib.request.urlopen(req, timeout=4).read() else 1)
except Exception:
    sys.exit(1)
