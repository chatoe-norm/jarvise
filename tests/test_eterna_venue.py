"""T2.5 Eterna VenueClient stub + registry."""

from pathlib import Path

import pytest

from jarvise_exchange.eterna_spot import EternaReadApiBlocked, EternaSpotClient
from jarvise_exchange.registry import default_venue, resolve_venue_client

FIXTURE = Path(__file__).parent / "fixtures" / "eterna_spot_balances.json"


def test_eterna_fixture_balances() -> None:
    client = EternaSpotClient(fixture_path=FIXTURE)
    rows = client.list_spot_balances()
    assert len(rows) == 2
    assert rows[0].venue == "eterna"
    assert rows[0].asset == "USDT"
    assert str(rows[0].total) == "100.5"


def test_eterna_blocked_without_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ETERNA_SPOT_FIXTURE", raising=False)
    client = EternaSpotClient()
    with pytest.raises(EternaReadApiBlocked):
        client.list_spot_balances()


def test_registry_eterna(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETERNA_SPOT_FIXTURE", str(FIXTURE))
    client = resolve_venue_client("eterna")
    assert client.list_spot_balances()[0].asset == "USDT"


def test_default_venue_binance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_EXCHANGE_VENUE", raising=False)
    assert default_venue() == "binance"
