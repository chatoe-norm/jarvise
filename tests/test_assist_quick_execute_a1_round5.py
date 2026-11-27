"""Quick Plan A1 — fix round 5 (terminal evidence, emergency bookkeeping, cleanup retry)."""

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
from tests.test_assist_quick_execute_a1_round4 import _TimeoutPosFallbackTrade  # noqa: E402

from jarvise_assist.execute import (
    RECOVER_MAX_AGE_MS,
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
# 1. High — Quick partial + flat stays unknown; FILLED+flat → done
# ---------------------------------------------------------------------------


class _PartialFlatRecoverTrade(RecordingTrade):
    """Recovery query returns PARTIALLY_FILLED; position flat until reopen."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.order_status = "PARTIALLY_FILLED"
        self.order_qty = "0.05"
        self._positions = []

    def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.calls.append(("query_order", {"symbol": symbol, "client_id": client_id}))
        return {
            "orderId": 9101,
            "status": self.order_status,
            "executedQty": self.order_qty,
            "avgPrice": "100",
            "clientOrderId": client_id,
        }

    def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.calls.append(("query_order_by_client_id", {"symbol": symbol, "client_id": client_id}))
        return self.query_order(symbol=symbol, client_id=client_id)


def test_r5_quick_partial_flat_stays_unknown_then_protects_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FOUND PARTIALLY_FILLED + flat must not finish Quick as done (classify treats qty>0 as FILLED)."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    conn = open_db(tmp_path / "r5_partial_flat.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, qty=0.1)
    preview = _quick_preview(qty="0.1", card_id=card)
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

    trade = _PartialFlatRecoverTrade(mark=Decimal("100"))
    trade.order_status = "PARTIALLY_FILLED"
    trade.order_qty = "0.05"
    trade._positions = []
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    after = get_exec_intent(conn, iid)
    assert after is not None
    assert after["status"] == "unknown", after["status"]

    # Further fill reopens position → next recovery protects / tracks it.
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()
    trade._positions = [_Pos("BTCUSDT", "long", "0.1", "100")]
    trade.order_status = "PARTIALLY_FILLED"
    trade.order_qty = "0.1"
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 120_000, send=None, sleep=lambda _s: None)
    mid = get_exec_intent(conn, iid)
    assert mid is not None
    # Non-final → still unknown (or recovering→unknown) while stop is placed/verified.
    assert mid["status"] in {"unknown", "recovering"}, mid["status"]
    assert any(
        c[0] == "place_algo_stop" and c[1].get("order_type") == "STOP_MARKET" for c in trade.calls
    ) or any(c[0] == "open_algo_orders" or c[0] == "all_open_orders" for c in trade.calls)

    # FOUND FILLED + flat → done
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()
    trade._positions = []
    trade.order_status = "FILLED"
    trade.order_qty = "0.1"
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 180_000, send=None, sleep=lambda _s: None)
    done = get_exec_intent(conn, iid)
    assert done is not None
    assert done["status"] == "done", done["status"]
    conn.close()


def test_r5_quick_filled_flat_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "r5_filled_flat.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = _quick_preview(qty="0.05")
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
    trade = _PartialFlatRecoverTrade(mark=Decimal("100"))
    trade.order_status = "FILLED"
    trade.order_qty = "0.05"
    trade._positions = []
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    done = get_exec_intent(conn, iid)
    assert done is not None
    assert done["status"] == "done", done["status"]
    conn.close()


# ---------------------------------------------------------------------------
# 2. Medium — emergency close logs mkt_order_resolve; sync keeps unresolved
# ---------------------------------------------------------------------------


class _TimeoutFallbackSlFailTrade(_TimeoutPosFallbackTrade):
    """Timeout → unreadable queries → position fallback → SL fail → emergency close.

    Resolve succeeds only after the position is flat (post emergency close).
    """

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.stop_ok = False

    def place_algo_stop(self, **kw: Any) -> dict[str, Any]:
        # Always fail STOP_MARKET; allow TP if any (won't reach TP after emergency).
        if kw.get("order_type") == "STOP_MARKET":
            self.calls.append(("place_algo_stop", {**kw, "reduce_only": True}))
            raise TradeClientError("stop refused", http_status=400, binance_code=-2021)
        return super().place_algo_stop(**kw)

    def _sl_attempted(self) -> bool:
        return any(c[0] == "place_algo_stop" and c[1].get("order_type") == "STOP_MARKET" for c in self.calls)

    def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.calls.append(("query_order", {"symbol": symbol, "client_id": client_id}))
        # Entry burst + while still open: unreadable → position fallback, no orderId logged.
        # After SL fail + emergency close left us flat: resolve for bookkeeping.
        if self._positions or not self._sl_attempted():
            raise TradeClientError("timeout", http_status=504, binance_code=None)
        return {
            "orderId": self._resolve_oid,
            "status": "FILLED",
            "executedQty": "0.05",
            "avgPrice": "100",
            "clientOrderId": client_id,
        }

    def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.calls.append(("query_order_by_client_id", {"symbol": symbol, "client_id": client_id}))
        return self.query_order(symbol=symbol, client_id=client_id)


def test_r5_emergency_close_logs_resolve_and_sync_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import REG_EXPIRE_GRACE_MS, create_quick_registration, sync_quick_trades

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r5_emerg.db")
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
    rid, reason = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.05,
        now_ms=ts,
        exec_intent_id=iid,
    )
    assert rid is not None, reason

    trade = _TimeoutFallbackSlFailTrade(
        intent_id=iid, mark=Decimal("100"), fill_qty=Decimal("0.05"), resolve_oid="424242"
    )
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    # Position-fallback stamps status NEW (non-final) → emergency close keeps unknown.
    assert result.status == "unknown", result
    rows = conn.execute(
        """
        SELECT order_or_algo_id FROM assist_exec_log
        WHERE intent_id=? AND step='mkt_order_resolve'
        """,
        (iid,),
    ).fetchall()
    assert any(str(r["order_or_algo_id"]) == "424242" for r in rows), rows
    # Registration must stay open (unknown ≠ proven unfilled).
    reg0 = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg0 is not None and reg0["status"] == "open", reg0

    # Sync tags/closes via resolved orderId; fills lack clientOrderId (loss counted, not expired).
    buy_t = ts + 1_000
    sell_t = ts + 5_000
    fills = [
        {
            "id": 1,
            "orderId": 424242,
            "side": "BUY",
            "price": "100",
            "qty": "0.05",
            "time": buy_t,
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "0",
        },
        {
            "id": 2,
            "orderId": 424243,
            "side": "SELL",
            "price": "99",
            "qty": "0.05",
            "time": sell_t,
            "commission": "0.01",
            "commissionAsset": "USDT",
            "realizedPnl": "-0.05",
        },
    ]

    class _Client:
        def position_risk(self, symbol: str | None = None) -> list[Any]:
            return []

    monkeypatch.setattr(qmod, "fetch_all_user_trades", lambda *_a, **_k: list(fills))
    monkeypatch.setattr(qmod, "fetch_income_range", lambda *_a, **_k: [])
    # Past registration TTL + grace; would have expired without resolve / keep rule.
    info = sync_quick_trades(
        conn,
        _Client(),
        now_ms=ts + 10 * 60_000 + REG_EXPIRE_GRACE_MS + 1,
        send=lambda *_a, **_k: True,
    )

    reg = conn.execute("SELECT status, trade_id FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None
    assert reg["status"] == "tagged", reg["status"]
    assert reg["trade_id"]
    trade_row = conn.execute(
        "SELECT status, net_r FROM assist_quick_trade WHERE id=?", (reg["trade_id"],)
    ).fetchone()
    assert trade_row is not None
    assert trade_row["status"] == "closed"
    assert float(trade_row["net_r"] or 0) < 0
    assert (info or {}).get("expired", 0) == 0
    conn.close()


def test_r5_sync_keeps_unresolved_exec_linked_when_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unresolved bot orderId must not expire merely because the position is flat."""
    from jarvise_assist import quick as qmod
    from jarvise_assist.quick import REG_EXPIRE_GRACE_MS, create_quick_registration, sync_quick_trades

    conn = open_db(tmp_path / "r5_keep.db")
    _seed_settings(conn)
    created = _daytime_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "qty": "0.05", "entry": "100", "stop": "99"},
        created_at_ms=created,
        expires_at_ms=created + 30_000,
    )
    # Market log has cid but no orderId — unresolved attribution.
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

    class _FlatClient:
        def position_risk(self, *, symbol: str | None = None) -> list[Any]:
            return []

        def all_orders(self, symbol: str, **_kw: Any) -> list[dict[str, Any]]:
            return []

        def user_trades(self, symbol: str, **_kw: Any) -> list[dict[str, Any]]:
            return []

    # Fills lack clientOrderId and we have no orderId → after stays empty.
    monkeypatch.setattr(
        qmod,
        "fetch_all_user_trades",
        lambda *_a, **_k: [
            {
                "id": 1,
                "orderId": 999,
                "side": "BUY",
                "price": "100",
                "qty": "0.05",
                "time": created + 1_000,
                "commission": "0",
                "commissionAsset": "USDT",
                "realizedPnl": "0",
            }
        ],
    )
    monkeypatch.setattr(qmod, "fetch_income_range", lambda *_a, **_k: [])
    sync_quick_trades(
        conn,
        _FlatClient(),
        now_ms=created + 10 * 60_000 + REG_EXPIRE_GRACE_MS + 1,
        send=lambda *_a, **_k: True,
    )

    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None
    assert reg["status"] == "open", "must keep unresolved exec-linked while within RECOVER_MAX_AGE_MS"

    # Past the bound → may expire.
    monkeypatch.setattr(qmod, "fetch_all_user_trades", lambda *_a, **_k: [])
    sync_quick_trades(
        conn,
        _FlatClient(),
        now_ms=created + RECOVER_MAX_AGE_MS + 1,
        send=lambda *_a, **_k: True,
    )
    reg2 = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg2 is not None
    assert reg2["status"] == "expired"
    conn.close()


