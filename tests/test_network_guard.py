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
