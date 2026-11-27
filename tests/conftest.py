"""Shared pytest fixtures for Jarvise tests."""

from __future__ import annotations

import base64
import os
import secrets
import socket
from pathlib import Path

import pytest

from jarvise_assist import partner_fx as fx
from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_store as ps


class NetworkBlockedError(ConnectionRefusedError):
    """Raised when a test accidentally opens a non-loopback socket.

    Subclasses ConnectionRefusedError (an OSError) so urllib/httpx wrap it like a
    real refused connection (URLError), matching Hermes gateway error handling.
    """


def _is_loopback_host(host: object) -> bool:
    if host is None:
        return False
    h = str(host).strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if "%" in h:
        h = h.split("%", 1)[0]
    return h in {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"}


@pytest.fixture(autouse=True)
def _block_non_loopback_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: no real HTTP/DNS to the public internet during the suite.

    Loopback stays allowed (local test servers). External connect / getaddrinfo
    / gethostbyname / sendmsg for non-local hosts raise immediately so a slipped
    Binance/Telegram call cannot leave the machine.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_sendmsg = socket.socket.sendmsg
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_gethostbyname_ex = socket.gethostbyname_ex

    def _guard_host(host: object) -> None:
        if _is_loopback_host(host):
            return
        raise NetworkBlockedError(f"non-loopback network blocked in tests: {host!r}")

    def _host_from_address(address: object) -> object:
        if isinstance(address, tuple) and address:
            return address[0]
        return address

    def connect(self: socket.socket, address: object) -> None:  # noqa: ANN001
        _guard_host(_host_from_address(address))
        return real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:  # noqa: ANN001
        _guard_host(_host_from_address(address))
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    def sendto(self: socket.socket, data: object, *args: object, **kwargs: object) -> int:  # noqa: ANN001
        # sendto(data, address) or sendto(data, flags, address)
        address: object | None = None
        if args:
            address = args[-1]
        elif "address" in kwargs:
            address = kwargs["address"]
        if address is not None:
            _guard_host(_host_from_address(address))
        return real_sendto(self, data, *args, **kwargs)  # type: ignore[arg-type]

    def sendmsg(  # noqa: ANN001
        self: socket.socket,
        buffers: object,
        ancdata: object = (),
        flags: int = 0,
        address: object | None = None,
    ) -> int:
        if address is not None:
            _guard_host(_host_from_address(address))
        return real_sendmsg(self, buffers, ancdata, flags, address)  # type: ignore[arg-type]

    def create_connection(address: object, *args: object, **kwargs: object) -> socket.socket:
        _guard_host(_host_from_address(address))
        return real_create_connection(address, *args, **kwargs)  # type: ignore[arg-type]

    def getaddrinfo(host: object, *args: object, **kwargs: object) -> list[object]:
        if not _is_loopback_host(host):
            raise NetworkBlockedError(f"non-loopback DNS blocked in tests: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    def gethostbyname(host: object) -> str:
        _guard_host(host)
        return real_gethostbyname(host)  # type: ignore[arg-type]

    def gethostbyname_ex(host: object) -> tuple[str, list[str], list[str]]:
        _guard_host(host)
        return real_gethostbyname_ex(host)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", sendmsg)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", gethostbyname_ex)


def _seed_kyc_form(partner_id: str) -> str:
    tail = str(partner_id).replace("pt_", "")
    number = int(tail[:8], 16) if tail else 1
    account = f"{number % 10**10:010d}"
    phone = f"08{number % 10**8:08d}"
    return (
        f"ข้อมูลรับเงิน\nชื่อจริง: ทดสอบ ระบบ\nเบอร์: {phone}\nธนาคาร: KBANK\nเลขบัญชี: {account}\nชื่อบัญชี: ทดสอบ ระบบ\n"
    )


@pytest.fixture(autouse=True)
def _seed_partner_kyc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give every non-admin partner a saved KYC row so existing deposit tests stay valid.

    Tests that need the first-deposit gate set ``JARVISE_TEST_NO_KYC_SEED=1``.
    The key is random and lives only in the test tmp directory.
    """
    key_path = tmp_path / "partner_kyc.key"
    key_path.write_text(base64.b64encode(secrets.token_bytes(32)).decode("ascii"), encoding="utf-8")
    monkeypatch.setenv("JARVISE_PARTNER_KYC_KEY_PATH", str(key_path))
    original = ps.create_partner

    def _wrapped(conn: object, **kwargs: object) -> dict[str, object]:
        result = original(conn, **kwargs)  # type: ignore[arg-type]
        if os.environ.get("JARVISE_TEST_NO_KYC_SEED") == "1":
            return result
        if not result.get("ok") or kwargs.get("is_admin"):
            return result
        saved = pk.admin_set_kyc(
            conn,  # type: ignore[arg-type]
            str(result["partner_id"]),
            _seed_kyc_form(str(result["partner_id"])),
            "seeded for existing tests",
            int(kwargs["now_ms"]),  # type: ignore[arg-type]
        )
        if not saved.get("ok"):
            raise RuntimeError(str(saved.get("reason")))
        return result

    monkeypatch.setattr(ps, "create_partner", _wrapped)


@pytest.fixture(autouse=True)
def _thb_flow_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR-2 tests see every paid payout as in the THB flow unless a test says otherwise."""
    monkeypatch.setattr(fx, "THB_FLOW_SINCE_MS", 0)


@pytest.fixture(autouse=True)
def _default_live_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live sizing must not fall back to paper equity; tests that need fail-closed
    can ``delenv("JARVISE_LIVE_EQUITY_USD")``."""
    monkeypatch.setenv("JARVISE_LIVE_EQUITY_USD", "10000")


@pytest.fixture(autouse=True)
def _disable_market_safety_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing paper/approval tests seed OHLCV only; gate is covered in
    test_market_safety.py with explicit MarketSafetyConfig."""
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")


@pytest.fixture(autouse=True)
def _no_http_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry/backoff must never slow the suite; test_http_retry asserts the delays explicitly.
    Circuit-breaker state is in-process here (no REDIS_URL) and reset per test."""
    monkeypatch.setattr("jarvise_ingest.http._sleep", lambda seconds: None)
    monkeypatch.setattr("jarvise_ingest.http._LOCAL_FAILURES", {})
    monkeypatch.setattr("jarvise_ingest.http._LOCAL_OPEN_UNTIL", {})


@pytest.fixture(autouse=True)
def _no_entry_bar_15m_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assist trail advice must not hit Binance 15m klines during the suite."""
    import jarvise_assist.trail_advice as trail_advice

    trail_advice._ENTRY_BAR_CACHE.clear()
    trail_advice._ENTRY_BAR_FAIL_MEMO.clear()
    monkeypatch.setattr(trail_advice, "_ENTRY_BAR_FETCH", lambda *_a, **_k: [])
