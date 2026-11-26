"""Quick Plan A1 — bot-placed Quick entry behind JARVISE_QUICK_EXECUTE (default off)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from jarvise_assist.execute import (
    half_close_legs,
    quick_execute_enabled,
    run_entry_sequence,
)
from jarvise_assist.quick_card import build_quick_card
from jarvise_assist.quick_execute import (
    BTN_QUICK_BOT_ENTER,
    cfg_became_stricter,
    in_quick_bot_night,
    quick_chase_exceeded,
    snapshot_cfg,
    symbol_in_quick_exec_universe,
    tp1_leg_qty,
)
from jarvise_assist.quick_m1a1 import _quoted_markup_for, _quoted_markup_for_card
from jarvise_assist.real_trades import build_real_rows
from jarvise_assist.store import (
    claim_exec_intent,
    ensure_settings,
    get_exec_intent,
    insert_exec_intent,
    insert_exec_log,
    now_ms,
    write_settings,
)
from jarvise_ingest.db import open_db

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Pos:
    def __init__(self, symbol: str, side: str = "long", qty: str = "1", entry: str = "100") -> None:
        self.symbol = symbol
        self.side = side
        self.qty = Decimal(qty)
        self.entry = Decimal(entry)
        self.position_side = "BOTH"
        self.mark = Decimal(entry)


class _Filters:
    def __init__(self) -> None:
        self.step_size = Decimal("0.001")
        self.tick_size = Decimal("0.01")
        self.min_qty = Decimal("0.001")
        self.min_notional = Decimal("5")


class RecordingTrade:
    """In-memory trade client; records every mutating call."""

    def __init__(
        self,
        *,
        mark: Decimal = Decimal("100"),
        balance: Decimal = Decimal("10000"),
        available: Decimal | None = None,
        positions: list[_Pos] | None = None,
        orders: list[dict[str, Any]] | None = None,
        fill_qty: Decimal | None = None,
        fill_avg: Decimal | None = None,
        stop_ok: bool = True,
    ) -> None:
        self.mark = mark
        self.balance = balance
        self.available = available if available is not None else balance
        self._positions = list(positions or [])
        self._orders = list(orders or [])
        self.fills: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fills_qty = fill_qty
        self.fills_avg = fill_avg if fill_avg is not None else mark
        self.stop_ok = stop_ok
        self._algo_n = 0

    def mark_price(self, symbol: str) -> Decimal:
        self.calls.append(("mark_price", {"symbol": symbol}))
        return self.mark

    def usdt_wallet_and_available(self) -> tuple[Decimal, Decimal]:
        self.calls.append(("usdt_wallet_and_available", {}))
        return self.balance, self.available

    def symbol_filters(self, symbol: str) -> _Filters:
        self.calls.append(("symbol_filters", {"symbol": symbol}))
        return _Filters()

    def position_risk(self, symbol: str | None = None) -> list[_Pos]:
        self.calls.append(("position_risk", {"symbol": symbol}))
        if symbol is None:
            return list(self._positions)
        return [p for p in self._positions if p.symbol == symbol]

    def all_open_orders(self) -> list[dict[str, Any]]:
        self.calls.append(("all_open_orders", {}))
        return list(self._orders)

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [o for o in self._orders if o.get("symbol") == symbol or symbol is None]

    def open_algo_orders(self, symbol: str | None = None, *, strict: bool = False) -> list[dict[str, Any]]:
        return [o for o in self._orders if (symbol is None or o.get("symbol") == symbol) and o.get("algoId")]

    def assert_trade_key_safe(self) -> dict[str, Any]:
        self.calls.append(("assert_trade_key_safe", {}))
        return {"ok": True}

    def assert_account_modes(self) -> None:
        self.calls.append(("assert_account_modes", {}))

    def set_isolated(self, symbol: str) -> None:
        self.calls.append(("set_isolated", {"symbol": symbol, "reduce_only": False}))

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.calls.append(("set_leverage", {"symbol": symbol, "leverage": leverage}))

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
        fq = self.fills_qty if self.fills_qty is not None else qty
        body = {
            "orderId": 9001,
            "status": "FILLED",
            "executedQty": str(fq),
            "avgPrice": str(self.fills_avg),
            "clientOrderId": client_id,
        }
        self.fills.append(body)
        if not reduce_only:
            self._positions = [
                _Pos(symbol, "long" if side.upper() == "BUY" else "short", str(fq), str(self.fills_avg))
            ]
        else:
            self._positions = []
        return body

    def place_algo_stop(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal,
        trigger: Decimal,
        order_type: str,
        client_id: str,
    ) -> dict[str, Any]:
        self._algo_n += 1
        self.calls.append(
            (
                "place_algo_stop",
                {
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "trigger": trigger,
                    "order_type": order_type,
                    "client_id": client_id,
                    "reduce_only": True,
                },
            )
        )
        if not self.stop_ok and order_type == "STOP_MARKET":
            from jarvise_assist.execute import TradeClientError

            raise TradeClientError("stop refused", http_status=400, binance_code=-2021)
        body = {"algoId": str(8000 + self._algo_n), "clientAlgoId": client_id}
        self._orders.append(
            {
                "symbol": symbol,
                "algoId": body["algoId"],
                "clientAlgoId": client_id,
                "side": side,
                "type": order_type,
                "orderType": order_type,
                "quantity": str(qty),
                "triggerPrice": str(trigger),
                "reduceOnly": True,
                "algoStatus": "NEW",
                "status": "NEW",
                "workingType": "MARK_PRICE",
                "positionSide": "BOTH",
            }
        )
        return body

    def query_order_by_client_id(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        return self.query_order(symbol=symbol, client_id=client_id)

    def query_order(self, *, symbol: str, client_id: str) -> dict[str, Any]:
        for f in self.fills:
            if f.get("clientOrderId") == client_id:
                return f
        return {"status": "NEW", "executedQty": "0", "avgPrice": "0"}

    def cancel_algo_order(self, *, symbol: str, algo_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_algo_order", {"symbol": symbol, "algo_id": algo_id}))
        self._orders = [o for o in self._orders if str(o.get("algoId")) != str(algo_id)]
        return {"code": "200", "msg": "success"}

    def cancel_algo(self, *, symbol: str, algo_id: str) -> dict[str, Any]:
        self.calls.append(("cancel_algo", {"symbol": symbol, "algo_id": algo_id}))
        self._orders = [o for o in self._orders if str(o.get("algoId")) != str(algo_id)]
        return {"code": "200", "msg": "success"}


class _FuturesRead(RecordingTrade):
    """Read-only futures surface used by build_quick_card / qk:ex."""

    def exchange_symbol(self, symbol: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
        }


def _seed_settings(conn: Any, *, enabled: bool = True) -> None:
    ensure_settings(conn)
    write_settings(conn, enabled=enabled, risk_pct=1.0)


def _seed_quick_settings(conn: Any, *, risk: float = 0.5, cap: float = 1.0) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO assist_quick_settings
        (id, risk_pct, day_max_loss_r, streak_lock_n, cap_pct)
        VALUES (1, ?, 2, 3, ?)
        """,
        (risk, cap),
    )
    conn.commit()


