"""Approve live branch — flag off unchanged; flag on uses mocked trade HTTP."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from jarvise_ingest.db import (
    ensure_paper_account,
    get_approval,
    list_paper_orders,
    open_db,
    upsert_market_technicals,
)
from jarvise_paper.approval import approve_approval, enqueue_approval


@pytest.fixture(autouse=True)
def _portfolio_room_for_legacy_sizes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_MAX_SYMBOL_NOTIONAL_PCT", "50")
    monkeypatch.setenv("JARVISE_MAX_GROSS_NOTIONAL_PCT", "50")
    monkeypatch.setenv("JARVISE_MAX_CORRELATED_BUCKET_PCT", "50")
    monkeypatch.setenv("JARVISE_MAX_OPEN_POSITIONS", "10")


def _seed_candle(conn, symbol: str, timeframe: str, close: float) -> None:
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": symbol,
                "timestamp": 1_700_000_000_000,
                "timeframe": timeframe,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1.0,
            }
        ],
    )


def test_approve_live_flag_off_no_trade_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_LIVE_TRADING", raising=False)
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    _seed_candle(conn, "BTCUSDT", "4h", 100.0)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a1",
            "symbol": "BTCUSDT",
            "action": "long",
            "invalidation_price": 97.0,
            "size_pct_equity": 10.0,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    with patch("jarvise_trade.binance_market.place_spot_market_order") as place:
        result = approve_approval(conn, row["id"], kill_switch=False, now_ms=2_000)
    assert result["ok"] is True
    assert result["paper_only"] is True
    assert place.call_count == 0
    assert list_paper_orders(conn)


def test_approve_live_missing_keys_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    monkeypatch.setenv("JARVISE_LIVE_EQUITY_USD", "10000")
    monkeypatch.delenv("BINANCE_TRADE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TRADE_API_SECRET", raising=False)
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a2",
            "symbol": "BTCUSDT",
            "action": "long",
            "invalidation_price": 97.0,
            "size_pct_equity": 5.0,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    result = approve_approval(conn, row["id"], kill_switch=False, now_ms=2_000)
    assert result["ok"] is False
    assert result["paper_only"] is False
    assert "missing" in (result["error"] or "").lower()
    assert get_approval(conn, row["id"])["status"] == "failed"
    assert list_paper_orders(conn) == []


def test_approve_live_mocked_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "tk")
    monkeypatch.setenv("BINANCE_TRADE_API_SECRET", "ts")
    monkeypatch.setenv("JARVISE_LIVE_EQUITY_USD", "10000")
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a3",
            "symbol": "BTCUSDT",
            "action": "long",
            "invalidation_price": 97.0,
            "size_pct_equity": 5.0,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"orderId": 777, "status": "FILLED"}
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = mock_resp

    with (
        patch("jarvise_trade.submit.place_spot_market_order") as place,
        patch("jarvise_trade.submit.query_order", return_value=None) as query,
    ):
        place.return_value = {
            "orderId": 777,
            "clientOrderId": "jrv-" + row["id"],
            "status": "FILLED",
            "executedQty": "0.00500000",
            "cummulativeQuoteQty": "500.00000000",
            "fills": [{"price": "100000", "qty": "0.005"}],
        }
        with patch(
            "jarvise_trade.submit.fetch_api_restrictions",
            return_value={
                "enableWithdrawals": False,
                "enableInternalTransfer": False,
                "permitsUniversalTransfer": False,
                "enableSpotAndMarginTrading": True,
            },
        ):
            result = approve_approval(conn, row["id"], kill_switch=False, now_ms=2_000)

    assert result["ok"] is True
    assert result["paper_only"] is False
    live = result["live_order"]
    assert live["status"] == "filled"
    assert live["venue_status"] == "FILLED"
    assert live["venue_order_id"] == "777"
    assert live["client_order_id"] == "jrv-" + row["id"]
    assert live["executed_qty"] == 0.005
    assert live["cummulative_quote_qty"] == 500.0
    assert live["fills_count"] == 1
    assert list_paper_orders(conn) == []
    assert get_approval(conn, row["id"])["status"] == "approved"
    assert place.call_count == 1
    # Pre-submit venue query ran once with the deterministic client order id.
    assert query.call_count == 1
    assert query.call_args.kwargs["orig_client_order_id"] == "jrv-" + row["id"]
    assert place.call_args.kwargs["new_client_order_id"] == "jrv-" + row["id"]


def test_approve_live_blocks_withdraw_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "tk")
    monkeypatch.setenv("BINANCE_TRADE_API_SECRET", "ts")
    monkeypatch.setenv("JARVISE_LIVE_EQUITY_USD", "10000")
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a5",
            "symbol": "BTCUSDT",
            "action": "long",
            "invalidation_price": 97.0,
            "size_pct_equity": 5.0,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    with patch(
        "jarvise_trade.submit.fetch_api_restrictions",
        return_value={
            "enableWithdrawals": True,
            "enableSpotAndMarginTrading": True,
        },
    ):
        with patch("jarvise_trade.submit.place_spot_market_order") as place:
            result = approve_approval(conn, row["id"], kill_switch=False, now_ms=2_000)
    assert result["ok"] is False
    assert "withdraw" in (result["error"] or "").lower()
    assert place.call_count == 0


def test_approve_live_flat_skips_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "tk")
    monkeypatch.setenv("BINANCE_TRADE_API_SECRET", "ts")
    conn = open_db(tmp_path / "p.db")
    ensure_paper_account(conn)
    row = enqueue_approval(
        conn,
        analysis={
            "analysis_id": "a4",
            "symbol": "BTCUSDT",
            "action": "flat",
            "invalidation_price": 97.0,
            "size_pct_equity": 0.0,
        },
        timeframe="4h",
        now_ms=1_000,
    )
    with patch("jarvise_trade.submit.place_spot_market_order") as place:
        result = approve_approval(conn, row["id"], kill_switch=False, now_ms=2_000)
    assert result["ok"] is True
    assert result.get("skipped") is True
    assert place.call_count == 0
    assert result["live_order"]["status"] == "skipped"
