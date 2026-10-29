"""Head appearance: a python-requests caller must not look like a bot to Cloudflare.

These cover the mirror header rewrite: library transport defaults and client hints are
replaced with the ones the bypassed browser would send, while genuine caller intent
(custom Accept, cookies, custom headers) survives.
"""

from cf_bypasser.core.mirror import RequestMirror, client_hint_headers
from cf_bypasser.utils.constants import CHROME_HEADERS

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/156.0.0.0 Safari/537.36")

CF_DATA = {"cookies": {"cf_clearance": "abc", "__cf_bm": "bm", "user_uuid": "drop-me"}, "user_agent": UA}

# what `requests.get()` puts on the wire
REQUESTS_HEADERS = {
    "user-agent": "python-requests/2.32.3",
    "accept-encoding": "gzip, deflate",
    "accept": "*/*",
    "connection": "keep-alive",
    "x-hostname": "missav.ws",
    "cookie": "__Host-sid=keep",
}


def prepare(headers):
    return RequestMirror(bypasser=object())._prepare_request_headers(headers, CF_DATA)


def test_requests_defaults_are_replaced_with_browser_values():
    h = prepare(REQUESTS_HEADERS)
    lower = {k.lower(): v for k, v in h.items()}

    assert lower["user-agent"] == UA
    assert lower["accept-encoding"] == CHROME_HEADERS["Accept-Encoding"]
    assert lower["accept"] == CHROME_HEADERS["Accept"]
    assert "connection" not in lower  # hop-by-hop, belongs to curl


def test_client_hints_match_the_bypassed_user_agent():
    lower = {k.lower(): v for k, v in prepare(REQUESTS_HEADERS).items()}

    assert 'v="156"' in lower["sec-ch-ua"]
    assert lower["sec-ch-ua-platform"] == '"Windows"'
    assert lower["sec-ch-ua-mobile"] == "?0"
    assert lower["sec-fetch-dest"] == "document"
    assert lower["sec-fetch-mode"] == "navigate"
    assert lower["sec-fetch-site"] == "none"
    # a browser-issued clearance cookie set is merged, the site's own session is the caller's
    assert lower["cookie"] == "__Host-sid=keep; cf_clearance=abc; __cf_bm=bm"


def test_custom_accept_is_preserved_and_hints_switch_to_xhr():
    h = prepare({**REQUESTS_HEADERS, "accept": "application/json"})
    lower = {k.lower(): v for k, v in h.items()}

    assert lower["accept"] == "application/json"
    assert lower["sec-fetch-dest"] == "empty"
    assert lower["sec-fetch-mode"] == "cors"
    assert "sec-fetch-user" not in lower


def test_control_and_auth_headers_are_not_forwarded():
    h = prepare({**REQUESTS_HEADERS, "x-auth-token": "secret", "x-proxy": "http://p:1", "x-bypass-cache": "true"})
    lower = {k.lower() for k in h}

    assert not {"x-hostname", "x-auth-token", "x-proxy", "x-bypass-cache"} & lower
    assert "secret" not in str(h)


def test_client_hints_follow_a_non_chrome_user_agent():
    hints = client_hint_headers("Mozilla/5.0 (X11; Linux x86_64) Firefox/133.0", document=False)

    assert hints["sec-ch-ua-platform"] == '"Linux"'
    assert hints["sec-fetch-dest"] == "empty"
