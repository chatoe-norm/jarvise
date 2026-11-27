"""Quick Plan A1 — fix round 4 (partial-fill recovery evidence, sync resolve, main parity)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_quick_execute_a1 import (  # noqa: E402
    RecordingTrade,
    _daytime_ms,
    _insert_quoted_card,
    _Pos,
    _quick_preview,
    _seed_quick_settings,
    _seed_settings,
)
from tests.test_assist_quick_execute_a1_round3 import _PartialFillTrade  # noqa: E402

from jarvise_assist.execute import (
    TradeClientError,
    _recover_entry_intent,
    client_order_id,
    finish_exec_intent,
    run_entry_sequence,
)
from jarvise_assist.store import (
    claim_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
)
from jarvise_ingest.db import open_db

# ---------------------------------------------------------------------------
# 1. Quick partial → unreadable recovery stays unknown; FOUND+FILLED → done
# ---------------------------------------------------------------------------


class _PartialThenRecoverTrade(_PartialFillTrade):
    """Entry partial; recovery query mode controlled by ``recover_mode``."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.recover_mode = "unreadable"  # unreadable | filled
        self._recover_query_n = 0
        self.resolve_calls: list[str] = []

    def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        # During entry, market body already returned PARTIALLY_FILLED — query unused.
        # During recovery, _query_fill_after_unknown bursts this.
        self._recover_query_n += 1
        if self.recover_mode == "filled":
            for f in self.fills:
                if f.get("clientOrderId") == client_id:
                    return {
                        **f,
                        "status": "FILLED",
                        "executedQty": str(self._positions[0].qty if self._positions else f["executedQty"]),
                    }
            return {
                "orderId": 9101,
                "status": "FILLED",
                "executedQty": "0.1",
                "avgPrice": str(self.fills_avg),
                "clientOrderId": client_id,
            }
        raise TradeClientError("timeout", http_status=504, binance_code=None)

    def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.resolve_calls.append(client_id)
        self.calls.append(("query_order_by_client_id", {"symbol": symbol, "client_id": client_id}))
        return self.query_order(symbol=symbol, client_id=client_id)


def test_r4_quick_partial_unreadable_recovery_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop covering partial qty must not finish Quick as done without terminal order evidence."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r4_partial_unk.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, qty=0.2)
    preview = _quick_preview(qty="0.2", card_id=card)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid)
    trade = _PartialThenRecoverTrade(mark=Decimal("100"), fill_qty=Decimal("0.2"))
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    assert result.status == "unknown", result
    finish_exec_intent(conn, iid, status="unknown", result_text=str(result), expect_status="confirmed")

    # Recovery: unreadable market query + stop still covers current partial qty.
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()
    trade.recover_mode = "unreadable"
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    after = get_exec_intent(conn, iid)
    assert after is not None
    assert after["status"] == "unknown", after["status"]

    # Later pass with FOUND+FILLED → done (terminal evidence).
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()
    trade.recover_mode = "filled"
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 120_000, send=None, sleep=lambda _s: None)
    done = get_exec_intent(conn, iid)
    assert done is not None
    assert done["status"] == "done", done["status"]
    conn.close()


# ---------------------------------------------------------------------------
# 2. sync_quick_trades consumes mkt_order_resolve
# ---------------------------------------------------------------------------


