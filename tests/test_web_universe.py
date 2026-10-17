"""Test universe and suitability endpoints on Jarvise web API."""

import pytest
from fastapi.testclient import TestClient

from jarvise_ingest.db import open_db
from jarvise_ingest.universe import seed_named_universe
from jarvise_web.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_universe.db"
    conn = open_db(db_file)
    seed_named_universe(conn, "paper_liquid")
    conn.close()

    monkeypatch.setenv("JARVISE_DB", str(db_file))
    monkeypatch.setenv("JARVISE_PAPER_UNIVERSE", "paper_liquid")
    return TestClient(app)


def test_api_universe_returns_symbols_and_suitability(client):
    resp = client.get("/api/universe")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["universe_id"] == "paper_liquid"
    assert "BTCUSDT" in data["active_symbols"]
    assert "ETHUSDT" in data["active_symbols"]
    assert "SOLUSDT" in data["active_symbols"]
    assert "BNBUSDT" in data["active_symbols"]
    assert len(data["items"]) >= 4

    sol = next(i for i in data["items"] if i["symbol"] == "SOLUSDT")
    assert "score" in sol
    assert "grade" in sol
    assert "reasons" in sol
    assert sol["is_active"] is True


def test_api_universe_add_and_remove(client):
    # Add ADAUSDT
    resp = client.post(
        "/api/universe/add",
        data={"symbol": "ADAUSDT", "asset_class": "crypto"},
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "ADAUSDT" in data["active_symbols"]

    # Verify via GET
    get_resp = client.get("/api/universe")
    assert "ADAUSDT" in get_resp.json()["active_symbols"]

    # Remove ADAUSDT
    rem_resp = client.post(
        "/api/universe/remove",
        data={"symbol": "ADAUSDT"},
        headers={"accept": "application/json"},
    )
    assert rem_resp.status_code == 200
    assert "ADAUSDT" not in rem_resp.json()["active_symbols"]


def test_api_universe_removed_preset_symbol_stays_removed(client):
    """GET /api/universe re-seeds the preset; a removed member must not come back."""
    rem = client.post(
        "/api/universe/remove", data={"symbol": "BNBUSDT"}, headers={"accept": "application/json"}
    )
    assert "BNBUSDT" not in rem.json()["active_symbols"]
    again = client.get("/api/universe").json()
    assert "BNBUSDT" not in again["active_symbols"]


def test_api_universe_reset(client):
    resp = client.post(
        "/api/universe/reset",
        data={"preset": "paper_core"},
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # The active universe (paper_liquid) is reset, not the preset's own universe id.
    assert body["universe_id"] == "paper_liquid"
    assert set(body["active_symbols"]) == {"BTCUSDT", "ETHUSDT"}
    assert sorted(body["removed"]) == ["BNBUSDT", "SOLUSDT"]
    assert set(client.get("/api/universe").json()["active_symbols"]) == {"BTCUSDT", "ETHUSDT"}
