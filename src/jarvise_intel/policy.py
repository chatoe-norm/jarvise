"""Public-HTTPS-only allow policy for Hermes tool egress."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse

# Hostinger VPS public IPv4 and Tailscale CGNAT block (covers 100.89.171.36).
BLOCKED_HOST_IPS = frozenset(
    {
        ipaddress.ip_address("76.13.218.48"),
    }
)

Resolver = Callable[[str], list[str]]


def _default_resolve(hostname: str) -> list[str]:
    infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    addrs: list[str] = []
    seen: set[str] = set()
    for info in infos:
        raw = str(info[4][0])
        if raw not in seen:
            seen.add(raw)
            addrs.append(raw)
    return addrs


def is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if addr in BLOCKED_HOST_IPS:
        return True
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return True
    if addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return True
    # CGNAT / Tailscale (100.64.0.0/10)
    if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("100.64.0.0/10"):
        return True
    # Unique-local IPv6
    if isinstance(addr, ipaddress.IPv6Address) and addr in ipaddress.ip_network("fc00::/7"):
        return True
    # Cloud metadata
    if addr == ipaddress.ip_address("169.254.169.254"):
        return True
    return False


def allow_https_url(
    url: str,
    *,
    resolve: Resolver | None = None,
    require_port_443: bool = True,
) -> tuple[bool, str]:
    """Return (ok, reason). Deny HTTP, userinfo, non-443, and private/metadata targets."""
    raw = (url or "").strip()
    if not raw:
        return False, "empty_url"
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        return False, "scheme_not_https"
    if parsed.username is not None or parsed.password is not None:
        return False, "userinfo_forbidden"
    host = parsed.hostname
    if not host:
        return False, "missing_host"
    port = parsed.port
    if require_port_443 and port is not None and port != 443:
        return False, "port_not_443"
    if require_port_443 and port is None and ":" in host and not host.startswith("["):
        # urlparse may leave bare host:port weirdness; treat non-default as deny via parse fail
        pass

    resolver = resolve or _default_resolve
    try:
        # Literal IP in hostname
        lit = ipaddress.ip_address(host.strip("[]"))
        if is_blocked_ip(lit):
            return False, "blocked_ip"
        return True, "ok"
    except ValueError:
        pass

    try:
        addrs = resolver(host)
    except OSError as exc:
        return False, f"dns_error:{type(exc).__name__}"
    if not addrs:
        return False, "dns_empty"
    for addr_s in addrs:
        try:
            addr = ipaddress.ip_address(addr_s)
        except ValueError:
            return False, "dns_invalid"
        if is_blocked_ip(addr):
            return False, "blocked_dns"
    return True, "ok"


def allow_connect_target(
    host: str,
    port: int,
    *,
    resolve: Resolver | None = None,
) -> tuple[bool, str]:
    """CONNECT proxy gate: HTTPS (443) to a public host only."""
    if port != 443:
        return False, "port_not_443"
    if not host or "/" in host or "@" in host:
        return False, "bad_host"
    return allow_https_url(f"https://{host}/", resolve=resolve, require_port_443=True)