# ---------------------------------------------------------------------------
# 3. Medium — deferred jv- cleanup retried after recovery
# ---------------------------------------------------------------------------


def _seed_closed_exec_quick(conn: Any, *, intent_id: str, symbol: str = "BTCUSDT", closed_at: int) -> str:
    from jarvise_assist.quick import create_quick_registration

    rid, reason = create_quick_registration(
        conn,
        symbol=symbol,
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.05,
        now_ms=closed_at - 60_000,
        exec_intent_id=intent_id,
    )
    assert rid is not None, reason
    tid = f"aqt_{intent_id[-8:]}"
    conn.execute(
        """
        INSERT INTO assist_quick_trade (
            id, registration_id, symbol, side, planned_stop, entry_time_ms,
            entry_qty, entry_avg, closed_at_ms, exit_avg, entry_trade_ids, exit_trade_ids,
            realized_pnl_usdt, commission_usdt, funding_usdt, net_usdt, risk_usdt, net_r,
            status, updated_at_ms
        ) VALUES (?, ?, ?, 'long', 99, ?, 0.05, 100, ?, 99, '[]', '[]',
                  -0.05, 0, 0, -0.05, 0.05, -1.0, 'closed', ?)
        """,
        (tid, rid, symbol, closed_at - 50_000, closed_at, closed_at),
    )
    conn.execute(
        "UPDATE assist_quick_registration SET status='tagged', trade_id=?, updated_at_ms=? WHERE id=?",
        (tid, closed_at, rid),
    )
    conn.commit()
    return tid


