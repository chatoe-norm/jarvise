from decimal import Decimal
from pathlib import Path

from jarvise_exchange.models import SpotBalance
from jarvise_exchange.sync import sync_spot_balances


class FakeClient:
    def list_spot_balances(self):
        return [
            SpotBalance("binance", "BTC", Decimal("1"), Decimal("0"), Decimal("1")),
            SpotBalance("binance", "DUST", Decimal("0"), Decimal("0"), Decimal("0")),
        ]


def test_sync_persists_and_filters(tmp_path: Path):
    db = tmp_path / "t.db"
    result = sync_spot_balances(client=FakeClient(), db_path=db, dry_run=False)
    assert result.ok
    assert result.inserted == 1
    assert len(result.balances) == 1
    assert result.balances[0].asset == "BTC"


def test_sync_dry_run_writes_nothing(tmp_path: Path):
    db = tmp_path / "t.db"
    result = sync_spot_balances(client=FakeClient(), db_path=db, dry_run=True)
    assert result.ok and result.dry_run
    assert result.inserted == 0
    assert not db.exists()
