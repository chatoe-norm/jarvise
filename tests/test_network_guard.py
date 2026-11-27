"""Prove the suite network guard trips on non-loopback hosts."""

from __future__ import annotations

import socket

import pytest

from conftest import NetworkBlockedError

# RFC 5737 TEST-NET-3 — never routed on the public Internet.
_SENTINEL_IP = "203.0.113.1"
_SENTINEL_HOST = "example.invalid"


def test_network_blocked_error_is_connection_refused() -> None:
    err = NetworkBlockedError("blocked")
    assert isinstance(err, OSError)
    assert isinstance(err, ConnectionRefusedError)


def test_network_guard_blocks_public_connect() -> None:
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("example.com", 80), timeout=0.1)


def test_network_guard_blocks_public_getaddrinfo() -> None:
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("api.binance.com", 443)


def test_network_guard_allows_loopback_getaddrinfo() -> None:
    infos = socket.getaddrinfo("127.0.0.1", 9)
    assert infos


def test_network_guard_blocks_connect_ex_numeric_ip() -> None:
    """Numeric public IPs must be rejected before the real connect_ex runs."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlockedError):
            sock.connect_ex(("8.8.8.8", 53))
    finally:
        sock.close()


def test_network_guard_blocks_sendto_numeric_ip() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(NetworkBlockedError):
            sock.sendto(b"x", ("1.1.1.1", 53))
    finally:
        sock.close()


def test_network_guard_blocks_sendmsg_sentinel_ip() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(NetworkBlockedError) as ei:
            sock.sendmsg([b"x"], [], 0, (_SENTINEL_IP, 53))
        assert isinstance(ei.value, ConnectionRefusedError)
    finally:
        sock.close()


def test_network_guard_blocks_gethostbyname_sentinel() -> None:
    with pytest.raises(NetworkBlockedError) as ei:
        socket.gethostbyname(_SENTINEL_HOST)
    assert isinstance(ei.value, OSError)


def test_network_guard_blocks_gethostbyname_ex_sentinel() -> None:
    with pytest.raises(NetworkBlockedError) as ei:
        socket.gethostbyname_ex(_SENTINEL_HOST)
    assert isinstance(ei.value, ConnectionRefusedError)


def test_network_guard_allows_loopback_gethostbyname() -> None:
    assert socket.gethostbyname("127.0.0.1") == "127.0.0.1"
