"""Live SELL exit + realized PnL from fills (mocked venue)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from jarvise_ingest.db import (
    ensure_paper_account,
    get_live_order,
    insert_live_order,
    open_db,
    sum_live_realized_pnl_utc_day,
)
from jarvise_trade.binance_market import SymbolFilters
from jarvise_trade.submit import submit_live_for_approval

SAFE_PERMS = {
    "enableWithdrawals": False,
    "enableInternalTransfer": False,
    "permitsUniversalTransfer": False,
    "enableSpotAndMarginTrading": True,
}


def test_sell_records_realized_pnl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TRADE_API_SECRET", "s")
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    insert_live_order(
        conn,
        {
            "id": "buy1",
            "created_at_ms": 1,
            "approval_id": "open1",
            "venue": "binance",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "status": "filled",
            "executed_qty": 0.01,
            "cummulative_quote_qty": 1000.0,
            "kill_switch_clear": 1,
            "caps_ok": 1,
            "realized_pnl_usd": 0.0,
        },
    )
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", return_value=None),
        patch(
            "jarvise_trade.submit.place_spot_market_order",
            return_value={
                "orderId": 9,
                "status": "FILLED",
                "executedQty": "0.01",
                "cummulativeQuoteQty": "1010",
            },
        ) as place,
    ):
        result = submit_live_for_approval(
            conn,
            approval={"id": "flat1", "symbol": "BTCUSDT", "action": "flat", "size_pct_equity": 0.0},
            now_ms=2_000,
        )
    assert result["ok"] is True
    assert place.call_args.kwargs["side"] == "SELL"
    assert place.call_args.kwargs["quantity"] == Decimal("0.01")
    pnl = result["live_order"]["realized_pnl_usd"]
    assert abs(float(pnl) - 10.0) < 1e-6
    day = sum_live_realized_pnl_utc_day(conn, day_start_ms=0, day_end_ms=10_000)
    assert abs(day - 10.0) < 1e-6


def _seed_buy(conn, *, qty: float = 0.01, quote: float = 1000.0, approval_id: str = "open1") -> None:
    insert_live_order(
        conn,
        {
            "id": "buy1",
            "created_at_ms": 1,
            "approval_id": approval_id,
            "venue": "binance",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "status": "filled",
            "executed_qty": qty,
            "cummulative_quote_qty": quote,
            "kill_switch_clear": 1,
            "caps_ok": 1,
            "realized_pnl_usd": 0.0,
            "client_order_id": f"jrv-{approval_id}",
        },
    )


def _live_env(monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TRADE_API_SECRET", "s")
    monkeypatch.setenv("JARVISE_LIVE_EQUITY_USD", "10000")


def test_flat_sell_is_fee_net_and_lot_floored(tmp_path: Path, monkeypatch, venue_sizing) -> None:
    _live_env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    _seed_buy(conn)
    venue_sizing["filters"].side_effect = None
    venue_sizing["filters"].return_value = SymbolFilters(
        symbol="BTCUSDT",
        base_asset="BTC",
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.01"),
    )
    venue_sizing["free"].return_value = Decimal("0.00999")
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", return_value=None),
        patch(
            "jarvise_trade.submit.place_spot_market_order",
            return_value={
                "orderId": 9,
                "status": "FILLED",
                "executedQty": "0.00999",
                "cummulativeQuoteQty": "1008",
            },
        ) as place,
    ):
        result = submit_live_for_approval(
            conn,
            approval={"id": "flat1", "symbol": "BTCUSDT", "action": "flat", "size_pct_equity": 0.0},
            now_ms=2_000,
        )
    assert result["ok"] is True
    assert place.call_args.kwargs["quantity"] == Decimal("0.00999")
    assert result["live_order"]["requested_qty"] == 0.00999


def test_protective_stop_is_fee_net_and_tick_floored(
    tmp_path: Path, monkeypatch, venue_sizing, _mute_protective_stop
) -> None:
    _live_env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    venue_sizing["filters"].side_effect = None
    venue_sizing["filters"].return_value = SymbolFilters(
        symbol="BTCUSDT",
        base_asset="BTC",
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("0.1"),
        tick_size=Decimal("0.01"),
    )
    venue_sizing["free"].return_value = Decimal("0.004995")
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", return_value=None),
        patch(
            "jarvise_trade.submit.place_spot_market_order",
            return_value={
                "orderId": 7,
                "status": "FILLED",
                "executedQty": "0.005",
                "cummulativeQuoteQty": "500",
            },
        ),
    ):
        result = submit_live_for_approval(
            conn,
            approval={
                "id": "long1",
                "symbol": "BTCUSDT",
                "action": "long",
                "size_pct_equity": 1.0,
                "invalidation_price": 97.123,
            },
            now_ms=2_000,
        )
    assert result["ok"] is True
    kwargs = _mute_protective_stop.call_args.kwargs
    assert kwargs["quantity"] == Decimal("0.00499")
    assert kwargs["stop_price"] == Decimal("97.12")
    assert kwargs["limit_price"] == Decimal("97.02")
    assert kwargs["new_client_order_id"] == "jrv-xlong1"
    assert result["stop_order"]["status"] == "submitted"
    assert result["stop_order"]["requested_qty"] == 0.00499


def test_protective_stop_dust_records_error_without_post(
    tmp_path: Path, monkeypatch, venue_sizing, _mute_protective_stop
) -> None:
    _live_env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    venue_sizing["filters"].side_effect = None
    venue_sizing["filters"].return_value = SymbolFilters(
        symbol="BTCUSDT",
        base_asset="BTC",
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.01"),
    )
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", return_value=None),
        patch(
            "jarvise_trade.submit.place_spot_market_order",
            return_value={
                "orderId": 7,
                "status": "FILLED",
                "executedQty": "0.0005",
                "cummulativeQuoteQty": "50",
            },
        ),
    ):
        result = submit_live_for_approval(
            conn,
            approval={
                "id": "long2",
                "symbol": "BTCUSDT",
                "action": "long",
                "size_pct_equity": 0.5,
                "invalidation_price": 97.0,
            },
            now_ms=2_000,
        )
    assert result["ok"] is True
    assert _mute_protective_stop.call_count == 0
    assert result["stop_order"]["status"] == "error"
    assert "dust" in result["stop_order"]["error"]


def test_flat_sell_cancels_own_stop_before_selling(tmp_path: Path, monkeypatch) -> None:
    _live_env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    _seed_buy(conn)
    insert_live_order(
        conn,
        {
            "id": "stop1",
            "created_at_ms": 2,
            "approval_id": "open1",
            "venue": "binance",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "order_type": "STOP_LOSS_LIMIT",
            "status": "submitted",
            "venue_status": "NEW",
            "client_order_id": "jrv-xopen1",
            "kill_switch_clear": 1,
            "caps_ok": 1,
        },
    )
    calls: list[str] = []

    def cancel_stop(*_args, **_kwargs) -> dict:
        calls.append("cancel")
        return {"status": "CANCELED", "executedQty": "0", "cummulativeQuoteQty": "0"}

    def market_sell(*_args, **_kwargs) -> dict:
        calls.append("sell")
        return {"orderId": 9, "status": "FILLED", "executedQty": "0.01", "cummulativeQuoteQty": "1010"}

    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", return_value=None),
        patch("jarvise_trade.submit.cancel_order", side_effect=cancel_stop) as cancel,
        patch("jarvise_trade.submit.place_spot_market_order", side_effect=market_sell),
    ):
        result = submit_live_for_approval(
            conn,
            approval={"id": "flat1", "symbol": "BTCUSDT", "action": "flat", "size_pct_equity": 0.0},
            now_ms=3_000,
        )
    assert result["ok"] is True
    assert calls == ["cancel", "sell"]
    assert cancel.call_args.kwargs["orig_client_order_id"] == "jrv-xopen1"
    assert result["cancelled"] == ["stop1"]
    assert get_live_order(conn, "stop1")["status"] == "canceled"
