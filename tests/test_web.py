from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jarvise_ingest.db import (
    ensure_paper_account,
    open_db,
    upsert_analysis_output,
)
from jarvise_paper.engine import apply_signal
from jarvise_web.app import app


def test_healthz() -> None:
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["paper_only"] is True


def _stub_control_deps(monkeypatch) -> None:
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)

    def fake_redis_get(key: str):
        return "0"

    def fake_json(key: str):
        return {"ok": True}

    def fake_qdrant():
        return {"exists": True, "points": 0}

    monkeypatch.setattr("jarvise_web.app.redis_get", fake_redis_get)
    monkeypatch.setattr("jarvise_web.app.redis_get_json", fake_json)
    monkeypatch.setattr("jarvise_web.app.qdrant_info", fake_qdrant)


def test_dashboard_renders_without_auth(monkeypatch) -> None:
    _stub_control_deps(monkeypatch)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"PAPER ONLY" in resp.content
    assert b'href="/analytics"' in resp.content


def test_analytics_empty_db(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("JARVISE_DB", str(missing))
    client = TestClient(app)
    resp = client.get("/analytics")
    assert resp.status_code == 200
    assert b"Database not found" in resp.content or b"No analysis rows" in resp.content
    assert b"PAPER ONLY" in resp.content
    assert b'href="/"' in resp.content


def test_analytics_and_api_with_rows(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "x1",
            "timestamp": 1_700_000_000_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "regime_state": "trend_up",
            "confidence_score": 0.72,
            "action": "long",
            "invalidation_price": 90.0,
            "size_pct_equity": 1.1,
            "thesis": "paper long",
        },
    )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))

    client = TestClient(app)
    html = client.get("/analytics")
    assert html.status_code == 200
    assert b"BTCUSDT" in html.content
    assert b"trend_up" in html.content
    assert b"paper long" in html.content

    filtered = client.get("/analytics", params={"symbol": "ETHUSDT"})
    assert filtered.status_code == 200
    assert b"No analysis rows" in filtered.content

    api = client.get("/api/analysis", params={"symbol": "BTCUSDT", "timeframe": "4h"})
    assert api.status_code == 200
    payload = api.json()
    assert payload["ok"] is True
    assert payload["paper_only"] is True
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["action"] == "long"
    assert payload["rows"][0]["timeframe"] == "4h"


def test_api_paper_ledger(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    ensure_paper_account(conn)
    apply_signal(
        conn,
        analysis={
            "analysis_id": "p1",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 5.0,
            "regime_state": "trend_up",
            "confidence_score": 0.7,
        },
        mid_price=100.0,
        timeframe="4h",
        now_ms=1_700_000_000_000,
    )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))

    client = TestClient(app)
    html = client.get("/analytics")
    assert html.status_code == 200
    assert b"Paper ledger" in html.content

    api = client.get("/api/paper")
    assert api.status_code == 200
    payload = api.json()
    assert payload["ok"] is True
    assert payload["paper_only"] is True
    assert payload["equity"] is not None
    assert any(p["symbol"] == "BTCUSDT" for p in payload["positions"])
