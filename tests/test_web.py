from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jarvise_ingest.db import (
    ensure_paper_account,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
    upsert_pending_approval,
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


def test_dashboard_serves_spa(monkeypatch) -> None:
    _stub_control_deps(monkeypatch)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Jarvise" in resp.content


def test_analytics_redirects_to_spa_shell(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("JARVISE_DB", str(missing))
    client = TestClient(app)
    resp = client.get("/analytics")
    assert resp.status_code == 200
    assert b"Jarvise" in resp.content


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
    api = client.get("/api/analysis", params={"symbol": "BTCUSDT", "timeframe": "4h"})
    assert api.status_code == 200
    payload = api.json()
    assert payload["ok"] is True
    assert payload["paper_only"] is True
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["action"] == "long"
    assert payload["rows"][0]["timeframe"] == "4h"

    filtered = client.get("/api/analysis", params={"symbol": "ETHUSDT"})
    assert filtered.status_code == 200
    assert filtered.json()["ok"] is True
    assert filtered.json()["rows"] == []


def test_parse_status_payload_json_and_legacy_text() -> None:
    from jarvise_web.app import format_status_pre, parse_status_payload

    assert parse_status_payload('{"ok": true, "chunks": 3}') == {"ok": True, "chunks": 3}
    legacy = "ok: True\npaper_only: True\nqdrant_url: http://qdrant:6333\nchunks: 4089\n"
    parsed = parse_status_payload(legacy)
    assert parsed["ok"] is True
    assert parsed["paper_only"] is True
    assert parsed["chunks"] == 4089
    assert parsed["qdrant_url"] == "http://qdrant:6333"
    pretty = format_status_pre(parsed)
    assert '"chunks": 4089' in pretty


def test_api_status_includes_paper_keys(monkeypatch) -> None:
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)

    def fake_json(key: str):
        return {"ok": True, "key": key}

    monkeypatch.setattr("jarvise_web.app.redis_get_json", fake_json)
    monkeypatch.setattr("jarvise_web.app.redis_get", lambda k: "0")
    monkeypatch.setattr("jarvise_web.app.qdrant_info", lambda: {"exists": True, "points": 0})
    client = TestClient(app)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "paper" in data
    assert "paper_expire" in data
    assert "risk_caps" in data
    assert "max_notional_per_order" in data["risk_caps"]


def test_api_dashboard_bundle(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    ensure_paper_account(conn)
    upsert_pending_approval(
        conn,
        {
            "id": "appr-dash-1",
            "created_at_ms": 1_000,
            "expires_at_ms": 3_600_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "a1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.7,
            "size_pct_equity": 5.0,
            "status": "pending",
        },
    )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["pending_count"] == 1
    assert data["approvals"][0]["symbol"] == "BTCUSDT"
    assert "status" in data
    assert "paper" in data
    assert "metrics" in data


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
    api = client.get("/api/paper")
    assert api.status_code == 200
    payload = api.json()
    assert payload["ok"] is True
    assert payload["paper_only"] is True
    assert payload["equity"] is not None
    assert any(p["symbol"] == "BTCUSDT" for p in payload["positions"])


def test_api_paper_metrics_and_persist_json(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    ensure_paper_account(conn)
    apply_signal(
        conn,
        analysis={
            "analysis_id": "m1",
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
    apply_signal(
        conn,
        analysis={
            "analysis_id": "m2",
            "symbol": "BTCUSDT",
            "action": "flat",
            "size_pct_equity": 0.0,
            "regime_state": "range",
            "confidence_score": 0.4,
        },
        mid_price=110.0,
        timeframe="4h",
        now_ms=1_700_000_100_000,
    )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))

    client = TestClient(app)
    api = client.get("/api/paper/metrics")
    assert api.status_code == 200
    payload = api.json()
    assert payload["ok"] is True
    assert payload["closed_trades"] == 1
    assert payload["expected_value_ev"] is not None

    persist = client.post(
        "/paper/metrics/persist",
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert persist.status_code == 200
    assert persist.json()["ok"] is True

    legacy = client.post("/paper/metrics/persist", follow_redirects=False)
    assert legacy.status_code == 303


def test_api_approvals_pending(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    ensure_paper_account(conn)
    upsert_pending_approval(
        conn,
        {
            "id": "appr-web-1",
            "created_at_ms": 1_000,
            "expires_at_ms": 3_600_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "a1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.7,
            "size_pct_equity": 5.0,
            "status": "pending",
        },
    )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.get("/api/approvals")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["expires_at_ms"] == 3_600_000


def test_post_approve_json_and_redirect(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    ensure_paper_account(conn)
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timestamp": 1_700_000_000_000,
                "timeframe": "4h",
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1.0,
            }
        ],
    )
    row = upsert_pending_approval(
        conn,
        {
            "id": "appr-web-approve",
            "created_at_ms": 1_000,
            "expires_at_ms": 3_600_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "a1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.7,
            "size_pct_equity": 5.0,
            "status": "pending",
        },
    )
    approval_id = row["id"]
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.post(
        "/approvals/approve",
        data={"id": approval_id},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers.get("location") == "/"
