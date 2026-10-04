"""E2E paper journey: analyze → enqueue → approve → stop exit (no live network)."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from jarvise_analyze.engine import analyze_snapshot
from jarvise_ingest.db import (
    ensure_paper_account,
    get_paper_account,
    get_paper_position,
    list_paper_orders,
    list_paper_positions,
    open_db,
    upsert_analysis_output,
    upsert_market_technicals,
)
from jarvise_paper.approval import enqueue_approval
from jarvise_paper.engine import mark_equity
from jarvise_paper.risk_monitor import run_risk_monitor
from jarvise_web.app import app


def test_paper_order_journey_stop_exit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_MARKET_SAFETY", "0")
    monkeypatch.delenv("JARVISE_LIVE_TRADING", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("WEB_BASIC_AUTH_PASSWORD", raising=False)
    monkeypatch.setattr("jarvise_web.app.redis_get_strict", lambda key: "0")
    monkeypatch.setattr("jarvise_web.app.redis_get", lambda key: None)
    monkeypatch.setattr("jarvise_web.app.redis_get_json", lambda key: {"ok": True})
    db = tmp_path / "j.db"
    monkeypatch.setenv("JARVISE_DB", str(db))
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
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1.0,
                "atr_14": 2.0,
                "rsi_14": 55.0,
                "ema_20": 101.0,
                "ema_200": 90.0,
            }
        ],
    )
    analysis = analyze_snapshot(
        {
            "symbol": "BTCUSDT",
            "timestamp": 1_700_000_000_000,
            "timeframe": "4h",
            "close": 100.0,
            "atr_14": 2.0,
            "rsi_14": 55.0,
            "ema_20": 101.0,
            "ema_200": 90.0,
        }
    )
    upsert_analysis_output(conn, analysis)
    analysis["action"] = "long"
    analysis["size_pct_equity"] = 1.5
    analysis["invalidation_price"] = 97.0
    analysis["confidence_score"] = 0.7
    row = enqueue_approval(conn, analysis=analysis, timeframe="4h")
    assert row.get("status") == "pending"
    conn.close()

    client = TestClient(app)
    resp = client.post(
        "/approvals/approve",
        data={"id": row["id"]},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("ok") is True
    assert (body.get("result") or {}).get("ok") is True

    conn = open_db(db)
    pos = get_paper_position(conn, "BTCUSDT")
    assert pos is not None
    assert pos.get("stop_price") is not None
    ids = (body.get("result") or {}).get("approval", {}).get("paper_order_ids_json")
    orders = list_paper_orders(conn)
    assert orders
    if ids:
        assert orders[0]["order_id"] in ids

    upsert_market_technicals(
        conn,
        [
            {
                "symbol": "BTCUSDT",
                "timestamp": 1_700_000_200_000,
                "timeframe": "4h",
                "open": 90.0,
                "high": 91.0,
                "low": 89.0,
                "close": 90.0,
                "volume": 1.0,
            }
        ],
    )
    run_risk_monitor(conn, now_ms=1_700_000_200_000)
    assert get_paper_position(conn, "BTCUSDT") is None
    acct = get_paper_account(conn)
    remaining = list_paper_positions(conn)
    marks = {}
    reconstructed = mark_equity(float(acct["cash"]), remaining, marks)
    assert abs(reconstructed - float(acct["equity"])) < 1e-6
    assert all(float(o["qty"]) > 0 for o in list_paper_orders(conn))
    conn.close()


def test_live_journey_mock_transport_blocks_real_host(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISE_LIVE_TRADING", "true")
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TRADE_API_SECRET", "s")
    monkeypatch.setenv("JARVISE_LIVE_EQUITY_USD", "10000")
    db = tmp_path / "live.db"
    conn = open_db(db)
    ensure_paper_account(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host not in {"api.binance.com"}:
            raise AssertionError(f"unexpected host {request.url.host}")
        if request.method == "GET" and "apiRestrictions" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "enableWithdrawals": False,
                    "enableInternalTransfer": False,
                    "permitsUniversalTransfer": False,
                    "enableSpotAndMarginTrading": True,
                },
            )
        if request.method == "GET":
            return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."})
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "orderId": 42,
                    "status": "FILLED",
                    "executedQty": "0.001",
                    "cummulativeQuoteQty": "100",
                    "fills": [{"price": "100000", "qty": "0.001"}],
                },
            )
        raise AssertionError(request.method)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    from jarvise_trade.submit import submit_live_for_approval

    blocked = submit_live_for_approval(
        conn,
        approval={"id": "n1", "symbol": "BTCUSDT", "action": "long", "size_pct_equity": 1.0},
        now_ms=1_000,
        http_client=client,
    )
    assert blocked["ok"] is False
    assert blocked["error"] == "missing_invalidation"

    filled = submit_live_for_approval(
        conn,
        approval={
            "id": "n2",
            "symbol": "BTCUSDT",
            "action": "long",
            "size_pct_equity": 1.0,
            "invalidation_price": 97.0,
        },
        now_ms=2_000,
        http_client=client,
    )
    assert filled["ok"] is True
    assert filled["live_order"]["status"] == "filled"
    conn.close()
    client.close()