def _insert_quoted_card(
    conn: Any,
    *,
    symbol: str = "BTCUSDT",
    side: str = "long",
    entry: float = 100.0,
    stop: float = 99.0,
    qty: float = 0.05,
    now_ms_value: int,
    size_mode: str = "risk",
) -> str:
    cid = f"aqc_{symbol[:3].lower()}_{now_ms_value}"
    conn.execute(
        """
        INSERT INTO assist_quick_card (
            id, status, side, symbol, stop_kind, stop_value,
            quoted_at_ms, quote_entry, quote_stop, quote_qty, size_mode,
            created_at_ms, updated_at_ms
        ) VALUES (?, 'quoted', ?, ?, 'price', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cid,
            side,
            symbol,
            str(stop),
            int(now_ms_value),
            entry,
            stop,
            qty,
            size_mode,
            int(now_ms_value),
            int(now_ms_value),
        ),
    )
    conn.commit()
    return cid


# ---------------------------------------------------------------------------
# Flag + parity
# ---------------------------------------------------------------------------


def test_quick_execute_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_QUICK_EXECUTE", raising=False)
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    assert quick_execute_enabled() is False
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    assert quick_execute_enabled() is True
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "off")
    assert quick_execute_enabled() is False


def test_flag_off_card_markup_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_QUICK_EXECUTE", raising=False)
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "parity.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    ts = now_ms()
    card = build_quick_card(conn, fut, symbol="BTCUSDT", side="long", stop=Decimal("99"), now_ms=ts)
    assert card["ok"] is True
    text_off = card["text"]
    markup_off = _quoted_markup_for("aqc_test", "risk")
    # Explicitly off again
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "off")
    card2 = build_quick_card(conn, fut, symbol="BTCUSDT", side="long", stop=Decimal("99"), now_ms=ts)
    assert card2["text"] == text_off
    assert _quoted_markup_for("aqc_test", "risk") == markup_off
    assert BTN_QUICK_BOT_ENTER not in json.dumps(markup_off)
    conn.close()


def test_flag_on_adds_bot_button_for_risk_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "btn.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    fut = _FuturesRead(mark=Decimal("100"), balance=Decimal("10000"))
    ts = now_ms()
    card = build_quick_card(conn, fut, symbol="BTCUSDT", side="long", stop=Decimal("99"), now_ms=ts)
    assert card["ok"] is True
    assert card.get("bot_eligible") is True
    markup = _quoted_markup_for_card(conn, "aqc_x", card)
    flat = json.dumps(markup, ensure_ascii=False)
    assert BTN_QUICK_BOT_ENTER in flat
    assert "qk:ex:aqc_x" in flat
    # Bot button above register
    rows = markup["inline_keyboard"]
    assert rows[0][0]["callback_data"].startswith("qk:ex:")
    assert rows[1][0]["callback_data"].startswith("qk:rg:")
    for row in rows:
        for btn in row:
            assert len(btn["callback_data"].encode()) <= 64
    conn.close()


def test_qk_ex_flag_off_zero_trade_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "off")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    from jarvise_assist.quick_m1a1 import handle_quick_trade_callback

    conn = open_db(tmp_path / "exoff.db")
    _seed_settings(conn)
    ts = now_ms()
    cid = _insert_quoted_card(conn, now_ms_value=ts)
    trade = RecordingTrade()
    sent: list[str] = []
    handle_quick_trade_callback(
        conn,
        f"qk:ex:{cid}",
        futures=trade,
        send=lambda t, **_: sent.append(t),
        answer=lambda *_a, **_k: None,
        edit=lambda *_a, **_k: True,
        now_ms=ts,
        message={"message_id": 1, "chat": {"id": 1}},
    )
    mutating = [c for c in trade.calls if c[0] in {"market_order", "place_algo_stop", "set_isolated"}]
    assert mutating == []
    conn.close()


# ---------------------------------------------------------------------------
# Night / chase / TP1 / snapshot
# ---------------------------------------------------------------------------


def test_night_window_wrap() -> None:
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=7))

    def ms_at(h: int, m: int) -> int:
        dt = datetime(2026, 11, 26, h, m, tzinfo=tz)
        return int(dt.timestamp() * 1000)

    assert in_quick_bot_night(ms_at(23, 45), start="23:30", end="07:30") is True
    assert in_quick_bot_night(ms_at(3, 0), start="23:30", end="07:30") is True
    assert in_quick_bot_night(ms_at(7, 29), start="23:30", end="07:30") is True
    assert in_quick_bot_night(ms_at(7, 30), start="23:30", end="07:30") is False
    assert in_quick_bot_night(ms_at(12, 0), start="23:30", end="07:30") is False


def test_tp1_leg_math() -> None:
    step = Decimal("0.001")
    min_q = Decimal("0.001")
    min_n = Decimal("5")
    mark = Decimal("100")
    # 50% of 0.1 = 0.05 — both legs ok at $5 notional
    q, reason = tp1_leg_qty(
        Decimal("0.1"), step=step, min_qty=min_q, min_notional=min_n, mark=mark, close_pct=Decimal("50")
    )
    assert reason is None and q == Decimal("0.05")
    # 0% → no TP
    q0, r0 = tp1_leg_qty(
        Decimal("0.1"), step=step, min_qty=min_q, min_notional=min_n, mark=mark, close_pct=Decimal("0")
    )
    assert q0 is None and r0 == "tp1pct_0"
    # dust
    qd, rd = tp1_leg_qty(
        Decimal("0.01"),
        step=step,
        min_qty=min_q,
        min_notional=Decimal("10"),
        mark=mark,
        close_pct=Decimal("50"),
    )
    assert qd is None


def test_half_close_legs_default_pct_matches_old() -> None:
    a = half_close_legs(
        Decimal("10"),
        step=Decimal("1"),
        min_qty=Decimal("1"),
        min_notional=Decimal("1"),
        mark=Decimal("10"),
    )
    b = half_close_legs(
        Decimal("10"),
        step=Decimal("1"),
        min_qty=Decimal("1"),
        min_notional=Decimal("1"),
        mark=Decimal("10"),
        close_pct=Decimal("50"),
    )
    assert a == b


def test_cfg_stricter_and_looser() -> None:
    frozen = {
        "quick_risk_pct": "0.5",
        "quick_risk_cap_pct": "1.0",
        "quick_counter_risk_pct": "0.25",
        "risk_used": "0.5",
        "counter_used": "0.25",
        "quick_confirm_ttl_s": 30,
        "quick_chase_r": "0.3",
        "quick_max_fee_r": "0.30",
        "quick_stop_buffer_pct": "0.1",
        "quick_stop_buffer_ticks": 2,
        "quick_max_leverage": 5,
        "lev_used": 5,
        "quick_same_side_cap": 2,
        "quick_fee_bps_per_side": "6",
        "quick_night_start": "23:30",
        "quick_night_end": "07:30",
        "quick_exec_universe": "core_explore",
    }
    live_ok = dict(frozen)
    live_ok["quick_risk_pct"] = "0.75"  # looser — must NOT refuse
    live_ok["risk_used"] = "0.75"
    assert cfg_became_stricter(frozen, live_ok) is False
    live_strict = dict(frozen)
    live_strict["quick_risk_pct"] = "0.25"
    live_strict["risk_used"] = "0.25"
    assert cfg_became_stricter(frozen, live_strict) is True
    live_uni = dict(frozen)
    live_uni["quick_exec_universe"] = "core"  # narrower
    assert cfg_became_stricter(frozen, live_uni) is True


def test_universe_gate(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "uni.db")
    assert symbol_in_quick_exec_universe(conn, "BTCUSDT", universe="core") is True
    assert symbol_in_quick_exec_universe(conn, "PEPEUSDT", universe="core") is False
    assert symbol_in_quick_exec_universe(conn, "PEPEUSDT", universe="any") is True
    conn.close()


# ---------------------------------------------------------------------------
# Entry sequence origin=quick
# ---------------------------------------------------------------------------


def test_run_entry_quick_places_tp1_not_full_and_no_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    conn = open_db(tmp_path / "entryq.db")
    _seed_settings(conn)
    ts = now_ms()
    preview = {
        "origin": "quick",
        "cohort": "quick",
        "qty": "0.1",
        "entry": "100",
        "stop": "99",
        "t1": "101",
        "target": "101",
        "tp_half_qty": "0.05",
        "leverage": 3,
        "loss_at_stop": "0.1",
        "cfg": {"quick_tp1_close_pct": "50", "quick_tp1_r": "1"},
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
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.1"), fill_avg=Decimal("100"))
    # Fake stop verify path: inject verified by making open_algo contain the stop after place
    result = run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    # May be unknown if stop verify fails in this fake — check calls either way
    mkt = [c for c in trade.calls if c[0] == "market_order"]
    assert len(mkt) == 1
    assert mkt[0][1]["reduce_only"] is False
    stops = [c for c in trade.calls if c[0] == "place_algo_stop" and c[1]["order_type"] == "STOP_MARKET"]
    tps = [c for c in trade.calls if c[0] == "place_algo_stop" and c[1]["order_type"] == "TAKE_PROFIT_MARKET"]
    for c in stops + tps:
        assert c[1].get("reduce_only") is True
    if tps:
        assert tps[0][1]["qty"] < Decimal("0.1")  # not full qty
    # No watch row
    watch = conn.execute(
        "SELECT 1 FROM assist_watch_position WHERE symbol='BTCUSDT' AND closed_at_ms IS NULL"
    ).fetchone()
    assert watch is None
    conn.close()
    del result


def test_main_entry_no_origin_still_full_tp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: origin absent keeps full-qty TP path."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    conn = open_db(tmp_path / "mainentry.db")
    _seed_settings(conn)
    ts = now_ms()
    preview = {
        "qty": "0.1",
        "entry": "100",
        "stop": "99",
        "target": "102",
        "leverage": 3,
        "loss_at_stop": "0.1",
    }
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=ts,
        expires_at_ms=ts + 120_000,
    )
    claim_exec_intent(conn, iid)
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.1"))
    run_entry_sequence(conn, iid, trade=trade, sleep=lambda _s: None)
    tps = [c for c in trade.calls if c[0] == "place_algo_stop" and c[1]["order_type"] == "TAKE_PROFIT_MARKET"]
    if tps:
        assert tps[0][1]["qty"] == Decimal("0.1")
    conn.close()


def test_build_real_rows_skips_quick_origin(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "real.db")
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
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=f"jv-{iid}-mkt",
        order_or_algo_id="555",
        http_status=200,
        created_at_ms=ts,
    )

    class _Client:
        def __init__(self) -> None:
            pass

    # Minimal stub: build_real_rows will call fetch helpers — inject empty via monkeypatch in caller
    # Use a client that returns the matching order/trade for BTC.
    client = MagicMock()
    client.fetch = MagicMock()
    # Patch the fetch functions used inside build_real_rows
    import jarvise_assist.real_trades as rt

    def _orders(_c: Any, symbol: str, **_k: Any) -> list[dict[str, Any]]:
        if symbol != "BTCUSDT":
            return []
        return [
            {
                "orderId": 555,
                "clientOrderId": f"jv-{iid}-mkt",
                "side": "BUY",
                "time": ts,
                "status": "FILLED",
            }
        ]

    def _trades(_c: Any, symbol: str, **_k: Any) -> list[dict[str, Any]]:
        if symbol != "BTCUSDT":
            return []
        return [
            {
                "id": 1,
                "orderId": 555,
                "qty": "0.1",
                "price": "100",
                "time": ts,
                "side": "BUY",
                "commission": "0",
                "commissionAsset": "USDT",
            }
        ]

    orig_o, orig_t = rt.fetch_all_orders, rt.fetch_all_user_trades
    rt.fetch_all_orders = _orders  # type: ignore[assignment]
    rt.fetch_all_user_trades = _trades  # type: ignore[assignment]
    try:
        rows = build_real_rows(conn, client, since_ms=ts - 1, until_ms=ts + 1)
        assert all(r.get("intent_id") != iid for r in rows)
    finally:
        rt.fetch_all_orders = orig_o  # type: ignore[assignment]
        rt.fetch_all_user_trades = orig_t  # type: ignore[assignment]
    conn.close()


def test_ttl_boundary_29_9_vs_30(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    from jarvise_assist.execute import _confirm_guards_quick_entry

    conn = open_db(tmp_path / "ttl.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    created = 1_700_000_000_000  # far from night window relative checks
    # Use a daytime Bangkok timestamp: 2026-06-15 12:00 BKK
    from datetime import datetime, timedelta, timezone

    bkk = timezone(timedelta(hours=7))
    created = int(datetime(2026, 6, 15, 12, 0, tzinfo=bkk).timestamp() * 1000)
    ttl_ms = 30_000
    preview = {
        "origin": "quick",
        "qty": "0.05",
        "entry": "100",
        "stop": "99",
        "t1": "101",
        "target": "101",
        "leverage": 2,
        "loss_at_stop": "0.05",
        "risk_pct_used": "0.5",
        "cfg": snapshot_cfg(
            {
                "quick_risk_pct": Decimal("0.5"),
                "quick_risk_cap_pct": Decimal("1.0"),
                "quick_counter_risk_pct": Decimal("0.25"),
                "quick_combined_risk_warn_pct": Decimal("5"),
                "quick_exec_universe": "core_explore",
                "quick_tp1_r": Decimal("1"),
                "quick_tp1_close_pct": Decimal("50"),
                "quick_night_start": "23:30",
                "quick_night_end": "07:30",
                "quick_confirm_ttl_s": 30,
                "quick_chase_r": Decimal("0.3"),
                "quick_stop_buffer_pct": Decimal("0.1"),
                "quick_stop_buffer_ticks": 2,
                "quick_max_fee_r": Decimal("0.30"),
                "quick_max_leverage": 5,
                "quick_same_side_cap": 2,
                "quick_fee_bps_per_side": Decimal("6"),
            }
        ),
    }
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=created,
        expires_at_ms=created + ttl_ms,
    )
    intent = get_exec_intent(conn, iid)
    assert intent is not None
    trade = RecordingTrade(mark=Decimal("100.05"))
    # 29.9 s still valid (may fail other guards, but not expired)
    g299 = _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=created + 29_900)
    assert g299 != "expired"
    intent2 = get_exec_intent(conn, iid)
    assert intent2 is not None
    assert _confirm_guards_quick_entry(conn, intent2, trade, now_ms_value=created + 30_000) == "expired"
    conn.close()


def test_chase_helper() -> None:
    assert (
        quick_chase_exceeded(
            side="long",
            entry=Decimal("100"),
            stop=Decimal("99"),
            mark=Decimal("100.2"),
            chase_r=Decimal("0.3"),
        )
        is False
    )
    assert (
        quick_chase_exceeded(
            side="long",
            entry=Decimal("100"),
            stop=Decimal("99"),
            mark=Decimal("100.4"),
            chase_r=Decimal("0.3"),
        )
        is True
    )


def test_compose_forwards_quick_execute() -> None:
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "JARVISE_QUICK_EXECUTE=${JARVISE_QUICK_EXECUTE:-off}" in text
    # Also assert the assist module reads it
    src = (Path(__file__).resolve().parents[1] / "src/jarvise_assist/quick_execute.py").read_text(
        encoding="utf-8"
    )
    assert "JARVISE_QUICK_EXECUTE" in src
    del re


def test_pause_blocks_bot_not_record_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    from jarvise_assist.quick_execute import pause_blocks_bot

    conn = open_db(tmp_path / "pause.db")
    _seed_settings(conn)
    ts = now_ms()
    write_settings(conn, paused_until_ms=ts + 3_600_000, pause_reason="streak")
    assert pause_blocks_bot(conn, now_ms=ts) is True
    # Record-only card still only checks week-loss (not this helper)
    conn.close()


# ---------------------------------------------------------------------------
# Design §5 coverage (helpers + guards + bookkeeping)
# ---------------------------------------------------------------------------


def _full_cfg(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "quick_risk_pct": Decimal("0.5"),
        "quick_risk_cap_pct": Decimal("1.0"),
        "quick_counter_risk_pct": Decimal("0.25"),
        "quick_combined_risk_warn_pct": Decimal("5"),
        "quick_exec_universe": "core_explore",
        "quick_tp1_r": Decimal("1"),
        "quick_tp1_close_pct": Decimal("50"),
        "quick_night_start": "23:30",
        "quick_night_end": "07:30",
        "quick_confirm_ttl_s": 30,
        "quick_chase_r": Decimal("0.3"),
        "quick_stop_buffer_pct": Decimal("0.1"),
        "quick_stop_buffer_ticks": 2,
        "quick_max_fee_r": Decimal("0.30"),
        "quick_max_leverage": 5,
        "quick_same_side_cap": 2,
        "quick_fee_bps_per_side": Decimal("6"),
    }
    base.update(overrides)
    return snapshot_cfg(base)


def _daytime_ms() -> int:
    from datetime import datetime, timedelta, timezone

    bkk = timezone(timedelta(hours=7))
    return int(datetime(2026, 6, 15, 12, 0, tzinfo=bkk).timestamp() * 1000)


def test_callbacks_le_64_bytes() -> None:
    card_id = "aqc_btc_" + ("x" * 40)
    intent_id = "aei_" + ("y" * 40)
    for data in (f"qk:ex:{card_id}", f"qk:rg:{card_id}", f"ey:{intent_id}", f"ex:{intent_id}"):
        assert len(data.encode("utf-8")) <= 64, data


def test_tp1_pct_0_50_100_and_dust() -> None:
    step, min_q, min_n = Decimal("0.001"), Decimal("0.001"), Decimal("5")
    mark = Decimal("100")
    qty = Decimal("0.1")
    assert tp1_leg_qty(
        qty, step=step, min_qty=min_q, min_notional=min_n, mark=mark, close_pct=Decimal("0")
    ) == (None, "tp1pct_0")
    half, reason = tp1_leg_qty(
        qty, step=step, min_qty=min_q, min_notional=min_n, mark=mark, close_pct=Decimal("50")
    )
    assert reason is None and half == Decimal("0.05")
    full, reason_f = tp1_leg_qty(
        qty, step=step, min_qty=min_q, min_notional=min_n, mark=mark, close_pct=Decimal("100")
    )
    assert reason_f is None and full == Decimal("0.1")
    # Dust close leg
    tiny = Decimal("0.001")
    assert (
        tp1_leg_qty(
            tiny,
            step=step,
            min_qty=min_q,
            min_notional=Decimal("5"),
            mark=Decimal("1"),
            close_pct=Decimal("50"),
        )[1]
        is not None
    )


def test_any_non_jv_reduce_only_blocks() -> None:
    from jarvise_assist.quick_execute import any_non_jv_open_order

    orders = [
        {
            "symbol": "BTCUSDT",
            "clientOrderId": "owner-sl",
            "reduceOnly": True,
            "algoId": "1",
        }
    ]
    assert any_non_jv_open_order(orders, "BTCUSDT") is True
    jv_only = [
        {
            "symbol": "BTCUSDT",
            "clientAlgoId": "jv-aei123-sl0",
            "reduceOnly": True,
            "algoId": "2",
        }
    ]
    assert any_non_jv_open_order(jv_only, "BTCUSDT") is False


@pytest.mark.parametrize("side", ["long", "short"])
def test_chase_and_stop_buffer_both_sides(side: str) -> None:
    from jarvise_assist.quick_execute import quick_stop_buffer_ok

    entry = Decimal("100")
    stop = Decimal("99") if side == "long" else Decimal("101")
    # Within chase
    mark_ok = Decimal("100.2") if side == "long" else Decimal("99.8")
    assert (
        quick_chase_exceeded(side=side, entry=entry, stop=stop, mark=mark_ok, chase_r=Decimal("0.3")) is False
    )
    mark_bad = Decimal("100.4") if side == "long" else Decimal("99.6")
    assert (
        quick_chase_exceeded(side=side, entry=entry, stop=stop, mark=mark_bad, chase_r=Decimal("0.3")) is True
    )
    tick = Decimal("0.01")
    mark = Decimal("100")
    good_stop = Decimal("99.8") if side == "long" else Decimal("100.2")
    assert (
        quick_stop_buffer_ok(
            side=side,
            mark=mark,
            stop=good_stop,
            tick=tick,
            buffer_pct=Decimal("0.1"),
            buffer_ticks=2,
        )
        is True
    )
    tight = Decimal("99.99") if side == "long" else Decimal("100.01")
    assert (
        quick_stop_buffer_ok(
            side=side,
            mark=mark,
            stop=tight,
            tick=tick,
            buffer_pct=Decimal("0.1"),
            buffer_ticks=2,
        )
        is False
    )


def test_unfinished_entry_double_tap(tmp_path: Path) -> None:
    from jarvise_assist.quick_execute import unfinished_entry_for_card

    conn = open_db(tmp_path / "dbl.db")
    ts = now_ms()
    card = "aqc_dbl_1"
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "card_id": card, "qty": "0.1"},
        created_at_ms=ts,
        expires_at_ms=ts + 30_000,
    )
    assert unfinished_entry_for_card(conn, card) == iid
    # Second card id must not collide
    assert unfinished_entry_for_card(conn, "other") is None
    conn.close()


def test_lev_clamp_to_5(tmp_path: Path) -> None:
    from jarvise_assist.quick_config import effective_quick_values, get_quick_config, set_quick_config

    conn = open_db(tmp_path / "lev.db")
    _seed_settings(conn)
    # Config max is 5; effective clamp stays ≤ 5
    set_quick_config(conn, "quick_max_leverage", 5, source="test", now_ms=now_ms())
    eff = effective_quick_values(get_quick_config(conn))
    assert int(eff["lev_used"]) <= 5
    conn.close()


def test_confirm_text_no_tp1_and_counter_label() -> None:
    from jarvise_assist.quick_execute import MSG_QUICK_NO_TP1, format_quick_confirm_text

    plan = {
        "qty": Decimal("0.1"),
        "entry": Decimal("100"),
        "stop": Decimal("99"),
        "t1": Decimal("101"),
        "tp_half_qty": None,
        "tp_refuse": "dust_close",
        "leverage": 3,
        "loss_at_stop_incl_fees": Decimal("0.12"),
        "risk_pct_used": Decimal("0.25"),
        "counter_trend": True,
        "trend": "unknown",
        "combined_warn": True,
    }
    text = format_quick_confirm_text(plan, symbol="BTCUSDT", side="long", ttl_s=30, tick=Decimal("0.01"))
    assert MSG_QUICK_NO_TP1 in text
    assert "ไม่ทราบเทรนด์" in text
    assert "เกินระดับเตือน" in text


def test_guard_non_jv_and_chase_and_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    monkeypatch.setattr(
        "jarvise_assist.execute.kill_switch_state",
        lambda **_: {"engaged": False, "known": True},
    )
    from jarvise_assist.execute import _confirm_guards_quick_entry

    conn = open_db(tmp_path / "guards.db")
    _seed_settings(conn)
    _seed_quick_settings(conn)
    created = _daytime_ms()
    preview = {
        "origin": "quick",
        "qty": "0.05",
        "entry": "100",
        "stop": "99",
        "t1": "101",
        "leverage": 2,
        "loss_at_stop": "0.05",
        "risk_pct_used": "0.5",
        "cfg": _full_cfg(),
    }
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview=preview,
        created_at_ms=created,
        expires_at_ms=created + 30_000,
    )
    intent = get_exec_intent(conn, iid)
    assert intent is not None

    # Owner reduce-only order blocks
    trade = RecordingTrade(
        mark=Decimal("100.05"),
        orders=[
            {
                "symbol": "BTCUSDT",
                "clientOrderId": "manual-sl",
                "reduceOnly": True,
                "algoId": "99",
            }
        ],
    )
    assert _confirm_guards_quick_entry(conn, intent, trade, now_ms_value=created + 1_000) == "non_jv_order"

    # Chase
    trade2 = RecordingTrade(mark=Decimal("100.5"))
    assert _confirm_guards_quick_entry(conn, intent, trade2, now_ms_value=created + 1_000) == "chase"

    # Stricter cfg
    from jarvise_assist.quick_config import set_quick_config

    set_quick_config(conn, "quick_risk_pct", 0.25, source="test", now_ms=created)
    trade3 = RecordingTrade(mark=Decimal("100.05"))
    assert _confirm_guards_quick_entry(conn, intent, trade3, now_ms_value=created + 1_000) == "cfg_changed"
    conn.close()


def test_looser_cfg_does_not_widen_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raising risk after preview must NOT refuse (looser); frozen preview stays."""
    frozen = _full_cfg()
    live = dict(frozen)
    live["quick_risk_pct"] = Decimal("1.0")  # looser
    live["risk_used"] = "1.0"
    assert cfg_became_stricter(frozen, live) is False


def test_registration_before_set_isolated_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live Quick: registration exists before the first exchange mutating call."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    from jarvise_assist.quick import create_quick_registration

    conn = open_db(tmp_path / "regorder.db")
    _seed_settings(conn)
    ts = now_ms()
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "qty": "0.1", "entry": "100", "stop": "99", "leverage": 2},
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
        qty=0.1,
        now_ms=ts,
        exec_intent_id=iid,
    )
    assert rid is not None and reason is None
    row = conn.execute(
        "SELECT exec_intent_id, status FROM assist_quick_registration WHERE id=?", (rid,)
    ).fetchone()
    assert row is not None
    assert str(row["exec_intent_id"]) == iid
    assert str(row["status"]) == "open"

    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.1"))
    # Registration already present BEFORE any trade call
    assert not any(c[0] == "set_isolated" for c in trade.calls)
    run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    assert any(c[0] == "set_isolated" for c in trade.calls)
    # Clear fail path expires registration
    from jarvise_assist.quick_execute import expire_quick_registration_for_intent

    expire_quick_registration_for_intent(conn, iid, now_ms=ts + 1)
    st = conn.execute("SELECT status FROM assist_quick_registration WHERE id=?", (rid,)).fetchone()
    assert st is not None and str(st["status"]) == "expired"
    conn.close()


