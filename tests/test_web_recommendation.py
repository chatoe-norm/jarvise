from pathlib import Path

from fastapi.testclient import TestClient

from jarvise_ingest.db import (
    ensure_paper_account,
    insert_llm_review,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
    upsert_pending_approval,
    write_indicators,
)
from jarvise_web.app import app


def _stub(monkeypatch, *, doctrine=None) -> None:
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)
    monkeypatch.setattr("jarvise_web.app.redis_get", lambda key: "0")
    monkeypatch.setattr("jarvise_web.app.redis_get_json", lambda key: {"ok": True, "key": key})
    monkeypatch.setattr("jarvise_web.app.qdrant_info", lambda: {"exists": True, "points": 0})
    monkeypatch.setattr(
        "jarvise_web.app.fetch_doctrine", lambda query, limit=3: list(doctrine or [])
    )


def _seed(db: Path) -> None:
    conn = open_db(db)
    ensure_paper_account(conn)
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timestamp": 1_700_000_000_000,
                "timeframe": "4h",
                "open": 85000.0,
                "high": 85500.0,
                "low": 84500.0,
                "close": 85000.0,
                "volume": 1.0,
            }
        ],
    )
    write_indicators(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "timestamp": 1_700_000_000_000,
                "atr_14": 1000.0,
                "rsi_14": 65.0,
                "ema_20": 84000.0,
                "ema_200": 80000.0,
            }
        ],
    )
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "an-1",
            "timestamp": 1_700_000_000_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "regime_state": "trend_up",
            "confidence_score": 0.75,
            "action": "long",
            "invalidation_price": 83500.0,
            "size_pct_equity": 1.125,
            "thesis": "trend_up",
        },
    )
    upsert_pending_approval(
        conn,
        {
            "id": "ap-1",
            "created_at_ms": 1_000,
            "expires_at_ms": 9_999_999_999_999,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "an-1",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.75,
            "size_pct_equity": 1.125,
            "status": "pending",
        },
    )
    conn.close()


def test_recommendation_200_template(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch, doctrine=["structure first"])
    db = tmp_path / "w.db"
    _seed(db)
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.get("/api/approvals/ap-1/recommendation")
    assert resp.status_code == 200
    card = resp.json()
    assert card["ok"] is True
    assert card["recommendation"] == "approve"
    assert card["recommendation_source"] == "template"
    assert card["doctrine"] == ["structure first"]
    assert card["risk"]["notional_usd"] == 112.5
    assert card["claude"] is None


def test_recommendation_claude_source(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch)
    db = tmp_path / "w2.db"
    _seed(db)
    conn = open_db(db)
    insert_llm_review(
        conn,
        {
            "approval_id": "ap-1",
            "model": "anthropic/claude-sonnet-4.5",
            "decision": "approve",
            "reason": "แนวโน้มชัด",
            "brief_hash": "h",
            "created_at_ms": 2_000,
        },
    )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    card = client.get("/api/approvals/ap-1/recommendation").json()
    assert card["recommendation_source"] == "claude"
    assert card["claude"] == {
        "decision": "approve",
        "reason": "แนวโน้มชัด",
        "model": "anthropic/claude-sonnet-4.5",
        "at_ms": 2_000,
    }


def test_recommendation_404(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch)
    db = tmp_path / "w3.db"
    _seed(db)
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    assert client.get("/api/approvals/nope/recommendation").status_code == 404


def test_status_exposes_paper_auto_and_ingest_health(monkeypatch, tmp_path: Path) -> None:
    _stub(monkeypatch)
    monkeypatch.setenv("JARVISE_DB", str(tmp_path / "missing.db"))
    client = TestClient(app)
    payload = client.get("/api/status").json()
    assert payload["paper_auto"] == {"ok": True, "key": "jarvise:paper_auto:last"}
    assert payload["ingest_health"] == {"ok": True, "key": "jarvise:ingest:health"}
