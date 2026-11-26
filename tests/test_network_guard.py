"""Prove the suite network guard trips on non-loopback hosts."""

from __future__ import annotations

import socket

import pytest

from conftest import NetworkBlockedError


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
