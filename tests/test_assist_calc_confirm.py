"""CALC-3: confirm re-sizes from live mark; never exceeds preview qty (incl. CALC-13)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist import _ms_bangkok, _seed_analysis
from tests.test_assist_execute import RecordingTransport, _client

from jarvise_assist.execute import _confirm_guards_entry
from jarvise_assist.sizing import CHASE_R, real_stop_loss_usdt, size_signal
from jarvise_assist.store import get_exec_intent, insert_exec_intent, write_settings
from jarvise_ingest.db import open_db


def _btc_exchange_info(*, step: str = "0.001", min_notional: str = "5", tick: str = "0.01") -> dict[str, Any]:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": step, "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": min_notional},
                    {"filterType": "PRICE_FILTER", "tickSize": tick},
                ],
            }
        ]
    }


def _seed_btc_long(conn: Any, *, ts: int) -> None:
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="long",
        confidence=0.8,
        invalidation=99.0,
        ts=ts,
    )


def _confirm_handlers(
    *,
    mark: str,
    balance: str = "10000",
    available: str | None = None,
    exchange_info: dict[str, Any] | None = None,
) -> dict[tuple[str, str], Any]:
    return {
        ("GET", "/fapi/v3/balance"): (
            200,
            [
                {
                    "asset": "USDT",
                    "balance": balance,
                    "availableBalance": available if available is not None else balance,
                }
            ],
        ),
        ("GET", "/fapi/v3/positionRisk"): (200, []),
        ("GET", "/fapi/v1/premiumIndex"): (200, {"markPrice": mark}),
        ("GET", "/fapi/v1/exchangeInfo"): (200, exchange_info or _btc_exchange_info()),
    }


def test_confirm_resizes_at_chase_cap_never_above_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "confirm_resize.db")
    write_settings(conn, risk_pct=1.0, enabled=True)
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_btc_long(conn, ts=bar)

    preview_sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99"),
        balance=Decimal("10000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("0.001"),
    )
    assert preview_sized is not None
    preview_qty = preview_sized.qty
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={
            "qty": str(preview_qty),
            "stop": "99",
            "target": str(preview_sized.target),
            "leverage": int(preview_sized.leverage),
            "entry": "100",
            "loss_at_stop": str(preview_sized.loss_at_stop),
            "r_distance": str(preview_sized.r_distance),
        },
        created_at_ms=now,
        expires_at_ms=now + 120_000,
    )
    intent = get_exec_intent(conn, intent_id)
    assert intent is not None

    # Exactly +0.3R from entry 100 / stop 99 → mark 100.3, still allowed.
    mark = Decimal("100.3")
    assert not (mark > Decimal("100") + CHASE_R * Decimal("1"))
    trade = _client(RecordingTransport(_confirm_handlers(mark=str(mark))), base="https://fapi.binance.com")
    guard = _confirm_guards_entry(conn, intent, trade, now_ms_value=now + 1_000)
    assert guard is None

    refreshed = get_exec_intent(conn, intent_id)
    assert refreshed is not None
    sent_qty = Decimal(str(refreshed["preview"]["qty"]))
    assert sent_qty <= preview_qty
    assert real_stop_loss_usdt(qty=sent_qty, entry=mark, stop=Decimal("99")) <= Decimal("100")
    assert Decimal(str(refreshed["preview"]["entry"])) == mark
    assert Decimal(str(refreshed["preview"]["stop"])) == Decimal("99")
    assert Decimal(str(refreshed["preview"]["target"])) == preview_sized.target
    conn.close()


def test_confirm_mark_past_chase_cap_returns_chase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "confirm_chase.db")
    write_settings(conn, risk_pct=1.0, enabled=True)
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_btc_long(conn, ts=bar)
    preview_sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99"),
        balance=Decimal("10000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("0.001"),
    )
    assert preview_sized is not None
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={
            "qty": str(preview_sized.qty),
            "stop": "99",
            "target": str(preview_sized.target),
            "leverage": 1,
            "entry": "100",
            "loss_at_stop": str(preview_sized.loss_at_stop),
            "r_distance": "1",
        },
        created_at_ms=now,
        expires_at_ms=now + 120_000,
    )
    intent = get_exec_intent(conn, intent_id)
    assert intent is not None
    # 100.3 + one tick (0.01) exceeds 0.3R chase.
    trade = _client(
        RecordingTransport(_confirm_handlers(mark="100.31")),
        base="https://fapi.binance.com",
    )
    assert _confirm_guards_entry(conn, intent, trade, now_ms_value=now + 1_000) == "chase"
    conn.close()


def test_confirm_at_preview_entry_keeps_preview_qty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "confirm_same.db")
    write_settings(conn, risk_pct=1.0, enabled=True)
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_btc_long(conn, ts=bar)
    preview_sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99"),
        balance=Decimal("10000"),
        risk_pct=Decimal("1"),
        step_size=Decimal("0.001"),
    )
    assert preview_sized is not None
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={
            "qty": str(preview_sized.qty),
            "stop": "99",
            "target": str(preview_sized.target),
            "leverage": int(preview_sized.leverage),
            "entry": "100",
            "loss_at_stop": str(preview_sized.loss_at_stop),
            "r_distance": str(preview_sized.r_distance),
        },
        created_at_ms=now,
        expires_at_ms=now + 120_000,
    )
    intent = get_exec_intent(conn, intent_id)
    assert intent is not None
    trade = _client(RecordingTransport(_confirm_handlers(mark="100")), base="https://fapi.binance.com")
    assert _confirm_guards_entry(conn, intent, trade, now_ms_value=now + 1_000) is None
    refreshed = get_exec_intent(conn, intent_id)
    assert refreshed is not None
    assert Decimal(str(refreshed["preview"]["qty"])) == preview_sized.qty
    conn.close()


def test_confirm_calc13_min_notional_uses_mark_not_preview_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qty * mark < minNotional <= qty * preview_entry → below_min_notional."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "confirm_min.db")
    write_settings(conn, risk_pct=1.0, enabled=True)
    bar = _ms_bangkok(2026, 11, 2, 3, 0)
    now = _ms_bangkok(2026, 11, 2, 8, 20)
    _seed_btc_long(conn, ts=bar)

    # Preview at entry 100 with qty 0.1 → preview notional 10; mark 90 → notional 9.
    # minNotional 9.5 sits between them.
    intent_id = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={
            "qty": "0.1",
            "stop": "80",
            "target": "140",
            "leverage": 1,
            "entry": "100",
            "loss_at_stop": "2",
            "r_distance": "20",
        },
        created_at_ms=now,
        expires_at_ms=now + 120_000,
    )
    intent = get_exec_intent(conn, intent_id)
    assert intent is not None
    qty = Decimal("0.1")
    mark = Decimal("90")
    preview_entry = Decimal("100")
    min_notional = Decimal("9.5")
    assert qty * mark < min_notional <= qty * preview_entry

    info = _btc_exchange_info(step="0.001", min_notional="9.5", tick="0.01")
    # Wide stop so size_signal at mark still yields qty >= preview (min caps to preview).
    trade = _client(
        RecordingTransport(_confirm_handlers(mark="90", exchange_info=info)),
        base="https://fapi.binance.com",
    )
    assert _confirm_guards_entry(conn, intent, trade, now_ms_value=now + 1_000) == "below_min_notional"
    conn.close()
