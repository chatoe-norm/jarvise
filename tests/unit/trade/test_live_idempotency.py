"""Live submit idempotency + reconciliation (mocked venue, no network)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from jarvise_exchange.binance_spot import BinanceAuth
from jarvise_ingest.db import (
    ensure_paper_account,
    get_live_order_by_client_id,
    insert_live_order,
    list_live_orders_open,
    open_db,
)
from jarvise_trade.binance_market import (
    ALLOWED_TRADE_CALLS,
    ORDER_PATH,
    client_order_id_for_approval,
    lifecycle_status,
    place_spot_market_order,
    query_order,
    summarize_fill,
)
from jarvise_trade.reconcile import reconcile_live_orders
from jarvise_trade.submit import submit_live_for_approval

SAFE_PERMS = {
    "enableWithdrawals": False,
    "enableInternalTransfer": False,
    "permitsUniversalTransfer": False,
    "enableSpotAndMarginTrading": True,
}


def _approval(aid: str = "abc123def456") -> dict:
    return {"id": aid, "symbol": "BTCUSDT", "action": "long", "size_pct_equity": 5.0}


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_TRADE_API_KEY", "tk")
    monkeypatch.setenv("BINANCE_TRADE_API_SECRET", "ts")
    monkeypatch.setenv("JARVISE_LIVE_EQUITY_USD", "10000")


def test_client_order_id_is_deterministic_and_bounded() -> None:
    cid = client_order_id_for_approval("fe6721cc38408d7c")
    assert cid == "jrv-fe6721cc38408d7c"
    assert client_order_id_for_approval("fe6721cc38408d7c") == cid
    long_id = "x" * 80
    assert len(client_order_id_for_approval(long_id)) == 36
    with pytest.raises(ValueError):
        client_order_id_for_approval("!!!")


def test_allowlist_has_get_and_post_order_only() -> None:
    assert ALLOWED_TRADE_CALLS == frozenset({("POST", ORDER_PATH), ("GET", ORDER_PATH)})


def test_place_order_sends_client_order_id_and_full_resp() -> None:
    auth = BinanceAuth(api_key="k", hmac_secret="s")
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"orderId": 1, "status": "FILLED"}
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = resp
    place_spot_market_order(
        auth,
        symbol="BTCUSDT",
        side="BUY",
        quote_order_qty=50.0,
        client=client,
        timestamp_ms=1,
        new_client_order_id="jrv-abc",
    )
    url = client.post.call_args.args[0]
    assert "newClientOrderId=jrv-abc" in url
    assert "newOrderRespType=FULL" in url


def test_query_order_returns_none_on_not_found() -> None:
    auth = BinanceAuth(api_key="k", hmac_secret="s")
    resp = MagicMock()
    resp.status_code = 400
    resp.json.return_value = {"code": -2013, "msg": "Order does not exist."}
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = resp
    assert (
        query_order(auth, symbol="BTCUSDT", orig_client_order_id="jrv-x", client=client, timestamp_ms=1)
        is None
    )
    url = client.get.call_args.args[0]
    assert ORDER_PATH in url and "origClientOrderId=jrv-x" in url
    assert client.post.call_count == 0


def test_summarize_fill_and_lifecycle() -> None:
    fill = summarize_fill(
        {
            "orderId": 9,
            "clientOrderId": "jrv-a",
            "status": "PARTIALLY_FILLED",
            "executedQty": "0.1",
            "cummulativeQuoteQty": "10",
            "fills": [{}, {}],
        }
    )
    assert fill["status"] == "partially_filled"
    assert fill["avg_price"] == 100.0
    assert fill["fills_count"] == 2
    assert lifecycle_status("FILLED") == "filled"
    assert lifecycle_status("NEW") == "submitted"
    assert lifecycle_status("CANCELED", executed_qty=0.0) == "canceled"
    # A canceled order that executed something is still a partial fill, never flat.
    assert lifecycle_status("EXPIRED", executed_qty=0.01) == "partially_filled"


def test_duplicate_submit_returns_existing_without_post(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    approval = _approval()
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", return_value=None),
        patch(
            "jarvise_trade.submit.place_spot_market_order",
            return_value={
                "orderId": 1,
                "status": "FILLED",
                "executedQty": "0.005",
                "cummulativeQuoteQty": "500",
            },
        ) as place,
    ):
        first = submit_live_for_approval(conn, approval=approval, now_ms=1_000)
        second = submit_live_for_approval(conn, approval=approval, now_ms=2_000)
    assert first["ok"] and first["live_order"]["status"] == "filled"
    assert second["ok"] and second.get("duplicate") is True
    assert second["live_order"]["id"] == first["live_order"]["id"]
    assert place.call_count == 1


def test_venue_already_has_order_skips_post(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    prior = {
        "orderId": 55,
        "clientOrderId": "jrv-abc123def456",
        "status": "FILLED",
        "executedQty": "0.004",
        "cummulativeQuoteQty": "400",
    }
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", return_value=prior),
        patch("jarvise_trade.submit.place_spot_market_order") as place,
    ):
        result = submit_live_for_approval(conn, approval=_approval(), now_ms=1_000)
    assert result["ok"] and result.get("duplicate") is True
    assert result["live_order"]["status"] == "filled"
    assert result["live_order"]["venue_order_id"] == "55"
    assert "recovered" in (result["live_order"]["error"] or "")
    assert place.call_count == 0


def test_timeout_then_recovery_does_not_double_order(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    recovered = {
        "orderId": 77,
        "clientOrderId": "jrv-abc123def456",
        "status": "FILLED",
        "executedQty": "0.005",
        "cummulativeQuoteQty": "500",
    }
    queries = [None, recovered]  # pre-submit: absent; post-timeout: present
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", side_effect=lambda *a, **k: queries.pop(0)),
        patch(
            "jarvise_trade.submit.place_spot_market_order", side_effect=httpx.ReadTimeout("timeout")
        ) as place,
    ):
        result = submit_live_for_approval(conn, approval=_approval(), now_ms=1_000)
    assert place.call_count == 1
    assert result["ok"] is True and result.get("recovered") is True
    assert result["live_order"]["status"] == "filled"
    assert result["live_order"]["venue_order_id"] == "77"
    # A retry now short-circuits on our own ledger.
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.place_spot_market_order") as place2,
        patch("jarvise_trade.submit.query_order") as q2,
    ):
        again = submit_live_for_approval(conn, approval=_approval(), now_ms=3_000)
    assert again.get("duplicate") is True and place2.call_count == 0 and q2.call_count == 0


def test_timeout_without_venue_order_records_error(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", return_value=None),
        patch("jarvise_trade.submit.place_spot_market_order", side_effect=httpx.ReadTimeout("timeout")),
    ):
        result = submit_live_for_approval(conn, approval=_approval(), now_ms=1_000)
    assert result["ok"] is False
    assert result["live_order"]["status"] == "error"
    assert result["live_order"]["client_order_id"] == "jrv-abc123def456"


def test_pre_submit_query_failure_blocks_post(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", side_effect=httpx.ConnectError("down")),
        patch("jarvise_trade.submit.place_spot_market_order") as place,
    ):
        result = submit_live_for_approval(conn, approval=_approval(), now_ms=1_000)
    assert result["ok"] is False and "pre-submit" in result["error"]
    assert place.call_count == 0


def test_partial_fill_persisted_and_reconciled(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    ensure_paper_account(conn)
    partial = {
        "orderId": 5,
        "clientOrderId": "jrv-abc123def456",
        "status": "PARTIALLY_FILLED",
        "executedQty": "0.002",
        "cummulativeQuoteQty": "200",
        "fills": [{}],
    }
    with (
        patch("jarvise_trade.submit.fetch_api_restrictions", return_value=SAFE_PERMS),
        patch("jarvise_trade.submit.query_order", return_value=None),
        patch("jarvise_trade.submit.place_spot_market_order", return_value=partial),
    ):
        result = submit_live_for_approval(conn, approval=_approval(), now_ms=1_000)
    live = result["live_order"]
    assert live["status"] == "partially_filled" and live["executed_qty"] == 0.002
    assert [r["id"] for r in list_live_orders_open(conn)] == [live["id"]]

    filled = {
        **partial,
        "status": "FILLED",
        "executedQty": "0.005",
        "cummulativeQuoteQty": "500",
        "fills": [{}, {}, {}],
    }
    with patch("jarvise_trade.reconcile.query_order", return_value=filled):
        rec = reconcile_live_orders(conn, now_ms=5_000)
    assert rec["ok"] and rec["updated"] == [{"id": live["id"], "from": "partially_filled", "to": "filled"}]
    row = get_live_order_by_client_id(conn, "jrv-abc123def456")
    assert row["status"] == "filled" and row["executed_qty"] == 0.005 and row["fills_count"] == 3
    assert row["reconciled_at_ms"] == 5_000
    assert list_live_orders_open(conn) == []


def test_reconcile_noop_without_open_orders_and_no_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_TRADE_API_KEY", raising=False)
    conn = open_db(tmp_path / "l.db")
    rec = reconcile_live_orders(conn, now_ms=1)
    assert rec == {
        "ok": True,
        "paper_only": False,
        "read_only": True,
        "dry_run": False,
        "at_ms": 1,
        "open": 0,
        "updated": [],
        "unchanged": [],
        "missing_on_venue": [],
        "errors": [],
    }


def test_reconcile_missing_keys_reports_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_TRADE_API_KEY", raising=False)
    conn = open_db(tmp_path / "l.db")
    insert_live_order(
        conn,
        {
            "id": "o1",
            "created_at_ms": 1,
            "venue": "binance",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "status": "submitted",
            "kill_switch_clear": 1,
            "caps_ok": 1,
            "client_order_id": "jrv-o1",
        },
    )
    rec = reconcile_live_orders(conn, now_ms=2)
    assert rec["ok"] is False and rec["open"] == 1
    assert "credentials" in rec["errors"][0]


def test_reconcile_marks_missing_on_venue_and_dry_run(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    conn = open_db(tmp_path / "l.db")
    insert_live_order(
        conn,
        {
            "id": "o2",
            "created_at_ms": 1,
            "venue": "binance",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "status": "submitted",
            "kill_switch_clear": 1,
            "caps_ok": 1,
            "client_order_id": "jrv-o2",
        },
    )
    with patch("jarvise_trade.reconcile.query_order", return_value=None):
        dry = reconcile_live_orders(conn, now_ms=3, dry_run=True)
    assert dry["missing_on_venue"] == ["o2"]
    assert list_live_orders_open(conn)[0]["status"] == "submitted"
    with patch("jarvise_trade.reconcile.query_order", return_value=None):
        wet = reconcile_live_orders(conn, now_ms=4)
    assert wet["missing_on_venue"] == ["o2"]
    assert list_live_orders_open(conn) == []
