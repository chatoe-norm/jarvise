"""Public-HTTPS-only allow policy for Hermes tool egress."""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

# Hostinger VPS public IPv4 and Tailscale CGNAT block (covers 100.89.171.36).
BLOCKED_HOST_IPS = frozenset(
    {
        ipaddress.ip_address("76.13.218.48"),
    }
)

Resolver = Callable[[str], list[str]]
DEFAULT_SOURCE_POLICY = Path("config/hermes/source-policy.json")


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


def validate_host_addrs(
    host: str,
    *,
    resolve: Resolver | None = None,
) -> tuple[list[str], str]:
    """Resolve once (or accept a literal) and return allowed IP strings.

    If any answer is blocked, the whole name is denied.
    """
    host = (host or "").strip()
    if not host or "/" in host or "@" in host:
        return [], "bad_host"
    try:
        lit = ipaddress.ip_address(host.strip("[]"))
        if is_blocked_ip(lit):
            return [], "blocked_ip"
        return [str(lit)], "ok"
    except ValueError:
        pass

    resolver = resolve or _default_resolve
    try:
        addrs = resolver(host)
    except OSError as exc:
        return [], f"dns_error:{type(exc).__name__}"
    if not addrs:
        return [], "dns_empty"
    allowed: list[str] = []
    for addr_s in addrs:
        try:
            addr = ipaddress.ip_address(addr_s)
        except ValueError:
            return [], "dns_invalid"
        if is_blocked_ip(addr):
            return [], "blocked_dns"
        text = str(addr)
        if text not in allowed:
            allowed.append(text)
    return allowed, "ok"


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
    addrs, reason = validate_host_addrs(host, resolve=resolve)
    if not addrs:
        return False, reason
    return True, "ok"


def allow_connect_target(
    host: str,
    port: int,
    *,
    resolve: Resolver | None = None,
) -> tuple[bool, str, list[str]]:
    """CONNECT proxy gate. Returns (ok, reason, validated_addrs)."""
    if port != 443:
        return False, "port_not_443", []
    if not host or "/" in host or "@" in host:
        return False, "bad_host", []
    addrs, reason = validate_host_addrs(host, resolve=resolve)
    if not addrs:
        return False, reason, []
    return True, "ok", addrs


def allow_redirect_url(
    url: str,
    *,
    resolve: Resolver | None = None,
) -> tuple[bool, str]:
    """Re-check each redirect hop with the same public-HTTPS rules."""
    return allow_https_url(url, resolve=resolve, require_port_443=True)


def load_source_policy(path: Path | None = None) -> dict[str, list[str]]:
    target = path or DEFAULT_SOURCE_POLICY
    if not target.is_file():
        return {"allow_suffixes": [], "deny_hosts": [], "deny_suffixes": []}
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"allow_suffixes": [], "deny_hosts": [], "deny_suffixes": []}
    return {
        "allow_suffixes": [str(x).lower() for x in (data.get("allow_suffixes") or [])],
        "deny_hosts": [str(x).lower() for x in (data.get("deny_hosts") or [])],
        "deny_suffixes": [str(x).lower() for x in (data.get("deny_suffixes") or [])],
    }


def allow_source_url(
    url: str,
    *,
    policy: dict[str, list[str]] | None = None,
    resolve: Resolver | None = None,
    policy_path: Path | None = None,
) -> tuple[bool, str]:
    """HTTPS + public DNS + explicit primary-source allowlist."""
    ok, reason = allow_https_url(url, resolve=resolve)
    if not ok:
        return False, reason
    host = (urlparse(url).hostname or "").lower().strip("[]")
    if not host:
        return False, "missing_host"
    try:
        ipaddress.ip_address(host)
        return False, "ip_literal_host"
    except ValueError:
        pass
    rules = policy if policy is not None else load_source_policy(policy_path)
    if host in rules["deny_hosts"]:
        return False, "denied_host"
    for suffix in rules["deny_suffixes"]:
        if host == suffix or host.endswith("." + suffix):
            return False, "denied_suffix"
    allowed = False
    for suffix in rules["allow_suffixes"]:
        if host == suffix or host.endswith("." + suffix):
            allowed = True
            break
    if not allowed:
        return False, "unknown_source"
    return True, "ok"
