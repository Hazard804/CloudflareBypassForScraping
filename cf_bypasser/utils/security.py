from urllib.parse import urlparse


def is_safe_url(url: str) -> bool:
    """Basic URL shape check; SSRF hostname/IP filtering is intentionally disabled."""
    try:
        parsed_url = urlparse(url)
        if parsed_url.scheme == "file":
            return False
        hostname = parsed_url.hostname
        if not hostname:
            return False
        return parsed_url.scheme in ("http", "https")
    except Exception:
        return False