def test_r4_sync_tags_via_mkt_order_resolve_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.quick import create_quick_registration, sync_quick_trades

    conn = open_db(tmp_path / "r4_sync_resolve.db")
    _seed_settings(conn)
    created = 1_700_000_400_000
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "qty": "0.05", "entry": "100", "stop": "99"},
        created_at_ms=created,
        expires_at_ms=created + 30_000,
    )
    # Only resolve step carries orderId; market log empty; fills lack clientOrderId.
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
        order_or_algo_id="",
        http_status=200,
        created_at_ms=created,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="mkt_order_resolve",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
        order_or_algo_id="555666",
        http_status=200,
        created_at_ms=created + 1,
    )
    rid, reason = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.05,
        now_ms=created,
        exec_intent_id=iid,
    )
    assert rid is not None, reason

    buy_t = created + 2_000
    sell_t = created + 60_000
    fills = [
        {
            "id": 1,
            "orderId": 555666,
            "side": "BUY",
            "price": "100",
            "qty": "0.05",
            "time": buy_t,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
            # No clientOrderId — attribution must use resolved orderId.
        },
        {
            "id": 2,
            "orderId": 555667,
            "side": "SELL",
            "price": "101",
            "qty": "0.05",
            "time": sell_t,
            "commission": "0",
            "commissionAsset": "USDT",
            "realizedPnl": "0.05",
        },
    ]

    monkeypatch.setattr(
        "jarvise_assist.quick.fetch_all_user_trades",
        lambda *_a, **_k: list(fills),
    )
    monkeypatch.setattr(
        "jarvise_assist.quick.fetch_income_range",
        lambda *_a, **_k: [],
    )
    info = sync_quick_trades(conn, object(), now_ms=sell_t + 5_000, send=lambda *_a, **_k: True)
    assert int(info.get("tagged") or 0) == 1, info
    st = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert st is not None and str(st["status"]) == "tagged", (st["status"], info)
    row = conn.execute("SELECT status FROM assist_quick_trade WHERE registration_id=?", (rid,)).fetchone()
    assert row is not None
    assert str(row["status"]) == "closed", dict(row)
    assert info.get("closed"), info
    conn.close()


# ---------------------------------------------------------------------------
# 3. Main-path parity: no resolve before SL; Quick resolve after SL
# ---------------------------------------------------------------------------


class _TimeoutPosFallbackTrade(RecordingTrade):
    """Market times out; position appears after send; resolve finds orderId."""

    def __init__(self, *, intent_id: str, resolve_oid: str = "424242", **kw: Any) -> None:
        super().__init__(**kw)
        self._mkt_cid = client_order_id(intent_id, "mkt")
        self._resolve_oid = resolve_oid
        self._positions: list[_Pos] = []  # flat at baseline
        self._filled_after_market = False
        self.resolve_call_indexes: list[int] = []

    def market_order(
        self, *, symbol: str, side: str, qty: Decimal, client_id: str, reduce_only: bool = False
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "market_order",
                {
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "client_id": client_id,
                    "reduce_only": reduce_only,
                },
            )
        )
        if reduce_only:
            self._positions = []
            return {
                "orderId": 99,
                "status": "FILLED",
                "executedQty": str(qty),
                "avgPrice": "100",
                "clientOrderId": client_id,
            }
        # Exchange accepted but reply timed out; position will show on next read.
        self._filled_after_market = True
        raise TradeClientError("timeout", http_status=504, binance_code=None)

    def position_risk(self, symbol: str | None = None) -> list[_Pos]:
        if self._filled_after_market and not self._positions:
            fq = self.fills_qty if self.fills_qty is not None else Decimal("0.05")
            self._positions = [_Pos("BTCUSDT", "long", str(fq), "100")]
        return super().position_risk(symbol=symbol)

    def _stop_already_placed(self) -> bool:
        return any(c[0] == "place_algo_stop" and c[1].get("order_type") == "STOP_MARKET" for c in self.calls)

    def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.calls.append(("query_order", {"symbol": symbol, "client_id": client_id}))
        # Entry burst: not found → position fallback. Post-SL resolve may succeed.
        if not self._stop_already_placed():
            raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)
        return {
            "orderId": self._resolve_oid,
            "status": "FILLED",
            "executedQty": "0.05",
            "avgPrice": "100",
            "clientOrderId": client_id,
        }

    def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.resolve_call_indexes.append(len(self.calls))
        self.calls.append(("query_order_by_client_id", {"symbol": symbol, "client_id": client_id}))
        # Same as query_order: fail before protection, succeed after SL (Quick resolve).
        if not self._stop_already_placed():
            raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)
        return {
            "orderId": self._resolve_oid,
            "status": "FILLED",
            "executedQty": "0.05",
            "avgPrice": "100",
            "clientOrderId": client_id,
        }


def _call_names(trade: RecordingTrade) -> list[str]:
    return [c[0] for c in trade.calls]


