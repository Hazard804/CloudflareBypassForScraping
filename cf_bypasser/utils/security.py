import ipaddress
import logging
import os
import socket
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if the IP is loopback/private/link-local/etc and must be rejected."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _parse_ip_literal(host: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Parse host as an IP literal (dotted, integer, hex, octal); None if not an IP."""
    h = host.strip("[]")
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    # integer (2130706433), hex (0x7f000001), octal-ish dotted forms (0177.0.0.1)
    try:
        return ipaddress.ip_address(int(h, 0))
    except (ValueError, OverflowError):
        pass
    try:
        packed = socket.inet_aton(h)
        return ipaddress.ip_address(packed)
    except OSError:
        return None


def _dns_check_enabled() -> bool:
    """Whether hostnames are pre-resolved for the SSRF check (CF_SSRF_DNS_CHECK, on by default)."""
    return os.environ.get("CF_SSRF_DNS_CHECK", "true").lower() in ("1", "true", "yes", "on")


def _is_relayed(proxy: Optional[str]) -> bool:
    """True when the request will be resolved and dialled by a proxy, not by this host."""
    return bool(proxy and "://" in proxy)


def is_safe_url(url: str, proxy: Optional[str] = None) -> bool:
    """Check if the URL is safe (not localhost/private/internal); fails closed.

    IP literals are always checked — they are the cheapest, unambiguous SSRF vector.
    A *hostname* is only pre-resolved when this host will dial it itself. With a proxy the
    name is resolved remotely, so the local answer describes nothing about the real target
    and, behind a censored or fake-ip resolver, is often a planted address (e.g. jable.tv
    resolving to Tumblr/Facebook IPs plus a Teredo 2001:: record that reads as private).
    Trusting that answer rejects perfectly safe targets.
    """
    try:
        parsed_url = urlparse(url)
        if parsed_url.scheme == "file":
            return False
        hostname = parsed_url.hostname
        if not hostname:
            return False

        ip_literal = _parse_ip_literal(hostname)
        if ip_literal is not None:
            return not _ip_is_blocked(ip_literal)

        if _is_relayed(proxy) or not _dns_check_enabled():
            logging.debug(f"Skipping local DNS check for {hostname} (proxied or CF_SSRF_DNS_CHECK=false)")
            return True

        infos = socket.getaddrinfo(hostname, None)
        if not infos:
            return False
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                return False
            if _ip_is_blocked(ip):
                return False
        return True
    except Exception:
        return False


def redact_proxy(proxy: Optional[str]) -> Optional[str]:
    """Proxy URL with its password replaced, safe for logs."""
    if not proxy or "@" not in proxy:
        return proxy
    scheme, sep, rest = proxy.partition("://")
    if not sep:
        scheme, rest = "", proxy
    userinfo, _, hostport = rest.rpartition("@")
    if ":" in userinfo:
        userinfo = userinfo.split(":", 1)[0] + ":***"
    return f"{scheme}{sep}{userinfo}@{hostport}"


def redact_query(path: str) -> str:
    """Request path with the password inside any proxy= query value redacted, for access logs."""
    base, sep, query = path.partition("?")
    if not sep or "proxy" not in query.lower():
        return path
    pairs = []
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name.lower() == "proxy":
            value = redact_proxy(value)
        pairs.append((name, value))
    return f"{base}?{urlencode(pairs)}"


class AccessLogRedactor(logging.Filter):
    """Keeps proxy credentials passed as ?proxy= out of uvicorn's access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            args = list(record.args)
            args[2] = redact_query(args[2])
            record.args = tuple(args)
        return True
