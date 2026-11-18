"""Shared pytest fixtures for Jarvise tests."""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

import pytest

from jarvise_assist import partner_fx as fx
from jarvise_assist import partner_kyc as pk
from jarvise_assist import partner_store as ps


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
