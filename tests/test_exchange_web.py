from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from jarvise_exchange.models import SpotBalance, SyncResult, ValuationResult, ValuedBalance
from jarvise_web.app import app

client = TestClient(app)


def test_analytics_hides_panel_without_keys(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_API_PRIVATE_KEY_PATH", raising=False)
    resp = client.get("/analytics")
    assert resp.status_code == 200
    assert "Exchange (spot)" not in resp.text


def test_analytics_shows_panel_on_successful_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "a.db"))
    bal = SpotBalance("binance", "BTC", Decimal("1"), Decimal("0"), Decimal("1"))
    fake = SyncResult(
        ok=True,
        dry_run=False,
        venue="binance",
        fetched_at_ms=1,
        balances=[bal],
        inserted=1,
        error=None,
    )
    valued = ValuationResult(
        rows=[ValuedBalance(balance=bal, usd=Decimal("60000"), price_usd=Decimal("60000"))],
        total_usd=Decimal("60000"),
        priced_count=1,
        unpriced_count=0,
    )
    with patch("jarvise_web.app.sync_spot_balances", return_value=fake):
        with patch("jarvise_web.app.BinanceSpotClient"):
            with patch("jarvise_web.app.value_spot_balances", return_value=valued):
                resp = client.get("/analytics")
    assert resp.status_code == 200
    assert "Exchange (spot)" in resp.text
    assert "BTC" in resp.text
    assert "~USD" in resp.text
    assert "60000.00" in resp.text
    assert "Total ~USD" in resp.text
    assert "BINANCE_API" not in resp.text
