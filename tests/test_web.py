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
    # Unauthenticated probe must not advertise live-trading state.
    assert "live_trading" not in resp.json()


def test_assets_require_basic_auth_when_configured(monkeypatch) -> None:
    import base64

    from jarvise_web import app as web_app

    monkeypatch.setenv("WEB_BASIC_AUTH_USER", "owner")
    monkeypatch.setenv("WEB_BASIC_AUTH_PASSWORD", "pw")
    static = web_app.static_dir()
    if static is None or not (static / "assets").is_dir():
        import pytest

        pytest.skip("no built SPA assets in this checkout")
    client = TestClient(app)
    anon = client.get("/assets/does-not-exist.js")
    assert anon.status_code == 401
    token = base64.b64encode(b"owner:pw").decode()
    authed = client.get("/assets/does-not-exist.js", headers={"Authorization": f"Basic {token}"})
    assert authed.status_code == 404  # authenticated, then normal static 404


def test_basic_header_check() -> None:
    import base64

    from jarvise_web.app import _basic_header_ok

    good = "Basic " + base64.b64encode(b"u:p").decode()
    assert _basic_header_ok(good, "u", "p") is True
    assert _basic_header_ok(good, "u", "x") is False
    assert _basic_header_ok("Bearer abc", "u", "p") is False
    assert _basic_header_ok("Basic ???", "u", "p") is False
    assert _basic_header_ok(None, "u", "p") is False


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
    monkeypatch.setattr("jarvise_web.app.redis_get_strict", fake_redis_get)
    monkeypatch.setattr("jarvise_web.app.redis_get_json", fake_json)
    monkeypatch.setattr("jarvise_web.app.qdrant_info", fake_qdrant)


