from decimal import Decimal

from jarvise_exchange.binance_spot import balances_from_account_payload


def test_balances_from_account_filters_zeros():
    payload = {
        "balances": [
            {"asset": "BTC", "free": "0.10000000", "locked": "0.00000000"},
            {"asset": "ETH", "free": "0.00000000", "locked": "0.00000000"},
            {"asset": "USDT", "free": "10.5", "locked": "1.5"},
        ]
    }
    rows = balances_from_account_payload(payload)
    assets = {b.asset: b for b in rows}
    assert set(assets) == {"BTC", "USDT"}
    assert assets["USDT"].total == Decimal("12.0")
    assert assets["BTC"].venue == "binance"
