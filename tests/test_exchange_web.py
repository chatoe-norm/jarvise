from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from jarvise_exchange.models import SpotBalance, SyncResult, ValuationResult, ValuedBalance
from jarvise_web.app import app

client = TestClient(app)


def test_exchange_api_hides_without_keys(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_API_PRIVATE_KEY_PATH", raising=False)
    resp = client.get("/api/exchange")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["available"] is False


def test_exchange_api_shows_on_successful_sync(monkeypatch, tmp_path):
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
        rows=[
            ValuedBalance(
                balance=bal,
                usd=Decimal("60000"),
                price_usd=Decimal("60000"),
                pricing="ticker",
            )
        ],
        total_usd=Decimal("60000"),
        priced_count=1,
        unpriced_count=0,
    )
    with patch("jarvise_web.app.sync_spot_balances", return_value=fake):
        with patch("jarvise_web.app.BinanceSpotClient"):
            with patch("jarvise_web.app.value_spot_balances", return_value=valued):
                resp = client.get("/api/exchange")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["balances"][0]["asset"] == "BTC"
    assert data["balances"][0]["usd"] == 60000.0
    assert data["balances"][0]["pricing"] == "ticker"
    assert data["total_usd"] == 60000.0
    assert data["priced_count"] == 1
    assert data["unpriced_count"] == 0


def test_exchange_api_exposes_unpriced_count(monkeypatch, tmp_path):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "b.db"))
    ethw = SpotBalance("binance", "ETHW", Decimal("0.01"), Decimal("0"), Decimal("0.01"))
    fake = SyncResult(
        ok=True,
        dry_run=False,
        venue="binance",
        fetched_at_ms=2,
        balances=[ethw],
        inserted=1,
        error=None,
    )
    valued = ValuationResult(
        rows=[ValuedBalance(balance=ethw, usd=None, price_usd=None, pricing=None)],
        total_usd=Decimal("0"),
        priced_count=0,
        unpriced_count=1,
    )
    with patch("jarvise_web.app.sync_spot_balances", return_value=fake):
        with patch("jarvise_web.app.BinanceSpotClient"):
            with patch("jarvise_web.app.value_spot_balances", return_value=valued):
                resp = client.get("/api/exchange")
    data = resp.json()
    assert data["balances"][0]["pricing"] is None
    assert data["unpriced_count"] == 1
    assert data["priced_count"] == 0
