"""Quick Plan A1 — fix round 9 (market_attempt / audit best-effort / testnet tick)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.test_assist_quick_execute_a1 import (  # noqa: E402
    RecordingTrade,
    _daytime_ms,
    _full_cfg,
    _insert_quoted_card,
    _Pos,
    _quick_preview,
    _seed_quick_settings,
    _seed_settings,
)
from tests.test_assist_quick_execute_a1_round2 import _ey_env  # noqa: E402

from jarvise_assist.execute import (
    MSG_BINANCE_NOT_SENT,
    MSG_OUTCOME_UNKNOWN,
    _confirm_guards_quick_entry,
    _recover_entry_intent,
    client_order_id,
    finish_exec_intent,
    handle_exec_callback,
    run_entry_sequence,
)
from jarvise_assist.quick import create_quick_registration
from jarvise_assist.quick_execute import maybe_expire_quick_registration, quick_entry_proven_unfilled
from jarvise_assist.sizing import real_stop_loss_usdt, round_level_to_tick
from jarvise_assist.store import (
    claim_exec_intent,
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
)
from jarvise_ingest.db import open_db

# ---------------------------------------------------------------------------
# 1. High — market_attempt + best-effort audit + unknown / recovery
# ---------------------------------------------------------------------------


def test_r9_quick_market_log_fail_still_places_sl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(i) Quick: market fills, both market log writes raise → SL still placed; not failed."""
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "r9_i.db")
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
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.05"))

    import jarvise_assist.execute as ex
    import jarvise_assist.store as store_mod

    orig_insert = store_mod.insert_exec_log

    def _flaky_insert(conn2: Any, **kwargs: Any) -> None:
        if kwargs.get("step") == "market":
            raise RuntimeError("db down on market log")
        return orig_insert(conn2, **kwargs)

    monkeypatch.setattr(store_mod, "insert_exec_log", _flaky_insert)
    monkeypatch.setattr(ex, "insert_exec_log", _flaky_insert)

    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    assert result.status != "failed", result
    stops = [c for c in trade.calls if c[0] == "place_algo_stop" and c[1]["order_type"] == "STOP_MARKET"]
    assert len(stops) == 1
    assert stops[0][1]["qty"] == Decimal("0.05")
    assert stops[0][1]["reduce_only"] is True
    attempt = conn.execute(
        "SELECT client_order_id FROM assist_exec_log WHERE intent_id=? AND step='market_attempt'",
        (iid,),
    ).fetchone()
    assert attempt is not None
    assert str(attempt["client_order_id"]) == client_order_id(iid, "mkt")
    conn.close()


def test_r9_quick_post_send_raise_keeps_unknown_then_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(ii) Post-send audit fails + sequence raises later → unknown; recovery protects."""
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "r9_ii.db")
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

    sent: list[str] = []
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.05"))

    import jarvise_assist.execute as ex
    import jarvise_assist.store as store_mod

    orig_insert = store_mod.insert_exec_log

    def _fail_post_send(conn2: Any, **kwargs: Any) -> None:
        step = kwargs.get("step")
        if step in {"market", "stop", "tp", "market_query", "emergency_close"}:
            raise RuntimeError(f"audit fail {step}")
        return orig_insert(conn2, **kwargs)

    monkeypatch.setattr(store_mod, "insert_exec_log", _fail_post_send)
    monkeypatch.setattr(ex, "insert_exec_log", _fail_post_send)

    def _boom_after_fill(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("post-send sequence boom")

    monkeypatch.setattr(ex, "_place_and_verify_stop", _boom_after_fill)

    out = handle_exec_callback(
        conn,
        f"ey:{iid}",
        send=lambda t, **_k: sent.append(str(t)) or True,
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: None,
        trade=trade,
        now_ms_value=ts + 1_000,
    )
    assert out.get("reason") == "handler_error"
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    assert intent["status"] == "unknown", intent
    assert str(intent.get("result_text") or "") == MSG_OUTCOME_UNKNOWN
    reg = conn.execute(
        "SELECT status FROM assist_quick_registration WHERE exec_intent_id=?",
        (iid,),
    ).fetchone()
    assert reg is not None and str(reg["status"]) == "open"
    assert not quick_entry_proven_unfilled(conn, iid)
    attempt = conn.execute(
        "SELECT 1 FROM assist_exec_log WHERE intent_id=? AND step='market_attempt'",
        (iid,),
    ).fetchone()
    assert attempt is not None

    # Recovery: FOUND FILLED → places SL (_recover_entry_intent uses place_algo_stop directly).
    monkeypatch.setattr(store_mod, "insert_exec_log", orig_insert)
    monkeypatch.setattr(ex, "insert_exec_log", orig_insert)

    trade._positions = [_Pos("BTCUSDT", "long", "0.05", "100")]
    conn.execute("UPDATE assist_exec_intent SET status='recovering' WHERE id=?", (iid,))
    conn.commit()
    intent2 = get_exec_intent(conn, iid)
    assert intent2 is not None
    _recover_entry_intent(conn, trade, intent2, now_ms_value=ts + 60_000, send=None, sleep=lambda _s: None)
    intent3 = get_exec_intent(conn, iid)
    assert intent3 is not None
    assert intent3["status"] == "done", intent3
    stops = [c for c in trade.calls if c[0] == "place_algo_stop" and c[1]["order_type"] == "STOP_MARKET"]
    assert any(s[1]["qty"] == Decimal("0.05") for s in stops)
    conn.close()


def test_r9_quick_presend_attempt_fail_no_market(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(iii) Pre-send attempt write fails → no market_order, failed, MSG_BINANCE_NOT_SENT."""
    _ey_env(monkeypatch)
    conn = open_db(tmp_path / "r9_iii.db")
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
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.05"))

    import jarvise_assist.execute as ex
    import jarvise_assist.store as store_mod

    orig_insert = store_mod.insert_exec_log

    def _fail_attempt(conn2: Any, **kwargs: Any) -> None:
        if kwargs.get("step") == "market_attempt":
            raise RuntimeError("cannot persist attempt")
        return orig_insert(conn2, **kwargs)

    monkeypatch.setattr(store_mod, "insert_exec_log", _fail_attempt)
    monkeypatch.setattr(ex, "insert_exec_log", _fail_attempt)

    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    assert result.status == "failed"
    assert MSG_BINANCE_NOT_SENT in str(result)
    assert not any(c[0] == "market_order" for c in trade.calls)
    conn.close()


