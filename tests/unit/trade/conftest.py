"""Avoid venue STOP_LOSS_LIMIT / exchangeInfo / account HTTP in unit trade tests."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from jarvise_trade.binance_market import SymbolFilters


def permissive_filters(symbol: str = "BTCUSDT", base_asset: str = "BTC") -> SymbolFilters:
    return SymbolFilters(
        symbol=symbol,
        base_asset=base_asset,
        step_size=Decimal("0.00000001"),
        min_qty=Decimal("0"),
        min_notional=Decimal("0"),
        tick_size=Decimal("0.01"),
    )


@pytest.fixture(autouse=True)
def _mute_protective_stop() -> Iterator[MagicMock]:
    with patch(
        "jarvise_trade.submit.place_spot_stop_loss_limit",
        return_value={
            "orderId": 1,
            "status": "NEW",
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "fills": [],
        },
    ) as mocked:
        yield mocked


def _filters_for(symbol: str, **_: object) -> SymbolFilters:
    sym = symbol.upper()
    return permissive_filters(sym, sym.removesuffix("USDT"))


@pytest.fixture(autouse=True)
def venue_sizing() -> Iterator[dict[str, MagicMock]]:
    """Unconstrained filters + ample free balance; tests override to tighten."""
    with (
        patch("jarvise_trade.submit.fetch_symbol_filters", side_effect=_filters_for) as filters,
        patch("jarvise_trade.submit.fetch_free_balance", return_value=Decimal("1000000")) as free,
    ):
        yield {"filters": filters, "free": free}
