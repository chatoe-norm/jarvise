"""Tests for live_orders schema + helpers."""

from pathlib import Path

from jarvise_ingest.db import (
    get_live_order,
    insert_live_order,
    open_db,
    sum_live_realized_pnl_utc_day,
)


def test_live_orders_roundtrip(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "l.db")
    row = insert_live_order(
        conn,
        {
            "id": "lo1",
            "created_at_ms": 1_700_000_000_000,
            "approval_id": "ap1",
            "venue": "binance",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "requested_notional_usd": 100.0,
            "status": "submitted",
            "venue_order_id": "99",
            "kill_switch_clear": 1,
            "caps_ok": 1,
            "realized_pnl_usd": 0.0,
        },
    )
    assert row["id"] == "lo1"
    assert get_live_order(conn, "lo1")["venue_order_id"] == "99"


def test_sum_live_realized_pnl_utc_day(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "l.db")
    day = 1_700_000_000_000
    insert_live_order(
        conn,
        {
            "id": "a",
            "created_at_ms": day + 1,
            "venue": "binance",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "status": "submitted",
            "kill_switch_clear": 1,
            "caps_ok": 1,
            "realized_pnl_usd": -40.0,
        },
    )
    insert_live_order(
        conn,
        {
            "id": "b",
            "created_at_ms": day + 2,
            "venue": "binance",
            "symbol": "ETHUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "status": "submitted",
            "kill_switch_clear": 1,
            "caps_ok": 1,
            "realized_pnl_usd": -10.0,
        },
    )
    insert_live_order(
        conn,
        {
            "id": "c",
            "created_at_ms": day + 86_400_000 + 5,
            "venue": "binance",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "status": "submitted",
            "kill_switch_clear": 1,
            "caps_ok": 1,
            "realized_pnl_usd": -999.0,
        },
    )
    total = sum_live_realized_pnl_utc_day(
        conn, day_start_ms=day, day_end_ms=day + 86_400_000
    )
    assert total == -50.0