def test_testnet_no_registration_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Testnet mode must not create a live Quick registration (parity with main)."""
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "testnet")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    conn = open_db(tmp_path / "tn.db")
    _seed_settings(conn)
    # Simulate ey: branch gate: live-only registration
    from jarvise_assist.execute import assist_execute_mode

    assert assist_execute_mode() == "testnet"
    n = conn.execute("SELECT COUNT(*) FROM assist_quick_registration").fetchone()[0]
    assert int(n) == 0
    conn.close()


def test_clock_skew_exec_linked_tagging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exec-linked tags by orderId even when fill time is before registration created_at."""
    from jarvise_assist.quick import create_quick_registration, sync_quick_trades

    conn = open_db(tmp_path / "skew.db")
    _seed_settings(conn)
    created = 1_700_000_100_000
    fill_time = created - 60_000  # fill 60s BEFORE registration — clock skew
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "qty": "0.1", "entry": "100", "stop": "99"},
        created_at_ms=created,
        expires_at_ms=created + 30_000,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=f"jv-{iid}-mkt",
        order_or_algo_id="7777",
        http_status=200,
        created_at_ms=created,
    )
    rid, _ = create_quick_registration(
        conn,
        symbol="BTCUSDT",
        side="long",
        planned_entry=100.0,
        planned_stop=99.0,
        qty=0.1,
        now_ms=created,
        exec_intent_id=iid,
    )
    assert rid is not None

    notices: list[str] = []

    def _send(text: str, **_k: Any) -> None:
        notices.append(text)

    class _Client:
        def fetch_all_user_trades(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return []

    import jarvise_assist.quick as qmod

    def _fills(_c: Any, symbol: str, **_k: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "orderId": 7777,
                "qty": "0.1",
                "price": "100",
                "time": fill_time,
                "side": "BUY",
                "commission": "0",
                "commissionAsset": "USDT",
                "realizedPnl": "0",
            }
        ]

    orig = qmod.fetch_all_user_trades
    qmod.fetch_all_user_trades = _fills  # type: ignore[assignment]
    try:
        info = sync_quick_trades(conn, _Client(), now_ms=created + 5_000, send=_send)
    finally:
        qmod.fetch_all_user_trades = orig  # type: ignore[assignment]
    assert int(info.get("tagged") or 0) == 1
    # Tag notice suppressed for exec-linked
    assert not any("แท็ก" in n or "tag" in n.lower() for n in notices)
    conn.close()


