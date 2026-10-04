"""Live SELL exit + realized PnL from fills (mocked venue)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jarvise_ingest.db import ensure_paper_account, insert_live_order, open_db, sum_live_realized_pnl_utc_day
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
    assert place.call_args.kwargs["quantity"] == 0.01
    pnl = result["live_order"]["realized_pnl_usd"]
    assert abs(float(pnl) - 10.0) < 1e-6
    day = sum_live_realized_pnl_utc_day(conn, day_start_ms=0, day_end_ms=10_000)
    assert abs(day - 10.0) < 1e-6
