"""Tests for spot balance ~USD valuation."""

from decimal import Decimal
from unittest.mock import MagicMock

import httpx

from jarvise_exchange.models import SpotBalance
from jarvise_exchange.value import (
    STABLE_ASSETS,
    fetch_usdt_price,
    underlying_asset,
    value_spot_balances,
)


def _bal(asset: str, total: str) -> SpotBalance:
    t = Decimal(total)
    return SpotBalance("binance", asset, t, Decimal("0"), t)


def test_stables_face_value() -> None:
    assert "USDT" in STABLE_ASSETS
    result = value_spot_balances(
        [_bal("USDT", "100"), _bal("USDC", "50")],
        price_fn=lambda _a: Decimal("999"),  # must not be called for stables
    )
    assert result.total_usd == Decimal("150")
    assert result.priced_count == 2
    assert result.unpriced_count == 0
    assert result.rows[0].usd == Decimal("100")
    assert result.rows[0].pricing == "stable"


def test_btc_mocked_price() -> None:
    result = value_spot_balances(
        [_bal("BTC", "0.5")],
        price_fn=lambda a: Decimal("60000") if a == "BTC" else None,
    )
    assert result.rows[0].usd == Decimal("30000")
    assert result.total_usd == Decimal("30000")
    assert result.rows[0].pricing == "ticker"


def test_missing_price_excluded_from_total() -> None:
    result = value_spot_balances(
        [_bal("BTC", "1"), _bal("WEIRD", "10"), _bal("USDT", "20")],
        price_fn=lambda a: Decimal("100") if a == "BTC" else None,
    )
    assert result.rows[1].usd is None
    assert result.rows[1].pricing is None
    assert result.unpriced_count == 1
    assert result.total_usd == Decimal("120")  # 100 + 20


def test_ldfdusd_earn_stable_face_value() -> None:
    result = value_spot_balances(
        [_bal("LDFDUSD", "0.07280273")],
        price_fn=lambda _a: None,  # no direct ticker
    )
    assert result.rows[0].pricing == "earn_stable"
    assert result.rows[0].usd == Decimal("0.07280273")
    assert result.priced_count == 1


def test_ldbtc_earn_ticker_via_underlying() -> None:
    def prices(a: str) -> Decimal | None:
        if a == "LDBTC":
            return None
        if a == "BTC":
            return Decimal("60000")
        return None

    result = value_spot_balances([_bal("LDBTC", "0.001")], price_fn=prices)
    assert result.rows[0].pricing == "earn_ticker"
    assert result.rows[0].usd == Decimal("60")


def test_ldo_direct_ticker_wins_over_earn_parse() -> None:
    """LDO is a real coin — len==3 so not parsed as Earn; direct ticker wins."""
    assert underlying_asset("LDO") is None
    assert underlying_asset("LDFDUSD") == "FDUSD"

    def prices(a: str) -> Decimal | None:
        if a == "LDO":
            return Decimal("2")
        if a == "O":
            return Decimal("999")
        return None

    result = value_spot_balances([_bal("LDO", "10")], price_fn=prices)
    assert result.rows[0].pricing == "ticker"
    assert result.rows[0].usd == Decimal("20")


def test_ethw_unpriced() -> None:
    result = value_spot_balances([_bal("ETHW", "0.009")], price_fn=lambda _a: None)
    assert result.rows[0].usd is None
    assert result.rows[0].pricing is None
    assert result.unpriced_count == 1
    assert result.total_usd == Decimal("0")


def test_fetch_usdt_price_mocked() -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"symbol": "BTCUSDT", "price": "42000.5"}
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = mock_resp
    price = fetch_usdt_price("BTC", client=client)
    assert price == Decimal("42000.5")
    assert client.get.call_count == 1
    kwargs = client.get.call_args
    assert "/api/v3/ticker/price" in kwargs.args[0]


def test_fetch_usdt_price_miss() -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = mock_resp
    assert fetch_usdt_price("NOPE", client=client) is None
