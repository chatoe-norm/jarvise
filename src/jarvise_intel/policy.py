"""Public-HTTPS-only allow policy for Hermes tool egress and digest sources.

CONNECT targets must match ``allow_suffixes`` in ``JARVISE_HERMES_CONNECT_ALLOWLIST``.
When that variable is unset, the file is ``config/hermes/connect-allowlist.json``
(digest sources stay in ``source-policy.json`` and are not a provider allowlist).
A missing, empty, or unreadable allowlist denies every CONNECT.
The hostname is checked against the allowlist before DNS resolution.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from collections.abc import Callable, Mapping
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
DEFAULT_CONNECT_ALLOWLIST = Path("config/hermes/connect-allowlist.json")
# Unset uses DEFAULT_CONNECT_ALLOWLIST. An empty value, or a missing/empty file, denies CONNECT.
CONNECT_ALLOWLIST_ENV = "JARVISE_HERMES_CONNECT_ALLOWLIST"
# Printable ASCII only — the exact string that will be rendered into Markdown.
SOURCE_URL_RE = re.compile(r"^[\x21-\x7e]+$")
# Strict DNS hostname: lowercase labels, no empty labels, at least two labels (F3).
DNS_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


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


def _unwrap_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Normalize IPv4-mapped / transition forms before the global check."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    addr = _unwrap_ip(addr)
    if addr in BLOCKED_HOST_IPS:
        return True
    if not addr.is_global:
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
    # 6to4 / NAT64 transition ranges (still deny even if presented as global).
    if isinstance(addr, ipaddress.IPv6Address):
        if addr in ipaddress.ip_network("2002::/16"):
            return True
        if addr in ipaddress.ip_network("64:ff9b::/96"):
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

    If any answer is blocked, the whole name is denied. Used by the CONNECT proxy only.
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
    except (OSError, ValueError, UnicodeError) as exc:
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


def _https_syntax(url: str, *, require_port_443: bool = True) -> tuple[bool, str, str]:
    """Scheme/userinfo/port/host syntax only. Returns (ok, reason, host)."""
    raw = (url or "").strip()
    if not raw:
        return False, "empty_url", ""
    if not SOURCE_URL_RE.fullmatch(raw):
        return False, "url_not_printable", ""
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        return False, "scheme_not_https", ""
    if parsed.username is not None or parsed.password is not None:
        return False, "userinfo_forbidden", ""
    # Reject parser-differential authority before suffix allowlists (F3).
    # netloc can contain backslash / percent / semicolon that urlparse keeps in hostname.
    authority = parsed.netloc or ""
    if "\\" in authority or "%" in authority or ";" in authority:
        return False, "authority_forbidden_chars", ""
    host = parsed.hostname
    if not host:
        return False, "missing_host", ""
    host_norm = host.lower().strip("[]")
    port = parsed.port
    if require_port_443 and port is not None and port != 443:
        return False, "port_not_443", ""
    return True, "ok", host_norm


def allow_https_url(
    url: str,
    *,
    resolve: Resolver | None = None,
    require_port_443: bool = True,
) -> tuple[bool, str]:
    """Proxy-facing HTTPS check: syntax plus DNS/IP validation."""
    ok, reason, host = _https_syntax(url, require_port_443=require_port_443)
    if not ok:
        return False, reason
    addrs, dns_reason = validate_host_addrs(host, resolve=resolve)
    if not addrs:
        return False, dns_reason
    return True, "ok"


def _suffix_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        text = item.strip().lower().strip(".")
        if text:
            out.append(text)
    return out


def _connect_allowlist_path() -> Path | None:
    raw = os.environ.get(CONNECT_ALLOWLIST_ENV)
    if raw is None:
        return DEFAULT_CONNECT_ALLOWLIST
    text = raw.strip()
    if not text:
        return None
    return Path(text)


def load_connect_allowlist(
    path: Path | None = None,
    *,
    policy: Mapping[str, object] | None = None,
) -> dict[str, list[str]] | None:
    """CONNECT domain rules, or None when the allowlist must fail closed.

    An explicit ``policy`` skips the file. Otherwise ``path`` wins, then
    ``JARVISE_HERMES_CONNECT_ALLOWLIST``, then ``DEFAULT_CONNECT_ALLOWLIST``.
    Missing, unreadable, non-object, or empty ``allow_suffixes`` returns None.
    """
    if policy is not None:
        data: object = policy
    else:
        target = path if path is not None else _connect_allowlist_path()
        if target is None or not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
    if not isinstance(data, dict):
        return None
    allow = _suffix_list(data.get("allow_suffixes"))
    if not allow:
        return None
    if data.get("deny_hosts") is not None and _suffix_list(data.get("deny_hosts")) is None:
        return None
    if data.get("deny_suffixes") is not None and _suffix_list(data.get("deny_suffixes")) is None:
        return None
    return {
        "allow_suffixes": allow,
        "deny_hosts": _suffix_list(data.get("deny_hosts")) or [],
        "deny_suffixes": _suffix_list(data.get("deny_suffixes")) or [],
    }


def _connect_host_allowed(host: str, rules: dict[str, list[str]]) -> tuple[bool, str]:
    host_norm = host.lower().strip().strip("[]").rstrip(".")
    if not host_norm:
        return False, "bad_host"
    if host_norm in rules["deny_hosts"]:
        return False, "denied_host"
    for suffix in rules["deny_suffixes"]:
        if host_norm == suffix or host_norm.endswith("." + suffix):
            return False, "denied_suffix"
    for suffix in rules["allow_suffixes"]:
        if host_norm == suffix or host_norm.endswith("." + suffix):
            return True, "ok"
    return False, "connect_not_allowlisted"


def allow_connect_target(
    host: str,
    port: int,
    *,
    resolve: Resolver | None = None,
    policy: Mapping[str, object] | None = None,
    policy_path: Path | None = None,
) -> tuple[bool, str, list[str]]:
    """CONNECT proxy gate. Returns (ok, reason, validated_addrs).

    Port 443 and hostname grammar run first, then the domain allowlist, then
    DNS/IP checks. A missing or empty allowlist denies without resolving DNS.
    """
    if port != 443:
        return False, "port_not_443", []
    if not host or "/" in host or "@" in host:
        return False, "bad_host", []
    if "\\" in host or "%" in host or ";" in host:
        return False, "bad_host", []
    host_norm = host.lower().strip().strip("[]").rstrip(".")
    if not DNS_HOSTNAME_RE.fullmatch(host_norm):
        return False, "hostname_not_dns", []
    rules = load_connect_allowlist(policy_path, policy=policy)
    if rules is None:
        return False, "connect_allowlist_closed", []
    ok, why = _connect_host_allowed(host, rules)
    if not ok:
        return False, why, []
    addrs, reason = validate_host_addrs(host, resolve=resolve)
    if not addrs:
        return False, reason, []
    return True, "ok", addrs


def allow_redirect_url(
    url: str,
    *,
    resolve: Resolver | None = None,
) -> tuple[bool, str]:
    """Re-check each redirect hop with the same public-HTTPS rules (proxy path)."""
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
    """Adapter source check: syntax + allowlist only. Never resolves DNS.

    ``resolve`` is accepted for call-site compatibility and ignored — the adapter
    does not fetch URLs; CONNECT-time DNS stays on the egress proxy.
    """
    del resolve  # hermetic: adapter never looks up hosts
    ok, reason, host = _https_syntax(url, require_port_443=True)
    if not ok:
        return False, reason
    try:
        ipaddress.ip_address(host)
        return False, "ip_literal_host"
    except ValueError:
        pass
    if not DNS_HOSTNAME_RE.fullmatch(host):
        return False, "hostname_not_dns"
    rules = policy if policy is not None else load_source_policy(policy_path)
    if host in rules["deny_hosts"]:
        return False, "denied_host"
    for suffix in rules["deny_suffixes"]:
        if host == suffix or host.endswith("." + suffix):
            return False, "denied_suffix"
    for suffix in rules["allow_suffixes"]:
        if host == suffix or host.endswith("." + suffix):
            return True, "ok"
    return False, "unknown_source"
