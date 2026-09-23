import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from jarvise.cli import app
from jarvise_exchange.models import SpotBalance

runner = CliRunner()


def test_missing_keys_exit_2(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    result = runner.invoke(app, ["exchange", "sync-balances", "--json"])
    assert result.exit_code == 2
    assert "BINANCE_API_KEY" in result.output


def test_dry_run_json_no_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    db = tmp_path / "x.db"

    def fake_list(self):
        return [SpotBalance("binance", "BTC", Decimal("1"), Decimal("0"), Decimal("1"))]

    with patch(
        "jarvise_exchange.binance_spot.BinanceSpotClient.list_spot_balances",
        fake_list,
    ):
        result = runner.invoke(
            app,
            ["exchange", "sync-balances", "--dry-run", "--json", "--db", str(db)],
        )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["inserted"] == 0
    assert not db.exists()
