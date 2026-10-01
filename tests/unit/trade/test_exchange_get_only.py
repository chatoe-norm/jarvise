"""Regression: jarvise_exchange stays GET-only for account; no order POST."""

from pathlib import Path

from jarvise_exchange import binance_spot


def test_exchange_module_has_no_order_path() -> None:
    src = Path(binance_spot.__file__).read_text(encoding="utf-8")
    assert "/api/v3/order" not in src
    assert "ACCOUNT_PATH" in src
    assert binance_spot.ACCOUNT_PATH == "/api/v3/account"
