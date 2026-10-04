from jarvise_trade.submit import live_preflight


def test_live_preflight_requires_stop() -> None:
    err = live_preflight(
        None,
        {"action": "long", "size_pct_equity": 1.0},
    )
    assert err == "missing_invalidation"
    assert live_preflight(None, {"action": "long", "invalidation_price": 97.0}) is None