def test_r4_main_timeout_fallback_no_resolve_before_sl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r4_main_fb.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = {
        "qty": "0.05",
        "entry": "100",
        "stop": "99",
        "target": "101",
        "leverage": 2,
        "loss_at_stop": "0.05",
        "signal_id": "sig_r4_main",
    }
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid)
    trade = _TimeoutPosFallbackTrade(intent_id=iid, mark=Decimal("100"), fill_qty=Decimal("0.05"))
    result = run_entry_sequence(conn, iid, trade=trade, origin=None, sleep=lambda _s: None)
    assert result.status in {"done", "unknown"}, result
    names = _call_names(trade)
    # Market-query burst may use query_order_by_client_id (5608315). Attribution resolve must not.
    rows = conn.execute(
        "SELECT step FROM assist_exec_log WHERE intent_id=? AND step='mkt_order_resolve'",
        (iid,),
    ).fetchall()
    assert rows == []
    sl_i = next(
        i
        for i, c in enumerate(trade.calls)
        if c[0] == "place_algo_stop" and c[1].get("order_type") == "STOP_MARKET"
    )
    # After position fallback establishes the fill, no extra resolve before SL.
    # Find last position_risk before SL (the fallback read).
    pos_before_sl = [i for i, n in enumerate(names[:sl_i]) if n == "position_risk"]
    assert pos_before_sl, names
    between = names[pos_before_sl[-1] : sl_i]
    assert "query_order_by_client_id" not in between, (between, names)
    conn.close()


def test_r4_main_recovery_not_found_no_mkt_order_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "r4_main_rec.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = {
        "qty": "0.05",
        "entry": "100",
        "stop": "99",
        "target": "101",
        "leverage": 2,
        "loss_at_stop": "0.05",
        "signal_id": "sig_r4_main_rec",
    }
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    finish_exec_intent(conn, iid, status="unknown", result_text="unknown")
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()
    # Seed a verified stop log so recovery can verify without placing.
    insert_exec_log(
        conn,
        intent_id=iid,
        step="sl0",
        endpoint="/fapi/v1/algoOrder",
        client_order_id=client_order_id(iid, "sl0"),
        order_or_algo_id="8001",
        http_status=200,
        created_at_ms=ts,
    )

    class _MainRecTrade(RecordingTrade):
        def __init__(self) -> None:
            super().__init__(mark=Decimal("100"), fill_qty=Decimal("0.05"))
            sl0 = client_order_id(iid, "sl0")
            self._positions = [_Pos("BTCUSDT", "long", "0.05", "100")]
            self._orders = [
                {
                    "symbol": "BTCUSDT",
                    "algoId": "8001",
                    "clientAlgoId": sl0,
                    "side": "SELL",
                    "type": "STOP_MARKET",
                    "orderType": "STOP_MARKET",
                    "quantity": "0.05",
                    "triggerPrice": "99",
                    "reduceOnly": True,
                    "algoStatus": "NEW",
                    "status": "NEW",
                    "workingType": "MARK_PRICE",
                    "positionSide": "BOTH",
                }
            ]

        def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
            self.calls.append(("query_order", {"symbol": symbol, "client_id": client_id}))
            raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)

        def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
            self.calls.append(("query_order_by_client_id", {"symbol": symbol, "client_id": client_id}))
            raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)

    trade = _MainRecTrade()
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    # recover_market_query may call query_order_by_client_id; attribution resolve must not run.
    rows = conn.execute(
        "SELECT step FROM assist_exec_log WHERE intent_id=? AND step='mkt_order_resolve'",
        (iid,),
    ).fetchall()
    assert rows == []
    conn.close()


def test_r4_quick_fallback_resolve_after_sl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r4_quick_fb.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, qty=0.05)
    preview = _quick_preview(qty="0.05", card_id=card)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid)
    trade = _TimeoutPosFallbackTrade(
        intent_id=iid, mark=Decimal("100"), fill_qty=Decimal("0.05"), resolve_oid="777888"
    )
    # query_order always -2013 for entry burst; resolve finds order after SL.
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    assert result.status in {"done", "unknown"}, result
    names = _call_names(trade)
    sl_i = next(
        i
        for i, c in enumerate(trade.calls)
        if c[0] == "place_algo_stop" and c[1].get("order_type") == "STOP_MARKET"
    )
    # Attribution resolve (post-SL) — market-query burst may also use the same method earlier.
    resolve_after_sl = [i for i, n in enumerate(names) if n == "query_order_by_client_id" and i > sl_i]
    assert resolve_after_sl, (sl_i, names)
    rows = conn.execute(
        """
        SELECT order_or_algo_id FROM assist_exec_log
        WHERE intent_id=? AND step='mkt_order_resolve'
        """,
        (iid,),
    ).fetchall()
    assert any(str(r["order_or_algo_id"]) == "777888" for r in rows), rows
    conn.close()