def test_r5_deferred_cleanup_retries_after_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.watch import QUICK_JV_CLEANUP_STEP, _cleanup_exec_linked_quick_jv

    conn = open_db(tmp_path / "r5_cleanup.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    intent_a = "aei_r5cleanA001"
    intent_b = "aei_r5cleanB002"
    # Insert intent A as unknown (in-flight) so first cleanup skips.
    insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick"},
        created_at_ms=ts - 120_000,
        expires_at_ms=ts - 90_000,
    )
    # Force the id we use for client-order prefixes.
    conn.execute("UPDATE assist_exec_intent SET id=? WHERE symbol='BTCUSDT'", (intent_a,))
    conn.commit()
    finish_exec_intent(conn, intent_a, status="unknown", result_text="unknown")
    _seed_closed_exec_quick(conn, intent_id=intent_a, closed_at=ts - 30_000)

    # Leftover TP for intent A, owner order, and another intent's jv- order.
    trade = RecordingTrade()
    trade._orders = [
        {
            "symbol": "BTCUSDT",
            "algoId": "901",
            "clientAlgoId": client_order_id(intent_a, "tp"),
            "reduceOnly": True,
        },
        {
            "symbol": "BTCUSDT",
            "algoId": "902",
            "clientAlgoId": "owner-manual-tp",
            "reduceOnly": True,
        },
        {
            "symbol": "BTCUSDT",
            "algoId": "903",
            "clientAlgoId": client_order_id(intent_b, "tp"),
            "reduceOnly": True,
        },
    ]
    trade._positions = []
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "off")
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade, None))

    # Pass 1: intent still unknown → skip (no cancel).
    cancelled1 = _cleanup_exec_linked_quick_jv(
        conn,
        positions=[],
        quick_info={"closed": []},  # already closed last pass — only pending retry
        now_ms=ts,
    )
    assert cancelled1 == []
    assert not any(c[0].startswith("cancel") for c in trade.calls)

    # Recovery finishes intent → next pass cleans up.
    finish_exec_intent(conn, intent_a, status="done", result_text="done", expect_status="unknown")
    cancelled2 = _cleanup_exec_linked_quick_jv(
        conn,
        positions=[],
        quick_info={"closed": []},
        now_ms=ts + 1_000,
    )
    assert any(c["algo_id"] == "901" for c in cancelled2), cancelled2
    assert not any(c["algo_id"] == "902" for c in cancelled2)
    assert not any(c["algo_id"] == "903" for c in cancelled2)
    # Marker written.
    assert (
        conn.execute(
            "SELECT 1 FROM assist_exec_log WHERE intent_id=? AND step=?",
            (intent_a, QUICK_JV_CLEANUP_STEP),
        ).fetchone()
        is not None
    )

    # Pass 3: no re-cancel.
    trade.calls.clear()
    # Re-add leftover that somehow returned (should still not cancel again).
    trade._orders = [
        {
            "symbol": "BTCUSDT",
            "algoId": "901",
            "clientAlgoId": client_order_id(intent_a, "tp"),
            "reduceOnly": True,
        }
    ]
    cancelled3 = _cleanup_exec_linked_quick_jv(
        conn,
        positions=[],
        quick_info={"closed": []},
        now_ms=ts + 2_000,
    )
    assert cancelled3 == []
    assert not any(c[0].startswith("cancel") for c in trade.calls)
    conn.close()