def test_r9_main_path_market_log_fail_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(iv) Main path: same DB failure after fill → behaviour identical to dc645f3 (no attempt)."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / "r9_iv.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    preview = {
        "qty": "0.05",
        "entry": "100",
        "stop": "99",
        "target": "101",
        "leverage": 2,
        "loss_at_stop": "0.05",
        "signal_id": "sig_r9_main",
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
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.05"))

    import jarvise_assist.execute as ex
    import jarvise_assist.store as store_mod

    orig_insert = store_mod.insert_exec_log

    def _fail_market(conn2: Any, **kwargs: Any) -> None:
        if kwargs.get("step") == "market":
            raise RuntimeError("db down on market log")
        return orig_insert(conn2, **kwargs)

    monkeypatch.setattr(store_mod, "insert_exec_log", _fail_market)
    monkeypatch.setattr(ex, "insert_exec_log", _fail_market)

    with pytest.raises(RuntimeError, match="db down on market log"):
        run_entry_sequence(conn, iid, trade=trade, origin=None, sleep=lambda _s: None)
    # Market was sent, but SL must NOT have been placed (exception aborted sequence).
    assert any(c[0] == "market_order" and not c[1].get("reduce_only") for c in trade.calls)
    stops = [c for c in trade.calls if c[0] == "place_algo_stop" and c[1]["order_type"] == "STOP_MARKET"]
    assert stops == []
    attempt = conn.execute(
        "SELECT 1 FROM assist_exec_log WHERE intent_id=? AND step='market_attempt'",
        (iid,),
    ).fetchone()
    assert attempt is None
    conn.close()


# ---------------------------------------------------------------------------
# 2. Medium — testnet remapped stop tick-rounded before sizing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("side", ["long", "short"])
def test_r9_testnet_remap_rounds_stop_before_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str
) -> None:
    """Remapped fractional stop is tick-rounded before budget; preview == submit level."""
    from jarvise_assist.sizing import SizedSignal

    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    conn = open_db(tmp_path / f"r9_tn_{side}.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    ts = _daytime_ms()

    # Preview levels at demo mark scale but off-tick; testnet %-remap with
    # mark==entry is identity, then SAFE tick round widens the stop.
    if side == "long":
        main_entry, main_stop, main_t1 = "100", "99.003", "102"
        unrounded_stop = Decimal("99.003")
        expect_rounded = round_level_to_tick(unrounded_stop, Decimal("0.01"), side="long", kind="stop")
        assert expect_rounded == Decimal("99.00")
    else:
        main_entry, main_stop, main_t1 = "100", "100.997", "98"
        unrounded_stop = Decimal("100.997")
        expect_rounded = round_level_to_tick(unrounded_stop, Decimal("0.01"), side="short", kind="stop")
        assert expect_rounded == Decimal("101.00")

    balance = Decimal("5000")
    risk_pct = Decimal("0.5")
    budget = balance * (risk_pct / Decimal("100"))  # 25
    mark = Decimal("100")
    fee = Decimal("6")
    # Preview qty that would pass budget at the unrounded stop but exceed it
    # after outward tick rounding (the pre-fix bug).
    qty = Decimal("22.35")
    loss_u = real_stop_loss_usdt(qty=qty, entry=mark, stop=unrounded_stop, fee_bps_per_side=fee)
    loss_r = real_stop_loss_usdt(qty=qty, entry=mark, stop=expect_rounded, fee_bps_per_side=fee)
    assert loss_u <= budget < loss_r, (loss_u, budget, loss_r)

    def _sized_at_preview(**kw: Any) -> SizedSignal:
        """Keep preview qty so the budget check sees the rounded stop's true loss."""
        stop = kw["stop"]
        return SizedSignal(
            symbol=kw["symbol"],
            side=kw["side"],
            entry=kw["entry"],
            stop=stop,
            target=kw["entry"],
            qty=qty,
            notional=qty * kw["entry"],
            leverage=2,
            loss_at_stop=qty * abs(kw["entry"] - stop),
            risk_pct=kw["risk_pct"],
            r_distance=abs(kw["entry"] - stop),
            leverage_capped=False,
            uncapped_leverage=2,
            analysis_id=None,
            step_size=Decimal("0.001"),
            balance_usdt=balance,
            budget_usdt=budget,
        )

    preview = _quick_preview(side=side, qty=str(qty))
    preview["entry"] = main_entry
    preview["stop"] = main_stop
    preview["t1"] = main_t1
    preview["target"] = main_t1
    preview["risk_pct_used"] = str(risk_pct)
    preview["cfg"] = _full_cfg(quick_fee_bps_per_side=fee)
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side=side,
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid)
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=mark, balance=balance, fill_qty=qty)
    monkeypatch.setattr("jarvise_assist.execute.size_signal", _sized_at_preview)
    monkeypatch.setattr("jarvise_assist.execute.below_min_notional", lambda **_: False)
    monkeypatch.setattr(
        "jarvise_assist.execute.assign_shared_leverage",
        lambda admitted, **_: (list(admitted), []),
    )
    monkeypatch.setattr(
        "jarvise_assist.execute.half_close_legs",
        lambda *_a, **_k: (Decimal("0"), Decimal("0"), "skip"),
    )

    guard = _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=ts + 1)
    assert guard == "budget_exceeded", (guard, side, expect_rounded)

    # Passing case: re-size against the rounded stop; persist that exact level.
    def _sized_fit(**kw: Any) -> SizedSignal:
        stop = kw["stop"]
        fit_qty = Decimal("22.3")
        return SizedSignal(
            symbol=kw["symbol"],
            side=kw["side"],
            entry=kw["entry"],
            stop=stop,
            target=kw["entry"],
            qty=fit_qty,
            notional=fit_qty * kw["entry"],
            leverage=2,
            loss_at_stop=fit_qty * abs(kw["entry"] - stop),
            risk_pct=kw["risk_pct"],
            r_distance=abs(kw["entry"] - stop),
            leverage_capped=False,
            uncapped_leverage=2,
            analysis_id=None,
            step_size=Decimal("0.001"),
            balance_usdt=balance,
            budget_usdt=budget,
        )

    monkeypatch.setattr("jarvise_assist.execute.size_signal", _sized_fit)
    preview2 = dict(preview)
    preview2["qty"] = "22.3"
    iid2 = insert_exec_intent(
        conn,
        kind="entry",
        symbol="ETHUSDT",
        side=side,
        preview=preview2,
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    claim_exec_intent(conn, iid2)
    intent2 = get_exec_intent(conn, iid2)
    assert intent2 is not None
    trade2 = RecordingTrade(mark=mark, balance=balance, fill_qty=Decimal("22.3"))
    assert _confirm_guards_quick_entry(conn, intent2, trade2, now_ms_value=ts + 1) is None
    updated = get_exec_intent(conn, iid2)
    assert updated is not None
    persisted = Decimal(str(updated["preview"]["stop"]))
    assert persisted == expect_rounded
    assert round_level_to_tick(persisted, Decimal("0.01"), side=side, kind="stop") == persisted
    final_qty = Decimal(str(updated["preview"]["qty"]))
    assert real_stop_loss_usdt(qty=final_qty, entry=mark, stop=persisted, fee_bps_per_side=fee) <= budget
    conn.close()


# ---------------------------------------------------------------------------
# Sanity: market_attempt is market evidence for expire gate
# ---------------------------------------------------------------------------


def test_r9_market_attempt_blocks_expire_when_unknown(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "r9_expire.db")
    _seed_settings(conn)
    ts = _daytime_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=_quick_preview(),
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market_attempt",
        endpoint="/fapi/v1/order",
        client_order_id=client_order_id(iid, "mkt"),
    )
    finish_exec_intent(conn, iid, status="unknown", result_text=MSG_OUTCOME_UNKNOWN)
    assert quick_entry_proven_unfilled(conn, iid) is False
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
    maybe_expire_quick_registration(conn, iid, now_ms=ts + 1)
    reg = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert reg is not None and str(reg["status"]) == "open"
    conn.close()
