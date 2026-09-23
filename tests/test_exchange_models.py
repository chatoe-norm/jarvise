from decimal import Decimal

from jarvise_exchange.models import SpotBalance, SyncResult


def test_spot_balance_total_is_decimal():
    b = SpotBalance(
        venue="binance",
        asset="BTC",
        free=Decimal("1.5"),
        locked=Decimal("0.5"),
        total=Decimal("2.0"),
    )
    assert b.total == Decimal("2.0")
    assert isinstance(b.free, Decimal)


def test_sync_result_defaults():
    r = SyncResult(
        ok=True,
        dry_run=False,
        venue="binance",
        fetched_at_ms=1,
        balances=[],
        inserted=0,
        error=None,
    )
    assert r.ok is True
    assert r.error is None