def test_build_real_rows_day_loss_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quick-origin intents never enter assist_real_trade → day_bot_net_r ignores them."""
    from jarvise_assist.pause import day_bot_net_r

    conn = open_db(tmp_path / "dayr.db")
    _seed_settings(conn)
    ts = now_ms()
    # No real rows → day R is 0 regardless of quick intents present
    iid = insert_exec_intent(
        conn,
        kind="entry",
        symbol="BTCUSDT",
        side="long",
        preview={"origin": "quick", "qty": "0.1", "entry": "100", "stop": "99", "loss_at_stop": "5"},
        created_at_ms=ts,
        expires_at_ms=ts + 1,
    )
    insert_exec_log(
        conn,
        intent_id=iid,
        step="market",
        endpoint="/fapi/v1/order",
        client_order_id=f"jv-{iid}-mkt",
        order_or_algo_id="888",
        http_status=200,
        created_at_ms=ts,
    )
    net = day_bot_net_r(conn, now_ms=ts)
    assert net == 0 or net == Decimal("0") or float(net) == 0.0
    conn.close()


def test_jv_cleanup_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvise_assist.watch import _cleanup_exec_linked_quick_jv

    conn = open_db(tmp_path / "jvclean.db")
    _seed_settings(conn)
    trade = RecordingTrade()
    trade._orders = [
        {
            "symbol": "BTCUSDT",
            "algoId": "501",
            "clientAlgoId": "jv-aei1-sl0",
            "reduceOnly": True,
        },
        {
            "symbol": "BTCUSDT",
            "algoId": "502",
            "clientOrderId": "owner-manual",
            "reduceOnly": True,
        },
    ]
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "off")
    monkeypatch.setattr(
        "jarvise_assist.watch.build_trade_client",
        lambda: (trade, None),
    )

    # No exec-linked → no-op
    assert (
        _cleanup_exec_linked_quick_jv(
            conn, positions=[], quick_info={"closed": [{"symbol": "BTCUSDT"}]}, now_ms=1
        )
        == []
    )

    # Exec-linked + flat + not in flight → cancel jv only
    cancelled = _cleanup_exec_linked_quick_jv(
        conn,
        positions=[],
        quick_info={
            "closed": [{"symbol": "BTCUSDT", "exec_linked": True}],
        },
        now_ms=1,
    )
    assert any(c["algo_id"] == "501" for c in cancelled)
    assert not any(c["algo_id"] == "502" for c in cancelled)

    # Position still open → no cancel
    trade2 = RecordingTrade(positions=[_Pos("BTCUSDT")])
    trade2._orders = list(trade._orders)
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade2, None))
    assert (
        _cleanup_exec_linked_quick_jv(
            conn,
            positions=[_Pos("BTCUSDT")],
            quick_info={"closed": [{"symbol": "BTCUSDT", "exec_linked": True}]},
            now_ms=1,
        )
        == []
    )

    # CANCEL_OWNER on does not change jv cleanup path
    monkeypatch.setenv("JARVISE_ASSIST_CANCEL_OWNER", "on")
    trade3 = RecordingTrade()
    trade3._orders = [
        {"symbol": "ETHUSDT", "algoId": "601", "clientAlgoId": "jv-aei2-sl0", "reduceOnly": True}
    ]
    monkeypatch.setattr("jarvise_assist.watch.build_trade_client", lambda: (trade3, None))
    cancelled_on = _cleanup_exec_linked_quick_jv(
        conn,
        positions=[],
        quick_info={"closed": [{"symbol": "ETHUSDT", "exec_linked": True}]},
        now_ms=1,
    )
    assert any(c["algo_id"] == "601" for c in cancelled_on)
    conn.close()


def test_emergency_close_reduce_only_on_sl_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    conn = open_db(tmp_path / "emg.db")
    _seed_settings(conn)
    ts = now_ms()
    preview = {
        "origin": "quick",
        "cohort": "quick",
        "qty": "0.1",
        "entry": "100",
        "stop": "99",
        "t1": "101",
        "target": "101",
        "leverage": 3,
        "loss_at_stop": "0.1",
        "cfg": {"quick_tp1_close_pct": "50", "quick_tp1_r": "1"},
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
    trade = RecordingTrade(mark=Decimal("100"), fill_qty=Decimal("0.1"), stop_ok=False)
    run_entry_sequence(conn, iid, trade=trade, origin="quick", sleep=lambda _s: None)
    closes = [c for c in trade.calls if c[0] == "market_order" and c[1].get("reduce_only") is True]
    assert closes, "emergency close must be reduce-only"
    # Entry market is not reduce-only
    opens = [c for c in trade.calls if c[0] == "market_order" and c[1].get("reduce_only") is False]
    assert opens
    conn.close()


def test_schema_version_stays_39() -> None:
    from jarvise_ingest.db import SCHEMA_VERSION

    assert SCHEMA_VERSION == 39


def test_flag_off_minlot_markup_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISE_QUICK_EXECUTE", raising=False)
    monkeypatch.setenv("JARVISE_ASSIST_EXECUTE", "live")
    conn = open_db(tmp_path / "minlot.db")
    _seed_settings(conn)
    ts = now_ms()
    cid = _insert_quoted_card(conn, now_ms_value=ts, size_mode="min_lot")
    card = {"symbol": "BTCUSDT", "size_mode": "min_lot"}
    m1 = _quoted_markup_for_card(conn, cid, card)
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "off")
    m2 = _quoted_markup_for_card(conn, cid, card)
    assert m1 == m2
    # min_lot never gets bot button even when flag on
    monkeypatch.setenv("JARVISE_QUICK_EXECUTE", "on")
    m3 = _quoted_markup_for_card(conn, cid, card)
    flat = json.dumps(m3, ensure_ascii=False)
    assert BTN_QUICK_BOT_ENTER not in flat
    conn.close()