# ---------------------------------------------------------------------------
# 4. Test gaps A45 / A47
# ---------------------------------------------------------------------------


class _FoundNeitherFillTrade(RecordingTrade):
    """Market body UNKNOWN; query FOUND neither FILLED nor NOT_FILLED → first fallback branch."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._market_sent = False

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
        self._market_sent = True
        # executedQty 0 + NEW → classify UNKNOWN
        body = {
            "orderId": 9100,
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
            "clientOrderId": client_id,
        }
        self.fills.append(body)
        return body

    def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.calls.append(("query_order", {"symbol": symbol, "client_id": client_id}))
        # FOUND but still neither FILLED nor NOT_FILLED (qty 0, NEW).
        return {
            "orderId": 9100,
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
            "clientOrderId": client_id,
        }

    def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self.calls.append(("query_order_by_client_id", {"symbol": symbol, "client_id": client_id}))
        return self.query_order(symbol=symbol, client_id=client_id)

    def position_risk(self, symbol: str | None = None) -> list[_Pos]:
        # After market send only: position appears (exchange filled despite NEW body).
        if self._market_sent and not self._positions:
            fq = self.fills_qty if self.fills_qty is not None else Decimal("0.05")
            self._positions = [_Pos("BTCUSDT", "long", str(fq), "100")]
        return super().position_risk(symbol=symbol)


def test_r5_a45_first_fallback_branch_keeps_nonfinal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A45: first position-fallback branch (~2867) must keep status NEW → unknown, not done."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r5_a45.db")
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
    trade = _FoundNeitherFillTrade(mark=Decimal("100"), fill_qty=Decimal("0.05"))
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    assert result.status == "unknown", result
    # Mutation survivor: if fallback fill were stamped FILLED, we'd finish done.
    assert result.status != "done"
    conn.close()


def test_r5_a47_main_recovery_verified_final_no_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A47: main recovery VERIFIED+final must not call `_ensure_mkt_order_id_logged`.

    Pre-logged orderId from recover_market_query would short-circuit the helper and
    hide the A47 mutant (`if True: _ensure_mkt_order_id_logged`). Track the helper
    itself so the mutant fails even when a query already logged an orderId.
    """
    import jarvise_assist.execute as execute_mod

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "r5_a47.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = {
        "qty": "0.05",
        "entry": "100",
        "stop": "99",
        "target": "101",
        "leverage": 2,
        "loss_at_stop": "0.05",
        "signal_id": "sig_r5_a47",
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
    mkt_cid = client_order_id(iid, "mkt")
    resolve_helper_calls: list[str] = []
    real_ensure = execute_mod._ensure_mkt_order_id_logged

    def _track_ensure(*args: Any, **kwargs: Any) -> Any:
        iid_s = kwargs.get("intent_id")
        if iid_s is None and len(args) > 2:
            iid_s = args[2]
        resolve_helper_calls.append(str(iid_s or ""))
        return real_ensure(*args, **kwargs)

    monkeypatch.setattr(execute_mod, "_ensure_mkt_order_id_logged", _track_ensure)

    class _MainVerifiedTrade(RecordingTrade):
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
            if client_id == mkt_cid:
                # FOUND final — no orderId logged as resolve source (empty → trim skip).
                return {
                    "orderId": "",
                    "status": "FILLED",
                    "executedQty": "0.05",
                    "avgPrice": "100",
                    "clientOrderId": client_id,
                }
            raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)

        def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
            self.calls.append(("query_order_by_client_id", {"symbol": symbol, "client_id": client_id}))
            return self.query_order(symbol=symbol, client_id=client_id)

    trade = _MainVerifiedTrade()
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    done = get_exec_intent(conn, iid)
    assert done is not None
    assert done["status"] == "done", done["status"]
    rows = conn.execute(
        "SELECT step FROM assist_exec_log WHERE intent_id=? AND step='mkt_order_resolve'",
        (iid,),
    ).fetchall()
    assert rows == []
    assert resolve_helper_calls == [], resolve_helper_calls
    conn.close()
