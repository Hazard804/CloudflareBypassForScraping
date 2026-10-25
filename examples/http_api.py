# needs a running server: python server.py
import os

from curl_cffi import requests

SERVER = os.environ.get("CF_SERVER", "http://localhost:8000")
URL = "https://cloudflare.hackmap.win/"
PROXY = os.environ.get("CF_PROXY")  # optional, e.g. http://user:pass@host:port

# only needed when the server runs with --auth-token / CF_AUTH_TOKEN
HEADERS = {"x-auth-token": os.environ["CF_AUTH_TOKEN"]} if os.environ.get("CF_AUTH_TOKEN") else {}
params = {"proxy": PROXY} if PROXY else {}

r = requests.get(f"{SERVER}/browsers", headers=HEADERS)
print("browsers:", r.json()["available"], "of", r.json()["total"], "available")

r = requests.get(f"{SERVER}/cookies", params={"url": URL, **params}, headers=HEADERS)
print("cookies:", r.json())

r = requests.get(f"{SERVER}/html", params={"url": URL, **params}, headers=HEADERS)
print("html:", r.status_code, len(r.text), "chars, final url", r.headers.get("x-cf-bypasser-final-url"))

r = requests.get(f"{SERVER}/fetch", params={"url": URL, **params}, headers=HEADERS)
print("fetch:", r.status_code, r.headers.get("content-type"), len(r.content), "bytes")

# request mirroring: send the request to the server, name the real host in x-hostname
mirror_headers = {**HEADERS, "x-hostname": "cloudflare.hackmap.win"}
if PROXY:
    mirror_headers["x-proxy"] = PROXY
r = requests.get(f"{SERVER}/", headers=mirror_headers)
print("mirror:", r.status_code, len(r.content), "bytes")

r = requests.get(f"{SERVER}/browsers")
print("without token:", r.status_code, "(401 when the server has an auth token)")
