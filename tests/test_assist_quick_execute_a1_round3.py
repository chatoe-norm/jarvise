"""Quick Plan A1 — fix round 3 (partial fill, attribution, mins, chase, real_trades, mutations)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_quick_execute_a1 import (  # noqa: E402
    RecordingTrade,
    _daytime_ms,
    _full_cfg,
    _FuturesRead,
    _insert_quoted_card,
    _Pos,
    _quick_preview,
    _seed_quick_settings,
    _seed_settings,
)
from tests.test_assist_quick_execute_a1_round2 import _ey_env, _mutating_calls  # noqa: E402

from jarvise_assist.execute import (
    client_order_id,
    finish_exec_intent,
    handle_exec_callback,
    run_entry_sequence,
)
from jarvise_assist.quick_m1a1 import handle_quick_trade_callback
from jarvise_assist.store import (
    claim_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
    now_ms,
    write_settings,
)
from jarvise_ingest.db import open_db

# ---------------------------------------------------------------------------
# 1. Partial fill → protect + unknown (Quick only)
# ---------------------------------------------------------------------------


class _PartialFillTrade(RecordingTrade):
    """Market returns PARTIALLY_FILLED with positive qty; later fills would grow."""

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
            body = {
                "orderId": 9100,
                "status": "FILLED",
                "executedQty": str(qty),
                "avgPrice": str(self.fills_avg),
                "clientOrderId": client_id,
            }
            self.fills.append(body)
            return body
        # Partial: half of requested
        fq = (self.fills_qty if self.fills_qty is not None else qty) / 2
        if fq <= 0:
            fq = Decimal("0.01")
        body = {
            "orderId": 9101,
            "status": "PARTIALLY_FILLED",
            "executedQty": str(fq),
            "avgPrice": str(self.fills_avg),
            "clientOrderId": client_id,
        }
        self.fills.append(body)
        self._positions = [
            _Pos(symbol, "long" if side.upper() == "BUY" else "short", str(fq), str(self.fills_avg))
        ]
        return body


def test_r3_quick_partial_fill_protects_and_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r3_partial.db")
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
    trade = _PartialFillTrade(mark=Decimal("100"), fill_qty=Decimal("0.2"))
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick")
    assert result.status == "unknown", result
    stops = [c for c in trade.calls if c[0] == "place_algo_stop" and c[1]["order_type"] == "STOP_MARKET"]
    assert stops, trade.calls
    assert stops[0][1]["qty"] == Decimal("0.1")
    assert stops[0][1]["reduce_only"] is True
    conn.close()


def test_r3_main_partial_fill_still_done_preexisting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Main path keeps pre-existing behaviour (done on partial) — documented only."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "r3_main_partial.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = {
        "qty": "0.2",
        "entry": "100",
        "stop": "99",
        "target": "101",
        "leverage": 2,
        "loss_at_stop": "0.2",
        "signal_id": "sig_main_partial",
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
    trade = _PartialFillTrade(mark=Decimal("100"), fill_qty=Decimal("0.2"))
    result = run_entry_sequence(conn, iid, trade=trade, origin=None)
    assert result.status == "done", result
    conn.close()


# ---------------------------------------------------------------------------
# 2. Position-fallback recovery logs mkt orderId; reg stays open with position
# ---------------------------------------------------------------------------


class _PosFallbackTrade(RecordingTrade):
    """Market send unknown; query eventually finds order; first path uses position."""

    def __init__(self, *, intent_id: str, **kw: Any) -> None:
        super().__init__(**kw)
        self._mkt_cid = client_order_id(intent_id, "mkt")
        self._query_n = 0
        self._positions = [_Pos("BTCUSDT", "long", "0.05", "100")]

    def market_order(
        self, *, symbol: str, side: str, qty: Decimal, client_id: str, reduce_only: bool = False
    ) -> dict[str, Any]:
        from jarvise_assist.execute import TradeClientError

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
        raise TradeClientError("timeout", http_status=504, binance_code=None)

    def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        self._query_n += 1
        # First burst: unreadable / not found → position fallback in entry.
        # Later recovery resolve finds the order.
        if client_id == self._mkt_cid and self._query_n >= 4:
            body = {
                "orderId": 777888,
                "status": "FILLED",
                "executedQty": "0.05",
                "avgPrice": "100",
                "clientOrderId": client_id,
            }
            return body
        from jarvise_assist.execute import TradeClientError

        raise TradeClientError("Order does not exist.", http_status=400, binance_code=-2013)


def test_r3_position_fallback_logs_mkt_order_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _recover_entry_intent

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r3_posfb.db")
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
    trade = _PosFallbackTrade(intent_id=iid, mark=Decimal("100"), fill_qty=Decimal("0.05"))
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    # Entry may finish unknown/done depending on stop; orderId must be resolvable.
    finish_exec_intent(conn, iid, status=result.status, result_text=str(result), expect_status="confirmed")
    # Force recovery path to resolve order id if entry did not.
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    if intent["status"] in {"unknown", "recovering", "done", "failed"}:
        # Re-claim as recovering for recovery helper
        conn.execute(
            "UPDATE assist_exec_intent SET status='recovering' WHERE id=?",
            (iid,),
        )
        conn.commit()
        intent = get_exec_intent(conn, iid)
        assert intent is not None
        _recover_entry_intent(conn, trade, intent, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    rows = conn.execute(
        """
        SELECT order_or_algo_id FROM assist_exec_log
        WHERE intent_id=? AND order_or_algo_id IS NOT NULL AND order_or_algo_id != ''
          AND step IN ('market','market_query','recover_market_query','entry_price_query','mkt_order_resolve')
        """,
        (iid,),
    ).fetchall()
    oids = {str(r["order_or_algo_id"]) for r in rows}
    assert "777888" in oids, oids
    conn.close()


def test_r3_exec_linked_reg_not_expired_while_position_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvise_assist.quick import REG_EXPIRE_GRACE_MS, create_quick_registration, sync_quick_trades

    conn = open_db(tmp_path / "r3_reg_ttl.db")
    _seed_settings(conn)
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
        expires_at_ms=ts + 1,
    )
    # Market logged without orderId (position-only attribution gap)
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
        order_or_algo_id="",
        http_status=200,
        created_at_ms=ts,
    )
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.05,
        now_ms=ts,
        exec_intent_id=iid,
    )
    assert rid is not None
    # Force expires_at into the past
    conn.execute(
        "UPDATE assist_quick_registration SET expires_at_ms=? WHERE id=?",
        (ts - REG_EXPIRE_GRACE_MS - 1, rid),
    )
    conn.commit()

    class _Client:
        def user_trades(self, *_a: Any, **_k: Any) -> list[Any]:
            return []  # no fills → unresolved

        def position_risk(self, symbol: str | None = None) -> list[_Pos]:
            return [_Pos("BTCUSDT", "long", "0.05", "100")]

    monkeypatch.setattr(
        "jarvise_assist.quick.fetch_all_user_trades",
        lambda *_a, **_k: [],
    )
    info = sync_quick_trades(conn, _Client(), now_ms=ts + 3_600_000, send=lambda *_a, **_k: True)
    assert info.get("expired", 0) == 0
    status = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()[
        "status"
    ]
    assert status == "open"
    conn.close()


# ---------------------------------------------------------------------------
# 3. Margin shrink re-checks exchange minimums
# ---------------------------------------------------------------------------


def test_r3_margin_shrink_below_min_refuses_guard_and_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from jarvise_assist.execute import _confirm_guards_quick_entry
    from jarvise_assist.quick_execute import build_bot_plan
    from jarvise_assist.sizing import SignalCandidate

    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "r3_mins.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()

    def _shrink_tiny(admitted: list[Any], **_k: Any) -> tuple[list[Any], list[Any]]:
        out = []
        for c in admitted:
            tiny = replace(
                c.sized,
                qty=Decimal("0.0001"),  # below min_qty 0.001
                notional=Decimal("0.01"),  # below min_notional 5
                loss_at_stop=Decimal("0.0001"),
                leverage_capped=True,
            )
            out.append(SignalCandidate(sized=tiny, confidence=c.confidence, symbol_rank=c.symbol_rank))
        return out, []

    monkeypatch.setattr("jarvise_assist.execute.assign_shared_leverage", _shrink_tiny)
    monkeypatch.setattr("jarvise_assist.quick_execute.assign_shared_leverage", _shrink_tiny)

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
    claim_exec_intent(conn, iid)
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=Decimal("100"), balance=Decimal("10000"), available=Decimal("1"))
    assert _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1) == "below_min_notional"

    # Positive control: without shrink, same setup admits (finish first so not in_flight)
    finish_exec_intent(conn, iid, status="failed", result_text="test", expect_status="confirmed")
    monkeypatch.setattr(
        "jarvise_assist.execute.assign_shared_leverage",
        lambda admitted, **_k: (list(admitted), []),
    )
    iid2 = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=_quick_preview(qty="0.05"),
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid2)
    intent2 = get_exec_intent(conn, iid2)
    assert intent2 is not None
    trade2 = RecordingTrade(mark=Decimal("100"), balance=Decimal("10000"))
    assert _confirm_guards_quick_entry(conn, intent2, trade2, now_ms_value=ts + 1) is None

    # Direct build_bot_plan with shrink → None
    plan = build_bot_plan(
        conn,
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99"),
        balance=Decimal("10000"),
        available=Decimal("1"),
        step=Decimal("0.001"),
        tick=Decimal("0.01"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
        now_ms=ts,
    )
    assert plan is None
    conn.close()


# ---------------------------------------------------------------------------
# 4. Testnet chase same-market (demo frozen at qk:ex)
# ---------------------------------------------------------------------------


def test_r3_testnet_qk_ex_freezes_demo_mark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "r3_tn_freeze.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    card = _insert_quoted_card(conn, now_ms_value=ts, entry=95000.0, stop=94050.0, qty=0.05)
    mainnet = _FuturesRead(mark=Decimal("95000"), balance=Decimal("10000"))
    demo = RecordingTrade(mark=Decimal("200"), balance=Decimal("10000"))
    monkeypatch.setattr(
        "jarvise_assist.execute.build_trade_client",
        lambda: (demo, None),
    )
    sent: list[str] = []
    out = handle_quick_trade_callback(
        conn,
        f"qk:ex:{card}",
        futures=mainnet,
        send=lambda t, **_: sent.append(t) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 42}},
    )
    assert out.get("ok") is True, out
    row = conn.execute("SELECT preview_json FROM assist_exec_intent").fetchone()
    assert row is not None
    preview = json.loads(row["preview_json"])
    assert preview.get("testnet_levels") is True
    assert Decimal(str(preview["entry"])) == Decimal("200")
    conn.close()


def test_r3_testnet_chase_demo_moved_and_stationary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _confirm_guards_quick_entry

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r3_tn_chase.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    # Frozen demo levels: entry 200, stop 198 (1% = R=2). chase_r=0.3 → refuse mark > 200.6
    preview = _quick_preview()
    preview["entry"] = "200"
    preview["stop"] = "198"
    preview["t1"] = "202"
    preview["target"] = "202"
    preview["testnet_levels"] = True
    preview["cfg"] = _full_cfg(quick_chase_r=Decimal("0.3"))
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
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    moved = RecordingTrade(mark=Decimal("200.7"), balance=Decimal("10000"))
    assert _confirm_guards_quick_entry(conn, intent, moved, now_ms_value=ts + 1) == "chase"
    finish_exec_intent(conn, iid, status="failed", result_text="chase", expect_status="confirmed")

    # Positive control: stationary
    iid2 = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=dict(preview),
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid2)
    intent2 = get_exec_intent(conn, iid2)
    assert intent2 is not None
    still = RecordingTrade(mark=Decimal("200.05"), balance=Decimal("10000"))
    assert _confirm_guards_quick_entry(conn, intent2, still, now_ms_value=ts + 1) is None
    conn.close()


# ---------------------------------------------------------------------------
# 5. real_trades Quick exclusion scoped by (symbol, id); algo ≠ trade order
# ---------------------------------------------------------------------------


def test_r3_real_trades_quick_exclusion_symbol_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvise_assist.real_trades import build_real_rows

    conn = open_db(tmp_path / "r3_scope.db")
    _seed_settings(conn)
    ts = now_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "entry": "100", "stop": "99", "qty": "0.1", "loss_at_stop": "0.1"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    # Quick BTC market orderId 4242 AND algoId 9999 (stop)
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
        order_or_algo_id="4242",
        http_status=200,
        created_at_ms=ts,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="stop",
        endpoint="/fapi/v1/algoOrder",
        client_order_id=client_order_id(iid, "sl0"),
        order_or_algo_id="9999",
        http_status=200,
        created_at_ms=ts,
    )

    class _Client:
        def all_orders(self, symbol: str, **_k: Any) -> list[dict[str, Any]]:
            if symbol == "ETHUSDT":
                # Unrelated owner order sharing numeric id 4242 on a different symbol
                return [
                    {
                        "symbol": symbol,
                        "orderId": 4242,
                        "clientOrderId": "owner-eth-4242",
                        "side": "BUY",
                        "status": "FILLED",
                        "time": ts,
                        "updateTime": ts,
                    }
                ]
            if symbol == "SOLUSDT":
                # Owner trade whose orderId equals Quick algoId 9999 — must NOT be hidden
                return [
                    {
                        "symbol": symbol,
                        "orderId": 9999,
                        "clientOrderId": "owner-sol-9999",
                        "side": "BUY",
                        "status": "FILLED",
                        "time": ts,
                        "updateTime": ts,
                    }
                ]
            return []

        def user_trades(self, symbol: str, **_k: Any) -> list[dict[str, Any]]:
            if symbol == "ETHUSDT":
                return [
                    {
                        "id": 10,
                        "orderId": 4242,
                        "symbol": symbol,
                        "side": "BUY",
                        "price": "100",
                        "qty": "0.1",
                        "time": ts,
                        "commission": "0",
                        "commissionAsset": "USDT",
                        "clientOrderId": "owner-eth-4242",
                    }
                ]
            if symbol == "SOLUSDT":
                return [
                    {
                        "id": 11,
                        "orderId": 9999,
                        "symbol": symbol,
                        "side": "BUY",
                        "price": "100",
                        "qty": "0.1",
                        "time": ts,
                        "commission": "0",
                        "commissionAsset": "USDT",
                        "clientOrderId": "owner-sol-9999",
                    }
                ]
            return []

        def income(self, **_k: Any) -> list[Any]:
            return []

    monkeypatch.setattr(
        "jarvise_assist.real_trades.fetch_all_orders",
        lambda client, symbol, **k: client.all_orders(symbol),
    )
    monkeypatch.setattr(
        "jarvise_assist.real_trades.fetch_all_user_trades",
        lambda client, symbol, **k: client.user_trades(symbol),
    )
    monkeypatch.setattr("jarvise_assist.real_trades.fetch_all_income", lambda *_a, **_k: [])
    rows = build_real_rows(conn, _Client(), since_ms=ts - 1, until_ms=ts + 100)
    eth = [r for r in rows if r.get("symbol") == "ETHUSDT"]
    sol = [r for r in rows if r.get("symbol") == "SOLUSDT"]
    assert eth, "ETH owner order sharing BTC Quick orderId must remain visible"
    assert sol, "SOL owner order sharing Quick algoId must remain visible"
    conn.close()


# ---------------------------------------------------------------------------
# 6a. ey: leverage ≤ min(config, 5) end-to-end (mutation killer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cfg_lev,expect_max", [(5, 5), (3, 3)])
def test_r3_ey_set_leverage_clamped_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg_lev: int, expect_max: int
) -> None:
    from jarvise_assist.quick_config import set_quick_config

    _ey_env(monkeypatch)
    conn = open_db(tmp_path / f"r3_lev_{cfg_lev}.db")
    _seed_settings(conn)
    _seed_quick_settings(conn, risk=2.0, cap=5.0)
    ts = _daytime_ms()
    set_quick_config(conn, "quick_max_leverage", str(cfg_lev), source="test", now_ms=ts)
    set_quick_config(conn, "quick_risk_pct", "2.0", source="test", now_ms=ts)
    set_quick_config(conn, "quick_risk_cap_pct", "5", source="test", now_ms=ts)
    set_quick_config(conn, "quick_max_fee_r", "0.5", source="test", now_ms=ts)
    # Tight stop → large qty for risk%; small balance → uncapped leverage > 5
    card = _insert_quoted_card(conn, now_ms_value=ts, qty=50.0, stop=99.7)
    preview = _quick_preview(qty="50.0", card_id=card)
    preview["stop"] = "99.7"
    preview["entry"] = "100"
    preview["t1"] = "100.3"
    preview["target"] = "100.3"
    preview["risk_pct_used"] = "2.0"
    preview["cfg"] = _full_cfg(
        quick_max_leverage=cfg_lev,
        lev_used=min(cfg_lev, 5),
        quick_risk_pct=Decimal("2.0"),
        quick_risk_cap_pct=Decimal("5"),
        risk_used="2.0",
        quick_max_fee_r=Decimal("0.5"),
    )
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    trade = RecordingTrade(
        mark=Decimal("100"),
        balance=Decimal("100"),
        available=Decimal("100"),
        fill_qty=Decimal("5"),
    )
    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda *_a, **_k: True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("ok") is True, out
    lev_calls = [c for c in trade.calls if c[0] == "set_leverage"]
    assert lev_calls, trade.calls
    assert all(int(c[1]["leverage"]) <= expect_max for c in lev_calls), lev_calls
    conn.close()


# ---------------------------------------------------------------------------
# 6b. Direct guard flag-off → disabled (early ey: gate alone is not enough)
# ---------------------------------------------------------------------------


def test_r3_guard_flag_off_direct_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.execute import _confirm_guards_quick_entry

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "off")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r3_flag_direct.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()
    preview = _quick_preview()
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
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=Decimal("100"))
    assert _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1) == "disabled"
    finish_exec_intent(conn, iid, status="failed", result_text="off", expect_status="confirmed")

    # Positive control: flag on → not disabled for the same setup
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    iid2 = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=_quick_preview(),
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid2)
    intent2 = get_exec_intent(conn, iid2)
    assert intent2 is not None
    assert _confirm_guards_quick_entry(conn, intent2, trade, now_ms_value=ts + 1) != "disabled"
    conn.close()


# ---------------------------------------------------------------------------
# 6c. qk:ex refusals with positive control (_FuturesRead)
# ---------------------------------------------------------------------------


def test_r3_qk_ex_refusals_with_positive_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    conn = open_db(tmp_path / "r3_qk_ex.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()

    def _run(cid: str, fut: _FuturesRead, *, now: int) -> dict[str, Any]:
        return handle_quick_trade_callback(
            conn,
            f"qk:ex:{cid}",
            futures=fut,
            send=lambda *_a, **_k: True,
            answer=lambda *_a, **_k: None,
            edit=lambda *_a, **_k: True,
            now_ms=now,
            message={"message_id": 1, "chat": {"id": 42}},
        )

    # Positive control: creates exactly one entry intent, zero trade writes
    cid_ok = _insert_quoted_card(conn, now_ms_value=ts, size_mode="risk")
    fut_ok = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    out_ok = _run(cid_ok, fut_ok, now=ts)
    assert out_ok.get("ok") is True, out_ok
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0] == 1
    assert _mutating_calls(fut_ok) == []

    # Flag off → zero new intents, zero trade writes
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "off")
    before = conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0]
    cid_off = _insert_quoted_card(conn, symbol="ETHUSDT", now_ms_value=ts + 1, size_mode="risk")
    fut_off = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    out_off = _run(cid_off, fut_off, now=ts + 1)
    assert out_off.get("ok") is False
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0] == before
    assert _mutating_calls(fut_off) == []

    # size_mode min_lot → refuse
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    before = conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0]
    cid_lot = _insert_quoted_card(conn, symbol="SOLUSDT", now_ms_value=ts + 2, size_mode="min_lot")
    fut_lot = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    out_lot = _run(cid_lot, fut_lot, now=ts + 2)
    assert out_lot.get("ok") is False
    assert out_lot.get("reason") == "size_mode"
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0] == before
    assert _mutating_calls(fut_lot) == []

    # pause → refuse
    write_settings(conn, paused_until_ms=ts + 3_600_000, pause_reason="streak")
    before = conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0]
    cid_pause = _insert_quoted_card(conn, symbol="BNBUSDT", now_ms_value=ts + 3, size_mode="risk")
    fut_pause = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    out_pause = _run(cid_pause, fut_pause, now=ts + 3)
    assert out_pause.get("ok") is False
    assert out_pause.get("reason") == "paused"
    assert conn.execute("SELECT COUNT(*) FROM assist_exec_intent").fetchone()[0] == before
    assert _mutating_calls(fut_pause) == []
    conn.close()


# ---------------------------------------------------------------------------
# 6d. cleanup re-reads live position (flat snapshot, reopen live → no cancel)
# ---------------------------------------------------------------------------


def test_r3_cleanup_reread_reopened_skips_and_flat_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvise_assist.watch import _cleanup_exec_linked_quick_jv

    conn = open_db(tmp_path / "r3_cleanup.db")
    _seed_settings(conn)
    intent_btc = "aei_cleanupbtc01"
    ts = now_ms()
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")

    trade_reopen = RecordingTrade()
    trade_reopen._orders = [
        {
            "symbol": "BTCUSDT",
            "algoId": "801",
            "clientAlgoId": client_order_id(intent_btc, "sl0"),
            "reduceOnly": True,
        }
    ]
    # Pre-sync snapshot flat; live re-read shows position reopened
    trade_reopen._positions = [_Pos("BTCUSDT", "long", "0.05", "100")]
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade_reopen, None))
    cancelled = _cleanup_exec_linked_quick_jv(
        conn,
        positions=[],  # flat snapshot
        quick_info={
            "closed": [
                {
                    "symbol": "BTCUSDT",
                    "exec_linked": True,
                    "exec_intent_id": intent_btc,
                    "closed_at_ms": ts - 1_000,
                }
            ]
        },
        now_ms=ts,
    )
    assert cancelled == []
    assert not any(c[0].startswith("cancel") for c in trade_reopen.calls)

    # Positive control: live still flat → cancel
    trade_flat = RecordingTrade()
    trade_flat._orders = [
        {
            "symbol": "BTCUSDT",
            "algoId": "802",
            "clientAlgoId": client_order_id(intent_btc, "sl0"),
            "reduceOnly": True,
        }
    ]
    trade_flat._positions = []
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade_flat, None))
    cancelled2 = _cleanup_exec_linked_quick_jv(
        conn,
        positions=[],
        quick_info={
            "closed": [
                {
                    "symbol": "BTCUSDT",
                    "exec_linked": True,
                    "exec_intent_id": intent_btc,
                    "closed_at_ms": ts - 1_000,
                }
            ]
        },
        now_ms=ts,
    )
    assert cancelled2, "flat live re-read must cancel leftover jv- orders"
    conn.close()
