"""Unit tests for jarvise_trade MARKET client (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from jarvise_exchange.binance_spot import BinanceAuth
from jarvise_trade.binance_market import (
    ALLOWED_TRADE_CALLS,
    ORDER_PATH,
    assert_trade_allowlisted,
    place_spot_market_order,
)
from jarvise_trade.flags import live_trading_enabled


def test_live_trading_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_LIVE_TRADING", raising=False)
    assert live_trading_enabled() is False
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "false")
    assert live_trading_enabled() is False
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    assert live_trading_enabled() is True


def test_trade_allowlist_only_order_post() -> None:
    assert ("POST", ORDER_PATH) in ALLOWED_TRADE_CALLS
    assert_trade_allowlisted("POST", ORDER_PATH)
    with pytest.raises(PermissionError):
        assert_trade_allowlisted("DELETE", ORDER_PATH)
    with pytest.raises(PermissionError):
        assert_trade_allowlisted("POST", "/api/v3/order/cancel")


def test_place_spot_market_buy_mocked() -> None:
    auth = BinanceAuth(api_key="k", hmac_secret="s")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"orderId": 12345, "status": "FILLED"}
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = mock_resp

    payload = place_spot_market_order(
        auth,
        symbol="BTCUSDT",
        side="BUY",
        quote_order_qty=50.0,
        client=client,
        timestamp_ms=1_700_000_000_000,
    )
    assert payload["orderId"] == 12345
    assert client.post.call_count == 1
    url = client.post.call_args.args[0]
    assert ORDER_PATH in url
    assert "type=MARKET" in url
    assert "side=BUY" in url
    assert "quoteOrderQty=" in url
    assert client.get.call_count == 0
