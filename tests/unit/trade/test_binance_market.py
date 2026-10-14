"""Unit tests for jarvise_trade MARKET client (mocked HTTP)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest

from jarvise_exchange.binance_spot import ACCOUNT_PATH, BinanceAuth
from jarvise_trade.binance_market import (
    ALLOWED_TRADE_CALLS,
    EXCHANGE_INFO_PATH,
    ORDER_PATH,
    SymbolFilters,
    assert_trade_allowlisted,
    cancel_order,
    fetch_free_balance,
    fetch_symbol_filters,
    floor_to_step,
    place_spot_market_order,
    place_spot_stop_loss_limit,
)
from jarvise_trade.flags import live_trading_enabled

AUTH = BinanceAuth(api_key="k", hmac_secret="s")


def _resp(payload: object, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = ""
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError("bad", request=MagicMock(), response=resp)
    else:
        resp.raise_for_status = MagicMock()
    return resp


def test_live_trading_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_LIVE_TRADING", raising=False)
    assert live_trading_enabled() is False
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "false")
    assert live_trading_enabled() is False
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    assert live_trading_enabled() is True


def test_trade_allowlist_order_cancel_and_reads_only() -> None:
    assert ALLOWED_TRADE_CALLS == frozenset(
        {
            ("POST", ORDER_PATH),
            ("GET", ORDER_PATH),
            ("DELETE", ORDER_PATH),
            ("GET", ACCOUNT_PATH),
            ("GET", EXCHANGE_INFO_PATH),
        }
    )
    assert_trade_allowlisted("DELETE", ORDER_PATH)
    for method, path in (
        ("DELETE", "/api/v3/openOrders"),
        ("POST", "/api/v3/order/cancelReplace"),
        ("POST", "/api/v3/order/cancel"),
        ("POST", ACCOUNT_PATH),
        ("POST", "/sapi/v1/capital/withdraw/apply"),
    ):
        with pytest.raises(PermissionError):
            assert_trade_allowlisted(method, path)


def test_place_spot_market_buy_mocked() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp({"orderId": 12345, "status": "FILLED"})

    payload = place_spot_market_order(
        AUTH,
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


def test_decimal_quantities_keep_step_precision() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _resp({"orderId": 1, "status": "NEW"})
    place_spot_stop_loss_limit(
        AUTH,
        symbol="BTCUSDT",
        side="SELL",
        quantity=Decimal("0.00998000"),
        stop_price=Decimal("83288.00"),
        limit_price=Decimal("83204.71"),
        client=client,
        timestamp_ms=1,
    )
    url = client.post.call_args.args[0]
    assert "quantity=0.00998&" in url
    assert "stopPrice=83288&" in url
    assert "price=83204.71&" in url


def test_cancel_order_by_client_id() -> None:
    client = MagicMock(spec=httpx.Client)
    client.delete.return_value = _resp({"orderId": 7, "status": "CANCELED", "executedQty": "0"})
    payload = cancel_order(
        AUTH, symbol="btcusdt", orig_client_order_id="jrv-xabc", client=client, timestamp_ms=1
    )
    assert payload is not None and payload["status"] == "CANCELED"
    url = client.delete.call_args.args[0]
    assert ORDER_PATH in url and "origClientOrderId=jrv-xabc" in url and "symbol=BTCUSDT" in url
    assert client.post.call_count == 0


def test_cancel_order_unknown_returns_none() -> None:
    client = MagicMock(spec=httpx.Client)
    client.delete.return_value = _resp({"code": -2011, "msg": "Unknown order sent."}, status=400)
    assert (
        cancel_order(AUTH, symbol="BTCUSDT", orig_client_order_id="jrv-x", client=client, timestamp_ms=1)
        is None
    )


def test_cancel_order_other_error_raises() -> None:
    client = MagicMock(spec=httpx.Client)
    client.delete.return_value = _resp({"code": -1021, "msg": "Timestamp outside recvWindow"}, status=400)
    with pytest.raises(httpx.HTTPStatusError):
        cancel_order(AUTH, symbol="BTCUSDT", orig_client_order_id="jrv-x", client=client, timestamp_ms=1)


def _exchange_info(filters: list[dict]) -> dict:
    return {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "filters": filters}]}


def test_fetch_symbol_filters_parses_lot_price_and_notional() -> None:
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = _resp(
        _exchange_info(
            [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
                {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
            ]
        )
    )
    filters = fetch_symbol_filters("btcusdt", client=client)
    assert filters == SymbolFilters(
        symbol="BTCUSDT",
        base_asset="BTC",
        step_size=Decimal("0.00001"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.01"),
    )
    assert EXCHANGE_INFO_PATH in client.get.call_args.args[0]
    assert client.get.call_args.kwargs["params"] == {"symbol": "BTCUSDT"}


def test_fetch_symbol_filters_legacy_min_notional() -> None:
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = _resp(
        _exchange_info(
            [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
            ]
        )
    )
    filters = fetch_symbol_filters("BTCUSDT", client=client)
    assert filters.min_notional == Decimal("10")
    assert filters.tick_size == Decimal("0")


def test_fetch_symbol_filters_without_lot_size_fails_closed() -> None:
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = _resp(_exchange_info([{"filterType": "PRICE_FILTER", "tickSize": "0.01"}]))
    with pytest.raises(ValueError, match="LOT_SIZE"):
        fetch_symbol_filters("BTCUSDT", client=client)


def test_fetch_free_balance_signed_account_read() -> None:
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = _resp(
        {
            "balances": [
                {"asset": "BTC", "free": "0.00999000", "locked": "0.00000000"},
                {"asset": "USDT", "free": "12.5", "locked": "0"},
            ]
        }
    )
    assert fetch_free_balance(AUTH, "btc", client=client) == Decimal("0.00999")
    url = client.get.call_args.args[0]
    assert ACCOUNT_PATH in url and "signature=" in url
    assert fetch_free_balance(AUTH, "ETH", client=client) == Decimal("0")


def test_floor_to_step() -> None:
    assert floor_to_step(Decimal("0.0099876"), Decimal("0.00001")) == Decimal("0.00998")
    assert floor_to_step(0.0099876, Decimal("0.00001")) == Decimal("0.00998")
    assert floor_to_step(Decimal("83204.7129"), Decimal("0.01")) == Decimal("83204.71")
    assert floor_to_step(Decimal("1.5"), Decimal("0")) == Decimal("1.5")
    assert floor_to_step(Decimal("-1"), Decimal("0.01")) == Decimal("0")