def test_kill_switch_unreadable_blocks_approve_and_reports_unknown(monkeypatch, tmp_path: Path) -> None:
    """Redis down must never read as 'kill-switch off'."""
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)

    def boom(key: str):
        raise ConnectionError("redis down")

    monkeypatch.setattr("jarvise_web.app.redis_get_strict", boom)
    monkeypatch.setattr("jarvise_web.app.redis_get_json", lambda key: None)
    monkeypatch.setattr("jarvise_web.app.qdrant_info", lambda: {"exists": False, "points": 0})
    db = tmp_path / "ks.db"
    conn = open_db(db)
    ensure_paper_account(conn)
    upsert_pending_approval(
        conn,
        {
            "id": "ks1",
            "created_at_ms": 1,
            "expires_at_ms": 10**15,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": None,
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.7,
            "invalidation_price": 97.0,
            "size_pct_equity": 1.0,
        },
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)

    status = client.get("/api/status").json()
    assert status["kill_switch"] is True
    assert status["kill_switch_state"]["known"] is False

    resp = client.post("/approvals/approve", data={"id": "ks1"}, headers={"accept": "application/json"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False and "unreadable" in body["error"]
    # Nothing was filled.
    conn = open_db(db)
    try:
        from jarvise_ingest.db import get_approval, list_paper_orders

        assert get_approval(conn, "ks1")["status"] == "pending"
        assert list_paper_orders(conn) == []
    finally:
        conn.close()


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
    assert payload["total"] == 1
    assert payload["limit"] == 10
    assert payload["offset"] == 0
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["action"] == "long"
    assert payload["rows"][0]["timeframe"] == "4h"

    filtered = client.get("/api/analysis", params={"symbol": "ETHUSDT"})
    assert filtered.status_code == 200
    assert filtered.json()["ok"] is True
    assert filtered.json()["rows"] == []
    assert filtered.json()["total"] == 0


def test_api_analysis_pagination(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    for i in range(25):
        upsert_analysis_output(
            conn,
            {
                "analysis_id": f"p{i}",
                "timestamp": 1_700_000_000_000 + i,
                "symbol": "BTCUSDT",
                "timeframe": "4h",
                "regime_state": "trend_up",
                "confidence_score": 0.5,
                "action": "long",
                "invalidation_price": 90.0,
                "size_pct_equity": 1.0,
                "thesis": f"row {i}",
            },
        )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)

    page1 = client.get("/api/analysis", params={"limit": 10, "offset": 0})
    assert page1.status_code == 200
    body1 = page1.json()
    assert body1["ok"] is True
    assert body1["total"] == 25
    assert body1["limit"] == 10
    assert body1["offset"] == 0
    assert len(body1["rows"]) == 10
    assert body1["rows"][0]["analysis_id"] == "p24"

    page2 = client.get("/api/analysis", params={"limit": 10, "offset": 10})
    body2 = page2.json()
    assert len(body2["rows"]) == 10
    assert body2["offset"] == 10
    assert body2["rows"][0]["analysis_id"] == "p14"

    default = client.get("/api/analysis")
    assert default.json()["limit"] == 10
    assert len(default.json()["rows"]) == 10


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
    monkeypatch.setattr("jarvise_web.app.redis_get_strict", lambda k: "0")
    monkeypatch.setattr("jarvise_web.app.qdrant_info", lambda: {"exists": True, "points": 0})
    client = TestClient(app)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "paper" in data
    assert "paper_expire" in data
    assert "risk_caps" in data
    assert "max_notional_per_order" in data["risk_caps"]
    assert data["kill_switch"] is False
    assert data["kill_switch_state"] == {
        "engaged": False,
        "known": True,
        "reason": None,
        "error": None,
    }


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
            "invalidation_price": 97.0,
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
            "invalidation_price": 97.0,
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
            "invalidation_price": 97.0,
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
            "invalidation_price": 97.0,
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
            "invalidation_price": 97.0,
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
            "invalidation_price": 97.0,
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


def test_api_ohlcv_404_unknown_id(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    ensure_paper_account(conn)
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.get("/api/approvals/missing/ohlcv")
    assert resp.status_code == 404


def test_api_ohlcv_empty_bars_when_no_technicals(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    ensure_paper_account(conn)
    upsert_pending_approval(
        conn,
        {
            "id": "ohlcv-empty",
            "created_at_ms": 1_000,
            "expires_at_ms": 3_600_000,
            "symbol": "ETHUSDT",
            "timeframe": "4h",
            "analysis_id": None,
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.7,
            "invalidation_price": 97.0,
            "size_pct_equity": 1.0,
        },
    )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.get("/api/approvals/ohlcv-empty/ohlcv")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["symbol"] == "ETHUSDT"
    assert body["timeframe"] == "4h"
    assert body["bars"] == []
    assert body["invalidation_price"] is None
    assert body["action"] == "long"


def test_api_ohlcv_seeded_closed_bars_in_order(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    ensure_paper_account(conn)
    candles = []
    for i in range(50):
        ts = 1_700_000_000_000 + i * 14_400_000
        candles.append(
            {
                "symbol": "BTCUSDT",
                "timestamp": ts,
                "timeframe": "4h",
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "volume": 1.0,
            }
        )
    upsert_market_technicals(conn, candles)
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "an-ohlcv",
            "timestamp": 1_700_000_000_000 + 49 * 14_400_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "regime_state": "trend_up",
            "confidence_score": 0.75,
            "action": "long",
            "invalidation_price": 90.5,
            "size_pct_equity": 1.0,
            "thesis": "seed",
        },
    )
    upsert_pending_approval(
        conn,
        {
            "id": "ohlcv-full",
            "created_at_ms": 1_000,
            "expires_at_ms": 3_600_000,
            "symbol": "BTCUSDT",
            "timeframe": "4h",
            "analysis_id": "an-ohlcv",
            "action": "long",
            "regime_state": "trend_up",
            "confidence_score": 0.75,
            "invalidation_price": 97.0,
            "size_pct_equity": 1.0,
        },
    )
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.get("/api/approvals/ohlcv-full/ohlcv")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "BTCUSDT"
    assert body["timeframe"] == "4h"
    assert len(body["bars"]) == 48
    stamps = [b["t"] for b in body["bars"]]
    assert stamps == sorted(stamps)
    assert stamps[-1] == 1_700_000_000_000 + 49 * 14_400_000
    last = body["bars"][-1]
    assert last["o"] == 149.0 and last["c"] == 149.5
    assert body["invalidation_price"] == 90.5
    assert body["action"] == "long"


def test_api_analysis_explain_404(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    open_db(db).close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.get("/api/analysis/missing-id/explain")
    assert resp.status_code == 404


def test_api_analysis_explain_ladder(monkeypatch, tmp_path: Path) -> None:
    _stub_control_deps(monkeypatch)
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    ts = 1_700_000_000_000
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "ETHUSDT",
                "timestamp": ts,
                "timeframe": "4h",
                "open": 2680.0,
                "high": 2700.0,
                "low": 2670.0,
                "close": 2687.0,
                "volume": 1.0,
            }
        ],
    )
    conn.execute(
        """
        UPDATE market_technicals
        SET atr_14=?, rsi_14=?, ema_20=?, ema_200=?
        WHERE symbol=? AND timeframe=? AND timestamp=?
        """,
        (40.0, 49.0, 2700.0, 2560.0, "ETHUSDT", "4h", ts),
    )
    upsert_analysis_output(
        conn,
        {
            "analysis_id": "an-explain",
            "timestamp": ts,
            "symbol": "ETHUSDT",
            "timeframe": "4h",
            "regime_state": "trend_up",
            "confidence_score": 0.65,
            "action": "long",
            "invalidation_price": 2600.0,
            "size_pct_equity": 0.975,
            "thesis": "Trend up pullback",
        },
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("JARVISE_DB", str(db))
    client = TestClient(app)
    resp = client.get("/api/analysis/an-explain/explain")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["analysis_id"] == "an-explain"
    assert body["symbol"] == "ETHUSDT"
    assert body["candle"]["close"] == 2687.0
    assert body["breakdown"]["total"] == 0.65
    assert body["breakdown"]["regime"] == "trend_up"
    assert body["breakdown"]["gates"]["flat_below"] == 0.55
    assert body["breakdown"]["gates"]["doctrine_free_approve"] == 0.70
    assert any(s["delta"] == -0.1 for s in body["breakdown"]["steps"])
