"""Trade assistant PR A/B — sizing, risk cap, watch, night hold."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from jarvise_assist.futures import (
    FuturesKeyUnsafeError,
    FuturesPermissionError,
    FuturesPosition,
    SymbolFilters,
    audit_key_for_assist,
)
from jarvise_assist.messages import (
    format_exit_advice,
    format_move_stop_advice,
    format_no_stop_warning,
    format_signal_message,
)
from jarvise_assist.signals import is_night_hold, run_assist_morning, run_assist_signals
from jarvise_assist.sizing import (
    ExistingIdea,
    SignalCandidate,
    SizedSignal,
    admit_signals,
    assign_shared_leverage,
    chase_exceeded,
    floor_to_step,
    next_4h_close_ms,
    next_assist_run_ms,
    passes_signal_floor,
    size_signal,
)
from jarvise_assist.store import (
    ensure_settings,
    get_edge,
    get_watch_position,
    insert_signal,
    list_held_signals,
    list_held_watch_advice,
)
from jarvise_assist.watch import run_assist_watch
from jarvise_ingest.db import open_db, upsert_analysis_output, upsert_market_technicals

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
FOUR_H_MS = 4 * 3_600_000


class FakeFutures:
    def __init__(
        self,
        *,
        balance: Decimal = Decimal("1000"),
        available: Decimal | None = None,
        marks: dict[str, Decimal] | None = None,
        steps: dict[str, Decimal] | None = None,
        min_qty: dict[str, Decimal] | None = None,
        min_notional: dict[str, Decimal] | None = None,
        open_symbols: set[str] | None = None,
        positions: list[FuturesPosition] | None = None,
        orders: list[dict[str, Any]] | None = None,
        algo_orders: list[dict[str, Any]] | None = None,
        key_safe: bool = True,
        key_error: Exception | None = None,
        read_error: Exception | None = None,
        permission_on_balance: Exception | None = None,
        income: list[dict[str, Any]] | None = None,
    ) -> None:
        self.balance = balance
        self.available = available
        self.marks = marks or {}
        self.steps = steps or {}
        self.min_qty = min_qty or {}
        self.min_notional = min_notional or {}
        self._open = open_symbols or set()
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.algo_orders = list(algo_orders or [])
        self.key_safe = key_safe
        self.key_error = key_error
        self.read_error = read_error
        self.permission_on_balance = permission_on_balance
        self.income = list(income or [])
        self.api_key = "TEST_KEY_SHOULD_NEVER_APPEAR"

    def assert_key_safe(self) -> dict[str, Any]:
        if self.key_error:
            raise self.key_error
        if not self.key_safe:
            raise FuturesKeyUnsafeError("enableWithdrawals=true")
        return {"ok_for_assist": True}

    def usdt_wallet_and_available(self) -> tuple[Decimal, Decimal]:
        if self.permission_on_balance:
            raise self.permission_on_balance
        if self.read_error:
            raise self.read_error
        avail = self.balance if self.available is None else self.available
        return self.balance, avail

    def usdt_wallet_balance(self) -> Decimal:
        return self.usdt_wallet_and_available()[0]

    def usdt_available_balance(self) -> Decimal:
        return self.usdt_wallet_and_available()[1]

    def open_symbols(self) -> set[str]:
        if self.read_error:
            raise self.read_error
        if self.positions:
            return {p.symbol for p in self.positions}
        return set(self._open)

    def position_risk(self, *, symbol: str | None = None) -> list[FuturesPosition]:
        if self.read_error:
            raise self.read_error
        if self.positions:
            out = self.positions
        else:
            out = []
            for sym in self._open:
                out.append(
                    FuturesPosition(
                        symbol=sym,
                        position_side="BOTH",
                        position_amt=Decimal("1"),
                        entry_price=Decimal("1"),
                        mark_price=self.marks.get(sym, Decimal("1")),
                        update_time_ms=1,
                    )
                )
        if symbol:
            return [p for p in out if p.symbol == symbol]
        return list(out)

    def mark_price(self, symbol: str) -> Decimal:
        return self.marks.get(symbol.upper(), Decimal("100"))

    def step_size(self, symbol: str) -> Decimal:
        return self.symbol_filters(symbol).step_size

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        sym = symbol.upper()
        return SymbolFilters(
            step_size=self.steps.get(sym, Decimal("1")),
            min_qty=self.min_qty.get(sym, Decimal("0")),
            min_notional=self.min_notional.get(sym, Decimal("0")),
        )

    def open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            return [o for o in self.orders if str(o.get("symbol")) == symbol]
        return list(self.orders)

    def open_algo_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            return [o for o in self.algo_orders if str(o.get("symbol")) == symbol]
        return list(self.algo_orders)

    def all_open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        return [*self.open_orders(symbol=symbol), *self.open_algo_orders(symbol=symbol)]

    def income_history(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        income_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        out = list(self.income)
        if income_type:
            out = [r for r in out if str(r.get("incomeType") or "") == income_type]
        if start_ms is not None:
            out = [r for r in out if int(r.get("time") or 0) >= int(start_ms)]
        if end_ms is not None:
            out = [r for r in out if int(r.get("time") or 0) <= int(end_ms)]
        return out[:limit]


def _seed_analysis(
    conn: Any,
    *,
    symbol: str,
    action: str,
    confidence: float,
    invalidation: float | None,
    ts: int = 1_700_000_000_000,
) -> None:
    upsert_analysis_output(
        conn,
        {
            "analysis_id": f"{symbol}-{ts}",
            "timestamp": ts,
            "symbol": symbol,
            "timeframe": "4h",
            "regime_state": "trend_up" if action == "long" else "trend_down",
            "confidence_score": confidence,
            "action": action,
            "invalidation_price": invalidation,
            "size_pct_equity": 1.0 if action != "flat" else 0.0,
            "thesis": "test",
        },
    )
    conn.commit()


def _ms_bangkok(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    dt = datetime(year, month, day, hour, minute, tzinfo=BANGKOK)
    return int(dt.timestamp() * 1000)


def test_floor_to_step_rounds_down() -> None:
    assert floor_to_step(Decimal("12.9"), Decimal("1")) == Decimal("12")
    assert floor_to_step(Decimal("5.75"), Decimal("0.1")) == Decimal("5.7")


def test_size_signal_2r_long_and_short() -> None:
    long_s = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert long_s is not None
    assert long_s.target == Decimal("120")  # 2R
    assert long_s.qty == Decimal("2.000")  # 20 / 10
    assert long_s.loss_at_stop == Decimal("20.00")

    short_s = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.0800"),
        stop=Decimal("0.0835"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert short_s is not None
    assert short_s.target == Decimal("0.0730")
    assert short_s.qty == Decimal("5714")  # floor(20/0.0035)


def test_leverage_cap_at_5x() -> None:
    # Tight stop → huge qty → leverage would exceed 5x.
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("99.9"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    assert sized.leverage == 5
    assert sized.leverage_capped is True
    assert sized.uncapped_leverage > 5
    assert sized.notional <= Decimal("500")


def test_passes_signal_floor() -> None:
    assert passes_signal_floor({"action": "long", "confidence_score": 0.55, "invalidation_price": 1.0})
    assert passes_signal_floor({"action": "short", "confidence_score": 0.60, "invalidation_price": 1.0})
    assert not passes_signal_floor({"action": "long", "confidence_score": 0.54, "invalidation_price": 1.0})
    assert not passes_signal_floor({"action": "flat", "confidence_score": 0.9, "invalidation_price": 1.0})
    assert not passes_signal_floor({"action": "long", "confidence_score": 0.9, "invalidation_price": None})
    # 0.70 is not required for assist.
    assert passes_signal_floor({"action": "long", "confidence_score": 0.56, "invalidation_price": 1.0})


def test_chase_limit_0_3r() -> None:
    assert chase_exceeded(
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        mark=Decimal("103.1"),
    )
    assert not chase_exceeded(
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        mark=Decimal("103.0"),
    )
    assert chase_exceeded(
        side="short",
        entry=Decimal("100"),
        stop=Decimal("110"),
        mark=Decimal("96.9"),
    )


def test_format_signal_message_style_a() -> None:
    sized = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("0.0800"),
        stop=Decimal("0.0835"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    # Expiry 20:00 Bangkok.
    expires = _ms_bangkok(2026, 10, 26, 20, 0)
    text = format_signal_message(sized, expires_at_ms=expires, balance=Decimal("1000"))
    assert "🔻 DOGEUSDT · SHORT (4h)" in text
    assert "เข้า: 0.08" in text or "เข้า: 0.0800" in text or "เข้า: 0.080" in text
    assert "Stop:" in text and "+" in text
    assert "(2R)" in text
    assert "isolated" in text
    assert "margin ≈" in text
    assert "ตั้ง Stop-Market (Reduce-Only, trigger = Mark Price) ทันทีหลังเข้า" in text
    assert "หมดอายุ 20:00" in text
    assert "api_key" not in text.lower()


def test_night_hold_window() -> None:
    assert is_night_hold(now_ms=_ms_bangkok(2026, 10, 26, 23, 30))
    assert is_night_hold(now_ms=_ms_bangkok(2026, 10, 27, 0, 0))
    assert is_night_hold(now_ms=_ms_bangkok(2026, 10, 27, 7, 29))
    assert not is_night_hold(now_ms=_ms_bangkok(2026, 10, 27, 7, 30))
    assert not is_night_hold(now_ms=_ms_bangkok(2026, 10, 27, 12, 0))


def test_no_signal_when_position_exists(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "a.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0, ts=day - 60_000)
    fake = FakeFutures(
        marks={"BTCUSDT": Decimal("100")},
        open_symbols={"BTCUSDT"},
    )
    sent: list[str] = []

    def capture(text: str) -> bool:
        sent.append(text)
        return True

    payload = run_assist_signals(conn, futures=fake, now_ms_value=day, send=capture)
    assert payload["ok"] is True
    assert sent == []
    assert any(s.get("reason") == "open_position" for s in payload["skipped"])
    conn.close()


def test_floor_blocks_flat_and_low_conf(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "b.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    _seed_analysis(conn, symbol="ETHUSDT", action="flat", confidence=0.9, invalidation=None, ts=day - 60_000)
    _seed_analysis(conn, symbol="SOLUSDT", action="long", confidence=0.5, invalidation=10.0, ts=day - 60_000)
    fake = FakeFutures(marks={"ETHUSDT": Decimal("2000"), "SOLUSDT": Decimal("100")})
    sent: list[str] = []

    def capture(text: str) -> bool:
        sent.append(text)
        return True

    payload = run_assist_signals(conn, futures=fake, now_ms_value=day, send=capture)
    assert sent == []
    reasons = {s["symbol"]: s["reason"] for s in payload["skipped"]}
    assert reasons.get("ETHUSDT") == "floor"
    assert reasons.get("SOLUSDT") == "floor"
    conn.close()


def test_night_hold_then_morning_send(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "c.db")
    night = _ms_bangkok(2026, 10, 26, 23, 45)
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.72,
        invalidation=0.0835,
        ts=night - 60_000,
    )
    fake = FakeFutures(
        balance=Decimal("1000"),
        marks={"DOGEUSDT": Decimal("0.0800")},
        steps={"DOGEUSDT": Decimal("1")},
    )
    sent: list[str] = []

    def capture(text: str) -> bool:
        sent.append(text)
        return True

    p1 = run_assist_signals(conn, futures=fake, now_ms_value=night, send=capture)
    assert p1["night_hold"] is True
    assert sent == []
    held = list_held_signals(conn)
    assert len(held) == 1
    assert held[0]["status"] == "held"
    assert held[0]["expires_at_ms"] == next_assist_run_ms(night)

    morning = _ms_bangkok(2026, 10, 27, 8, 0)
    # Keep analysis fresh for morning.
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.72,
        invalidation=0.0830,
        ts=morning - 60_000,
    )
    fake.marks["DOGEUSDT"] = Decimal("0.0795")
    p2 = run_assist_morning(conn, futures=fake, now_ms_value=morning, send=capture)
    assert p2["ok"] is True
    assert len(p2["sent"]) == 1
    assert len(sent) == 1
    assert "DOGEUSDT" in sent[0]
    assert "0.083" in sent[0]  # newest invalidation in message
    assert list_held_signals(conn) == []
    conn.close()


def test_morning_expires_on_chase(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "d.db")
    night = _ms_bangkok(2026, 10, 26, 23, 45)
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.72,
        invalidation=0.0835,
        ts=night - 60_000,
    )
    fake = FakeFutures(
        marks={"DOGEUSDT": Decimal("0.0800")},
        steps={"DOGEUSDT": Decimal("1")},
    )
    run_assist_signals(conn, futures=fake, now_ms_value=night, send=lambda _t: True)
    morning = _ms_bangkok(2026, 10, 27, 8, 0)
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.72,
        invalidation=0.0835,
        ts=morning - 60_000,
    )
    fake.marks["DOGEUSDT"] = Decimal("0.0780")
    sent: list[str] = []

    def capture(text: str) -> bool:
        sent.append(text)
        return True

    p = run_assist_morning(conn, futures=fake, now_ms_value=morning, send=capture)
    assert p["expired"]
    assert sent == []
    conn.close()


def test_stale_analysis_skipped(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "stale.db")
    day = _ms_bangkok(2026, 10, 27, 12, 25)
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="long",
        confidence=0.7,
        invalidation=90.0,
        ts=day - 9 * 3_600_000,
    )
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")}, steps={"BTCUSDT": Decimal("0.001")})
    payload = run_assist_signals(conn, futures=fake, now_ms_value=day, send=lambda _t: True)
    assert any(s.get("reason") == "stale_analysis" for s in payload["skipped"])
    conn.close()


def test_morning_dedupes_two_held(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "dup.db")
    night1 = _ms_bangkok(2026, 10, 27, 0, 25)
    night2 = _ms_bangkok(2026, 10, 27, 4, 25)
    fake = FakeFutures(
        marks={"DOGEUSDT": Decimal("0.0800")},
        steps={"DOGEUSDT": Decimal("1")},
    )
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.72,
        invalidation=0.0835,
        ts=night1 - 60_000,
    )
    run_assist_signals(conn, futures=fake, now_ms_value=night1, send=lambda _t: True)
    # Force a second held by inserting after clearing same_idea via older created_at.
    # Expire the first manually then create another held at night2.
    conn.execute("UPDATE assist_signal SET status='expired'")
    conn.commit()
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.72,
        invalidation=0.0835,
        ts=night2 - 60_000,
    )
    # Side flip then back so same_idea allows a new signal, then force two held rows.
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="long",
        confidence=0.72,
        invalidation=0.0760,
        ts=night2 - 120_000,
    )
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.72,
        invalidation=0.0835,
        ts=night2 - 60_000,
    )
    run_assist_signals(conn, futures=fake, now_ms_value=night2, send=lambda _t: True)
    # Insert a second held row with older created_at to simulate the pre-fix race.
    held = list_held_signals(conn)
    assert len(held) >= 1
    keep = held[0]
    conn.execute(
        """
        INSERT INTO assist_signal (
            id, analysis_id, symbol, side, entry, stop, target, qty, notional,
            leverage, loss_at_stop, created_at_ms, expires_at_ms, status, sent_at_ms,
            leverage_capped, uncapped_leverage
        ) VALUES (
            'as_old_dup', ?, 'DOGEUSDT', 'short', 0.08, 0.0835, 0.073, 5714, 457,
            1, 20, ?, ?, 'held', NULL, 0, 1
        )
        """,
        (keep.get("analysis_id"), night1, keep["expires_at_ms"]),
    )
    conn.commit()
    assert len(list_held_signals(conn)) == 2

    morning = _ms_bangkok(2026, 10, 27, 8, 0)
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.72,
        invalidation=0.0835,
        ts=morning - 60_000,
    )
    sent: list[str] = []

    def capture(text: str) -> bool:
        sent.append(text)
        return True

    p = run_assist_morning(conn, futures=fake, now_ms_value=morning, send=capture)
    assert len(p["sent"]) == 1
    assert "as_old_dup" in p["expired"]
    assert len(sent) == 1
    conn.close()


def test_same_idea_skips_then_allows_after_24h_or_side_change(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "idea.db")
    t0 = _ms_bangkok(2026, 10, 27, 12, 25)
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")}, steps={"BTCUSDT": Decimal("0.001")})
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0, ts=t0 - 60_000)
    p0 = run_assist_signals(conn, futures=fake, now_ms_value=t0, send=lambda _t: True)
    assert len(p0["created"]) == 1

    t1 = t0 + 4 * 3_600_000
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="long",
        confidence=0.7,
        invalidation=91.0,
        ts=t1 - 60_000,
    )
    # Previous bar same side + recent signal → same_idea
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="long",
        confidence=0.7,
        invalidation=90.0,
        ts=t1 - 4 * 3_600_000 - 60_000,
    )
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="long",
        confidence=0.7,
        invalidation=91.0,
        ts=t1 - 60_000,
    )
    p1 = run_assist_signals(conn, futures=fake, now_ms_value=t1, send=lambda _t: True)
    assert any(s.get("reason") == "same_idea" for s in p1["skipped"])

    # Side change allows a new signal inside 24h.
    t2 = t1 + 4 * 3_600_000
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="long",
        confidence=0.7,
        invalidation=91.0,
        ts=t2 - 4 * 3_600_000 - 60_000,
    )
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="short",
        confidence=0.7,
        invalidation=110.0,
        ts=t2 - 60_000,
    )
    p2 = run_assist_signals(conn, futures=fake, now_ms_value=t2, send=lambda _t: True)
    assert any(c.get("symbol") == "BTCUSDT" for c in p2["created"])

    # After 24h same side may send again.
    t3 = t2 + 25 * 3_600_000
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="short",
        confidence=0.7,
        invalidation=110.0,
        ts=t3 - 4 * 3_600_000 - 60_000,
    )
    _seed_analysis(
        conn,
        symbol="BTCUSDT",
        action="short",
        confidence=0.7,
        invalidation=111.0,
        ts=t3 - 60_000,
    )
    p3 = run_assist_signals(conn, futures=fake, now_ms_value=t3, send=lambda _t: True)
    assert any(c.get("symbol") == "BTCUSDT" for c in p3["created"])
    conn.close()


def test_expiry_equals_next_assist_run() -> None:
    now = _ms_bangkok(2026, 10, 27, 12, 25)
    nxt = next_assist_run_ms(now)
    assert nxt == _ms_bangkok(2026, 10, 27, 16, 25)
    assert next_assist_run_ms(_ms_bangkok(2026, 10, 27, 20, 25)) == _ms_bangkok(2026, 10, 28, 0, 25)


def test_permission_refusal_before_fapi() -> None:
    audit = audit_key_for_assist(
        {
            "enableReading": True,
            "enableWithdrawals": True,
            "enableInternalTransfer": False,
            "permitsUniversalTransfer": False,
            "enableSpotAndMarginTrading": False,
            "enableFutures": False,
        }
    )
    assert audit["ok_for_assist"] is False
    audit2 = audit_key_for_assist(
        {
            "enableReading": True,
            "enableWithdrawals": False,
            "enableInternalTransfer": False,
            "permitsUniversalTransfer": False,
            "enableSpotAndMarginTrading": True,
            "enableFutures": False,
        }
    )
    assert audit2["ok_for_assist"] is False
    audit3 = audit_key_for_assist(
        {
            "enableReading": True,
            "enableWithdrawals": False,
            "enableInternalTransfer": False,
            "permitsUniversalTransfer": False,
            "enableSpotAndMarginTrading": False,
            "enableFutures": True,
        }
    )
    assert audit3["ok_for_assist"] is False


def test_key_unsafe_alert_once(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "e.db")
    fake = FakeFutures(key_safe=False)
    sent: list[str] = []

    def capture(text: str) -> bool:
        sent.append(text)
        return True

    p1 = run_assist_signals(
        conn,
        futures=fake,
        now_ms_value=_ms_bangkok(2026, 10, 27, 12, 0),
        send=capture,
    )
    assert p1["ok"] is False
    assert p1["error"] == "key_unsafe"
    assert len(sent) == 1
    assert get_edge(conn)["key_unsafe_reported"] is True
    p2 = run_assist_signals(
        conn,
        futures=fake,
        now_ms_value=_ms_bangkok(2026, 10, 27, 12, 5),
        send=capture,
    )
    assert p2["error"] == "key_unsafe"
    assert len(sent) == 1
    conn.close()


def test_permission_flag_only_on_successful_send_and_clears(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "f.db")
    ensure_settings(conn)
    fake = FakeFutures(permission_on_balance=FuturesPermissionError("code=-2015"))
    p_fail = run_assist_signals(
        conn,
        futures=fake,
        now_ms_value=_ms_bangkok(2026, 10, 27, 12, 0),
        send=lambda _t: False,
    )
    assert p_fail["error"] == "permission_error"
    assert get_edge(conn)["permission_error_reported"] is False

    sent: list[str] = []

    def capture(text: str) -> bool:
        sent.append(text)
        return True

    p_ok = run_assist_signals(
        conn,
        futures=fake,
        now_ms_value=_ms_bangkok(2026, 10, 27, 12, 5),
        send=capture,
    )
    assert p_ok["error"] == "permission_error"
    assert get_edge(conn)["permission_error_reported"] is True
    assert len(sent) == 1

    # Recovery clears flags after a successful futures read.
    fake.permission_on_balance = None
    day = _ms_bangkok(2026, 10, 27, 12, 25)
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0, ts=day - 60_000)
    fake.marks["BTCUSDT"] = Decimal("100")
    fake.steps["BTCUSDT"] = Decimal("0.001")
    run_assist_signals(conn, futures=fake, now_ms_value=day, send=capture)
    edge = get_edge(conn)
    assert edge["permission_error_reported"] is False
    assert edge["key_unsafe_reported"] is False
    conn.close()


def test_payload_never_contains_api_key(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "g.db")
    day = _ms_bangkok(2026, 10, 27, 12, 25)
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0, ts=day - 60_000)
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")}, steps={"BTCUSDT": Decimal("0.001")})
    payload = run_assist_signals(
        conn,
        futures=fake,
        now_ms_value=day,
        send=lambda _t: True,
    )
    blob = json.dumps(payload)
    assert "TEST_KEY" not in blob
    assert "api_key" not in blob.lower()
    conn.close()


def test_migration_14_creates_assist_tables(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "h.db")
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'assist_%'"
        ).fetchall()
    }
    assert tables >= {
        "assist_settings",
        "assist_edge",
        "assist_signal",
        "assist_track",
        "assist_position_seen",
        "assist_watch_position",
        "assist_watch_advice",
    }
    settings = ensure_settings(conn)
    assert settings["risk_pct"] == 1.0
    assert settings["enabled"] is True
    conn.close()


def test_next_4h_close_ms() -> None:
    # 2026-10-27 10:00 UTC → next boundary 12:00 UTC
    ts = int(datetime(2026, 10, 27, 10, 0, tzinfo=UTC).timestamp() * 1000)
    nxt = next_4h_close_ms(ts)
    assert datetime.fromtimestamp(nxt / 1000, tz=UTC).hour == 12


ONE_H_MS = 3_600_000


def _seed_candle(
    conn: Any,
    *,
    symbol: str,
    ts: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    atr: float,
    timeframe: str = "4h",
) -> None:
    upsert_market_technicals(
        conn,
        [
            {
                "symbol": symbol,
                "timestamp": ts,
                "timeframe": timeframe,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1.0,
            }
        ],
    )
    conn.execute(
        "UPDATE market_technicals SET atr_14=? WHERE symbol=? AND timeframe=? AND timestamp=?",
        (atr, symbol, timeframe, ts),
    )
    conn.commit()


def test_admit_signals_stops_after_first_reject() -> None:
    sized_hi = size_signal(
        symbol="ADAUSDT",
        side="short",
        entry=Decimal("1"),
        stop=Decimal("1.02"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    sized_mid = size_signal(
        symbol="DOGEUSDT",
        side="short",
        entry=Decimal("1"),
        stop=Decimal("1.02"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    sized_lo = size_signal(
        symbol="XRPUSDT",
        side="short",
        entry=Decimal("1"),
        stop=Decimal("1.02"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized_hi and sized_mid and sized_lo
    existing = [ExistingIdea(symbol="BTCUSDT", side="long", risk_usdt=Decimal("2"))]
    admitted, rejected = admit_signals(
        existing=existing,
        candidates=[
            SignalCandidate(sized=sized_lo, confidence=0.56, symbol_rank=6),
            SignalCandidate(sized=sized_hi, confidence=0.80, symbol_rank=4),
            SignalCandidate(sized=sized_mid, confidence=0.70, symbol_rank=5),
        ],
        balance=Decimal("100"),
    )
    assert [c.sized.symbol for c in admitted] == ["ADAUSDT"]
    assert [c.sized.symbol for c in rejected] == ["DOGEUSDT", "XRPUSDT"]


def test_risk_cap_sends_highest_confidence_only(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "risk.db")
    ensure_settings(conn)
    day = _ms_bangkok(2026, 10, 27, 12, 25)
    # One existing open idea occupies a slot.
    existing = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert existing is not None
    insert_signal(
        conn,
        existing,
        status="sent",
        created_at_ms=day - 60_000,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=day - 60_000,
    )
    for sym, conf, inv in (
        ("ADAUSDT", 0.80, 0.42),
        ("DOGEUSDT", 0.70, 0.21),
        ("XRPUSDT", 0.60, 2.6),
    ):
        _seed_analysis(conn, symbol=sym, action="short", confidence=conf, invalidation=inv, ts=day - 60_000)
    fake = FakeFutures(
        balance=Decimal("100"),
        marks={
            "ADAUSDT": Decimal("0.40"),
            "DOGEUSDT": Decimal("0.20"),
            "XRPUSDT": Decimal("2.5"),
        },
        steps={
            "ADAUSDT": Decimal("1"),
            "DOGEUSDT": Decimal("1"),
            "XRPUSDT": Decimal("0.1"),
        },
    )
    payload = run_assist_signals(conn, futures=fake, now_ms_value=day, send=lambda _t: True)
    created_syms = [c["symbol"] for c in payload["created"]]
    assert created_syms == ["ADAUSDT"]
    risk_skips = [s for s in payload["skipped"] if s.get("reason") == "risk_cap"]
    assert {s["symbol"] for s in risk_skips} == {"DOGEUSDT", "XRPUSDT"}
    conn.close()


def test_below_min_notional_does_not_raise_qty(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "min.db")
    day = _ms_bangkok(2026, 10, 27, 12, 25)
    _seed_analysis(conn, symbol="ADAUSDT", action="short", confidence=0.7, invalidation=0.42, ts=day - 60_000)
    fake = FakeFutures(
        balance=Decimal("100"),
        marks={"ADAUSDT": Decimal("0.40")},
        steps={"ADAUSDT": Decimal("1")},
        min_notional={"ADAUSDT": Decimal("500")},
    )
    payload = run_assist_signals(conn, futures=fake, now_ms_value=day, send=lambda _t: True)
    skips = [s for s in payload["skipped"] if s.get("reason") == "below_min_notional"]
    assert len(skips) == 1
    assert skips[0]["min_notional"] == 500.0
    assert skips[0]["notional"] < 500.0
    assert payload["created"] == []
    conn.close()


def test_format_watch_messages() -> None:
    assert (
        format_no_stop_warning(symbol="ADAUSDT", side="long", stop=Decimal("0.32"))
        == "⚠️ ADAUSDT LONG ไม่มี stop order — ตั้ง Stop-Market (Reduce-Only, Mark Price) ที่ 0.32"
    )
    assert (
        format_move_stop_advice(symbol="ADAUSDT", side="long", stop=Decimal("0.35"))
        == "↗️ ADAUSDT LONG · เลื่อน stop → 0.35"
    )
    assert (
        format_move_stop_advice(symbol="ADAUSDT", side="short", stop=Decimal("0.48"))
        == "↘️ ADAUSDT SHORT · เลื่อน stop → 0.48"
    )
    text = format_exit_advice(symbol="ADAUSDT", side="short", reason="flat", approx_r=Decimal("0.4"))
    assert text == "✅ ADAUSDT SHORT · ออกตอนนี้\nสัญญาณ 4h เป็น FLAT · ประมาณ 0.4R"


def test_no_stop_warning_once_then_rearms(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "nostop.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    entry_ts = day - FOUR_H_MS
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=entry_ts - FOUR_H_MS,
        open_=0.40,
        high=0.41,
        low=0.39,
        close=0.40,
        atr=0.02,
    )
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.40"),
        update_time_ms=entry_ts,
    )
    fake = FakeFutures(positions=[pos], marks={"ADAUSDT": Decimal("0.40")})
    sent: list[str] = []
    p1 = run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert any(a.get("action") == "no_stop" for a in p1["actions"])
    assert len(sent) == 1
    assert "ไม่มี stop order" in sent[0]
    run_assist_watch(conn, futures=fake, now_ms_value=day + 60_000, send=lambda t: sent.append(t) or True)
    assert len(sent) == 1
    # Stop appears then disappears → re-arm (long closes with SELL stop).
    fake.orders = [
        {
            "symbol": "ADAUSDT",
            "side": "SELL",
            "type": "STOP_MARKET",
            "reduceOnly": True,
            "positionSide": "BOTH",
            "stopPrice": "0.37",
        }
    ]
    run_assist_watch(conn, futures=fake, now_ms_value=day + 120_000, send=lambda t: sent.append(t) or True)
    fake.orders = []
    run_assist_watch(conn, futures=fake, now_ms_value=day + 180_000, send=lambda t: sent.append(t) or True)
    assert len(sent) == 2
    conn.close()


def test_algo_stop_counts_as_protective_stop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "algo.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    entry_ts = day - FOUR_H_MS
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=entry_ts - FOUR_H_MS,
        open_=0.40,
        high=0.41,
        low=0.39,
        close=0.40,
        atr=0.02,
    )
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.40"),
        update_time_ms=entry_ts,
    )
    fake = FakeFutures(
        positions=[pos],
        algo_orders=[
            {
                "symbol": "ADAUSDT",
                "side": "BUY",
                "orderType": "STOP_MARKET",
                "reduceOnly": True,
                "positionSide": "BOTH",
                "triggerPrice": "0.43",
            }
        ],
    )
    sent: list[str] = []
    payload = run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert not any(a.get("action") == "no_stop" for a in payload["actions"])
    assert sent == []
    conn.close()


def test_taken_links_signal_stop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "taken.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    sized = size_signal(
        symbol="ADAUSDT",
        side="short",
        entry=Decimal("0.40"),
        stop=Decimal("0.43"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    sid = insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=day - 60_000,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=day - 60_000,
    )
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.40"),
        update_time_ms=day - 30_000,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "ADAUSDT",
                "side": "BUY",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "0.43",
            }
        ],
    )
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda _t: True)
    row = get_watch_position(conn, "ADAUSDT", "BOTH")
    assert row is not None
    assert row["signal_id"] == sid
    assert row["initial_stop_source"] == "signal"
    assert float(row["initial_stop"]) == 0.43
    status = conn.execute("SELECT status FROM assist_signal WHERE id=?", (sid,)).fetchone()[0]
    assert status == "taken"
    conn.close()


def test_off_signal_stop_is_1_5_atr(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "atr.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    entry_ts = day - FOUR_H_MS
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=entry_ts - FOUR_H_MS,
        open_=0.40,
        high=0.41,
        low=0.39,
        close=0.40,
        atr=0.02,
    )
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-50"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.40"),
        update_time_ms=entry_ts,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "ADAUSDT",
                "side": "BUY",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "0.43",
            }
        ],
    )
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda _t: True)
    row = get_watch_position(conn, "ADAUSDT", "BOTH")
    assert row is not None
    assert row["initial_stop_source"] == "atr"
    # short: entry + 1.5 * 0.02 = 0.43
    assert abs(float(row["initial_stop"]) - 0.43) < 1e-9
    conn.close()


def test_move_stop_once_per_level(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "move.db")
    day = _ms_bangkok(2026, 10, 27, 14, 0)
    entry_ts = day - 2 * ONE_H_MS
    # 1h candle opened at entry; favorable high for long (>= 0.5R).
    # entry 100, initial stop 90, R=10; high 106 → breakeven ~100.2
    _seed_candle(
        conn,
        symbol="BTCUSDT",
        ts=entry_ts,
        open_=100,
        high=106,
        low=99,
        close=105,
        atr=6.67,
        timeframe="1h",
    )
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("105"),
        update_time_ms=entry_ts,
    )
    # Link via signal stop 90
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "90",
            }
        ],
        marks={"BTCUSDT": Decimal("105")},
    )
    sent: list[str] = []
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert len(sent) == 1
    assert "เลื่อน stop" in sent[0]
    run_assist_watch(conn, futures=fake, now_ms_value=day + 60_000, send=lambda t: sent.append(t) or True)
    assert len(sent) == 1
    conn.close()


def test_pre_entry_extreme_does_not_move_stop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "pre.db")
    day = _ms_bangkok(2026, 10, 27, 14, 0)
    entry_ts = day - ONE_H_MS
    # Bar opened before entry with high past entry+1R must not drive trail.
    _seed_candle(
        conn,
        symbol="BTCUSDT",
        ts=entry_ts - ONE_H_MS,
        open_=100,
        high=115,
        low=99,
        close=114,
        atr=6.67,
        timeframe="1h",
    )
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("101"),
        update_time_ms=entry_ts,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "90",
            }
        ],
        marks={"BTCUSDT": Decimal("101")},
    )
    sent: list[str] = []
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert not any("เลื่อน stop" in t for t in sent)
    conn.close()


def test_1h_candle_after_entry_moves_stop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "post1h.db")
    day = _ms_bangkok(2026, 10, 27, 14, 0)
    entry_ts = day - 2 * ONE_H_MS
    _seed_candle(
        conn,
        symbol="BTCUSDT",
        ts=entry_ts,
        open_=100,
        high=111,
        low=99,
        close=110,
        atr=6.67,
        timeframe="1h",
    )
    sized = size_signal(
        symbol="BTCUSDT",
        side="long",
        entry=Decimal("100"),
        stop=Decimal("90"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.001"),
    )
    assert sized is not None
    insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    pos = FuturesPosition(
        symbol="BTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("0.1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("110"),
        update_time_ms=entry_ts,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "90",
            }
        ],
        marks={"BTCUSDT": Decimal("110")},
    )
    sent: list[str] = []
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert len(sent) == 1
    assert "เลื่อน stop" in sent[0]
    conn.close()


def test_no_stop_still_sends_exit_on_flat_and_2r(tmp_path: Path) -> None:
    # Flat exit with no protective order.
    conn = open_db(tmp_path / "nostop_exit.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    entry_ts = day - FOUR_H_MS
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=entry_ts - FOUR_H_MS,
        open_=0.40,
        high=0.41,
        low=0.39,
        close=0.40,
        atr=0.02,
    )
    sized = size_signal(
        symbol="ADAUSDT",
        side="short",
        entry=Decimal("0.40"),
        stop=Decimal("0.43"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    _seed_analysis(conn, symbol="ADAUSDT", action="flat", confidence=0.9, invalidation=None, ts=day - 60_000)
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.39"),
        update_time_ms=entry_ts,
    )
    fake = FakeFutures(positions=[pos], orders=[], marks={"ADAUSDT": Decimal("0.39")})
    sent: list[str] = []
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert any("ไม่มี stop order" in t for t in sent)
    assert any("FLAT" in t for t in sent)
    assert sum(1 for t in sent if "ไม่มี stop order" in t) == 1
    conn.close()

    # 2R with no protective order.
    conn2 = open_db(tmp_path / "nostop_2r.db")
    _seed_candle(
        conn2,
        symbol="XRPUSDT",
        ts=entry_ts - FOUR_H_MS,
        open_=2.5,
        high=2.6,
        low=2.4,
        close=2.5,
        atr=0.1,
    )
    sized2 = size_signal(
        symbol="XRPUSDT",
        side="long",
        entry=Decimal("2.5"),
        stop=Decimal("2.4"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.1"),
    )
    assert sized2 is not None
    insert_signal(
        conn2,
        sized2,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    _seed_analysis(conn2, symbol="XRPUSDT", action="long", confidence=0.7, invalidation=2.4, ts=day - 60_000)
    pos2 = FuturesPosition(
        symbol="XRPUSDT",
        position_side="BOTH",
        position_amt=Decimal("10"),
        entry_price=Decimal("2.5"),
        mark_price=Decimal("2.7"),
        update_time_ms=entry_ts,
    )
    fake2 = FakeFutures(positions=[pos2], orders=[], marks={"XRPUSDT": Decimal("2.7")})
    sent2: list[str] = []
    run_assist_watch(conn2, futures=fake2, now_ms_value=day, send=lambda t: sent2.append(t) or True)
    assert any("ไม่มี stop order" in t for t in sent2)
    assert any("2R" in t for t in sent2)
    conn2.close()


def test_hedge_short_stop_without_reduce_only(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "hedge.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    entry_ts = day - FOUR_H_MS
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=entry_ts - FOUR_H_MS,
        open_=0.40,
        high=0.41,
        low=0.39,
        close=0.40,
        atr=0.02,
    )
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="SHORT",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.40"),
        update_time_ms=entry_ts,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "ADAUSDT",
                "side": "BUY",
                "type": "STOP_MARKET",
                "positionSide": "SHORT",
                "stopPrice": "0.43",
            }
        ],
    )
    sent: list[str] = []
    payload = run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert not any(a.get("action") == "no_stop" for a in payload["actions"])
    assert not any("ไม่มี stop order" in t for t in sent)
    conn.close()


def test_exit_on_flat_opposite_and_2r(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "exit.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    entry_ts = day - FOUR_H_MS
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=entry_ts - FOUR_H_MS,
        open_=0.40,
        high=0.41,
        low=0.39,
        close=0.40,
        atr=0.02,
    )
    sized = size_signal(
        symbol="ADAUSDT",
        side="short",
        entry=Decimal("0.40"),
        stop=Decimal("0.43"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    stop_order = {
        "symbol": "ADAUSDT",
        "side": "BUY",
        "type": "STOP_MARKET",
        "reduceOnly": True,
        "stopPrice": "0.43",
    }

    # Flat exit
    _seed_analysis(conn, symbol="ADAUSDT", action="flat", confidence=0.9, invalidation=None, ts=day - 60_000)
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.39"),
        update_time_ms=entry_ts,
    )
    fake = FakeFutures(positions=[pos], orders=[stop_order], marks={"ADAUSDT": Decimal("0.39")})
    sent: list[str] = []
    run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert any("FLAT" in t for t in sent)

    # Opposite on a fresh symbol/position
    conn2 = open_db(tmp_path / "exit2.db")
    _seed_candle(
        conn2,
        symbol="DOGEUSDT",
        ts=entry_ts - FOUR_H_MS,
        open_=0.20,
        high=0.21,
        low=0.19,
        close=0.20,
        atr=0.01,
    )
    sized2 = size_signal(
        symbol="DOGEUSDT",
        side="long",
        entry=Decimal("0.20"),
        stop=Decimal("0.185"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized2 is not None
    insert_signal(
        conn2,
        sized2,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    _seed_analysis(
        conn2, symbol="DOGEUSDT", action="short", confidence=0.7, invalidation=0.22, ts=day - 60_000
    )
    pos2 = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("100"),
        entry_price=Decimal("0.20"),
        mark_price=Decimal("0.20"),
        update_time_ms=entry_ts,
    )
    fake2 = FakeFutures(
        positions=[pos2],
        orders=[
            {
                "symbol": "DOGEUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "0.185",
            }
        ],
    )
    sent2: list[str] = []
    run_assist_watch(conn2, futures=fake2, now_ms_value=day, send=lambda t: sent2.append(t) or True)
    assert any("กลับด้าน" in t for t in sent2)

    # 2R target
    conn3 = open_db(tmp_path / "exit3.db")
    _seed_candle(
        conn3,
        symbol="XRPUSDT",
        ts=entry_ts - FOUR_H_MS,
        open_=2.5,
        high=2.6,
        low=2.4,
        close=2.5,
        atr=0.1,
    )
    sized3 = size_signal(
        symbol="XRPUSDT",
        side="long",
        entry=Decimal("2.5"),
        stop=Decimal("2.4"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("0.1"),
    )
    assert sized3 is not None
    insert_signal(
        conn3,
        sized3,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=day + 3_600_000,
        sent_at_ms=entry_ts,
    )
    _seed_analysis(conn3, symbol="XRPUSDT", action="long", confidence=0.7, invalidation=2.4, ts=day - 60_000)
    pos3 = FuturesPosition(
        symbol="XRPUSDT",
        position_side="BOTH",
        position_amt=Decimal("10"),
        entry_price=Decimal("2.5"),
        mark_price=Decimal("2.7"),  # +2R (R=0.1)
        update_time_ms=entry_ts,
    )
    fake3 = FakeFutures(
        positions=[pos3],
        orders=[
            {
                "symbol": "XRPUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "2.4",
            }
        ],
        marks={"XRPUSDT": Decimal("2.7")},
    )
    sent3: list[str] = []
    run_assist_watch(conn3, futures=fake3, now_ms_value=day, send=lambda t: sent3.append(t) or True)
    assert any("2R" in t for t in sent3)
    conn.close()
    conn2.close()
    conn3.close()


def test_morning_drops_held_advice_when_position_closed(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "drop.db")
    night = _ms_bangkok(2026, 10, 27, 0, 0)
    morning = _ms_bangkok(2026, 10, 27, 8, 0)
    entry_ts = night - FOUR_H_MS
    _seed_candle(
        conn,
        symbol="ADAUSDT",
        ts=entry_ts - FOUR_H_MS,
        open_=0.40,
        high=0.41,
        low=0.39,
        close=0.40,
        atr=0.02,
    )
    sized = size_signal(
        symbol="ADAUSDT",
        side="short",
        entry=Decimal("0.40"),
        stop=Decimal("0.43"),
        balance=Decimal("100"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    insert_signal(
        conn,
        sized,
        status="sent",
        created_at_ms=entry_ts,
        expires_at_ms=morning + 3_600_000,
        sent_at_ms=entry_ts,
    )
    _seed_analysis(
        conn, symbol="ADAUSDT", action="flat", confidence=0.9, invalidation=None, ts=night - 60_000
    )
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.39"),
        update_time_ms=entry_ts,
    )
    fake = FakeFutures(
        positions=[pos],
        orders=[
            {
                "symbol": "ADAUSDT",
                "side": "BUY",
                "type": "STOP_MARKET",
                "reduceOnly": True,
                "stopPrice": "0.43",
            }
        ],
    )
    sent: list[str] = []
    run_assist_watch(conn, futures=fake, now_ms_value=night, send=lambda t: sent.append(t) or True)
    assert sent == []  # night hold
    held = list_held_watch_advice(conn)
    assert held
    # Position closed by morning.
    fake.positions = []
    payload = run_assist_morning(
        conn, futures=fake, now_ms_value=morning, send=lambda t: sent.append(t) or True
    )
    assert payload["watch_advice"]["dropped"]
    assert sent == []
    conn.close()


def test_outside_universe_is_stored_without_advice(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "out.db")
    day = _ms_bangkok(2026, 10, 27, 12, 0)
    pos = FuturesPosition(
        symbol="LTCUSDT",
        position_side="BOTH",
        position_amt=Decimal("1"),
        entry_price=Decimal("80"),
        mark_price=Decimal("80"),
        update_time_ms=day,
    )
    fake = FakeFutures(positions=[pos])
    sent: list[str] = []
    payload = run_assist_watch(conn, futures=fake, now_ms_value=day, send=lambda t: sent.append(t) or True)
    assert sent == []
    assert any(a.get("action") == "outside_universe" for a in payload["actions"])
    row = get_watch_position(conn, "LTCUSDT", "BOTH")
    assert row is not None
    assert int(row["in_universe"]) == 0
    conn.close()


def test_unsent_held_or_expired_signal_does_not_block_resend(tmp_path: Path) -> None:
    """Regression 2026-10-27: an idea the owner never saw must not count as same_idea."""
    conn = open_db(tmp_path / "unsent.db")
    night = _ms_bangkok(2026, 10, 27, 0, 25)
    fake = FakeFutures(marks={"DOGEUSDT": Decimal("0.0800")}, steps={"DOGEUSDT": Decimal("1")})
    bar = 4 * 3_600_000
    _seed_analysis(
        conn, symbol="DOGEUSDT", action="short", confidence=0.75, invalidation=0.0835, ts=night - bar - 60_000
    )
    _seed_analysis(
        conn, symbol="DOGEUSDT", action="short", confidence=0.75, invalidation=0.0835, ts=night - 60_000
    )
    p0 = run_assist_signals(conn, futures=fake, now_ms_value=night, send=lambda _t: True)
    assert [c["status"] for c in p0["created"]] == ["held"]
    conn.execute("UPDATE assist_signal SET status='expired'")
    conn.commit()

    day = _ms_bangkok(2026, 10, 27, 8, 25)
    _seed_analysis(
        conn, symbol="DOGEUSDT", action="short", confidence=0.75, invalidation=0.0835, ts=day - 60_000
    )
    p1 = run_assist_signals(conn, futures=fake, now_ms_value=day, send=lambda _t: True)
    assert not any(s.get("reason") == "same_idea" for s in p1["skipped"])
    assert [c["status"] for c in p1["created"]] == ["sent"]

    # Once sent, the same idea is not repeated within 24 h.
    later = day + bar
    _seed_analysis(
        conn, symbol="DOGEUSDT", action="short", confidence=0.75, invalidation=0.0835, ts=later - 60_000
    )
    p2 = run_assist_signals(conn, futures=fake, now_ms_value=later, send=lambda _t: True)
    assert any(s.get("reason") == "same_idea" for s in p2["skipped"])


def test_morning_batch_after_paper_run_sends_held_signal(tmp_path: Path) -> None:
    """Regression 2026-10-27: at 08:00 the newest analysis is 9 h old (stale); at 08:20 it is fresh."""
    conn = open_db(tmp_path / "morning_fresh.db")
    held_at = _ms_bangkok(2026, 10, 27, 4, 25)  # 21:25 UTC
    fake = FakeFutures(marks={"DOGEUSDT": Decimal("0.0800")}, steps={"DOGEUSDT": Decimal("1")})
    bar_16 = _ms_bangkok(2026, 10, 26, 23, 0)  # 16:00 UTC bar, written by the 04:00 paper-run
    _seed_analysis(conn, symbol="DOGEUSDT", action="short", confidence=0.75, invalidation=0.0835, ts=bar_16)
    p0 = run_assist_signals(conn, futures=fake, now_ms_value=held_at, send=lambda _t: True)
    assert [c["status"] for c in p0["created"]] == ["held"]

    bar_20 = _ms_bangkok(2026, 10, 27, 3, 0)  # 20:00 UTC bar, written by the 08:00 paper-run
    _seed_analysis(conn, symbol="DOGEUSDT", action="short", confidence=0.75, invalidation=0.0835, ts=bar_20)
    sent: list[str] = []
    p1 = run_assist_morning(
        conn,
        futures=fake,
        now_ms_value=_ms_bangkok(2026, 10, 27, 8, 20),
        send=lambda t: sent.append(t) or True,
    )
    assert len(p1["sent"]) == 1
    assert sent and "DOGEUSDT" in sent[0]


def _candidate_with_notional(
    *,
    symbol: str,
    notional: Decimal,
    confidence: float,
    symbol_rank: int,
) -> SignalCandidate:
    entry = Decimal("1")
    stop = Decimal("0.9")
    qty = notional / entry
    return SignalCandidate(
        sized=SizedSignal(
            symbol=symbol,
            side="long",
            entry=entry,
            stop=stop,
            target=Decimal("1.2"),
            qty=qty,
            notional=notional,
            leverage=1,
            loss_at_stop=Decimal("1"),
            risk_pct=Decimal("1"),
            r_distance=entry - stop,
            leverage_capped=False,
            uncapped_leverage=1,
            analysis_id=None,
        ),
        confidence=confidence,
        symbol_rank=symbol_rank,
    )


def test_assign_shared_leverage_ada_doge_l2() -> None:
    """ADA 69.39 + DOGE 100.32 on available 100 → shared L=2; margins sum under 100."""
    ada = _candidate_with_notional(
        symbol="ADAUSDT",
        notional=Decimal("69.39"),
        confidence=0.70,
        symbol_rank=4,
    )
    doge = _candidate_with_notional(
        symbol="DOGEUSDT",
        notional=Decimal("100.32"),
        confidence=0.72,
        symbol_rank=5,
    )
    admitted, dropped = assign_shared_leverage(
        [ada, doge],
        available_balance=Decimal("100"),
    )
    assert dropped == []
    assert len(admitted) == 2
    assert {c.sized.leverage for c in admitted} == {2}
    margins = sum((c.sized.notional / Decimal(c.sized.leverage) for c in admitted), Decimal("0"))
    assert margins < Decimal("100")
    assert margins == (Decimal("69.39") + Decimal("100.32")) / Decimal("2")


def test_assign_shared_leverage_drops_margin_cap() -> None:
    high = _candidate_with_notional(
        symbol="BTCUSDT",
        notional=Decimal("100"),
        confidence=0.80,
        symbol_rank=0,
    )
    low = _candidate_with_notional(
        symbol="ETHUSDT",
        notional=Decimal("80"),
        confidence=0.60,
        symbol_rank=1,
    )
    # available 30 → batch L=7; drop ETH; BTC alone needs L=4.
    admitted, dropped = assign_shared_leverage(
        [high, low],
        available_balance=Decimal("30"),
    )
    assert len(dropped) == 1
    assert dropped[0].sized.symbol == "ETHUSDT"
    assert len(admitted) == 1
    assert admitted[0].sized.symbol == "BTCUSDT"
    assert admitted[0].sized.leverage == 4


def test_assign_shared_leverage_lower_available_raises_l() -> None:
    ada = _candidate_with_notional(
        symbol="ADAUSDT",
        notional=Decimal("69.39"),
        confidence=0.70,
        symbol_rank=4,
    )
    doge = _candidate_with_notional(
        symbol="DOGEUSDT",
        notional=Decimal("100.32"),
        confidence=0.72,
        symbol_rank=5,
    )
    full, _ = assign_shared_leverage([ada, doge], available_balance=Decimal("100"))
    tight, _ = assign_shared_leverage([ada, doge], available_balance=Decimal("50"))
    assert full[0].sized.leverage == 2
    assert tight[0].sized.leverage > full[0].sized.leverage
    assert {c.sized.leverage for c in tight} == {4}


def test_shared_leverage_two_idea_telegram_text() -> None:
    """Fixture: print the two-idea Telegram text for the report."""
    ada = _candidate_with_notional(
        symbol="ADAUSDT",
        notional=Decimal("69.39"),
        confidence=0.70,
        symbol_rank=4,
    )
    doge = _candidate_with_notional(
        symbol="DOGEUSDT",
        notional=Decimal("100.32"),
        confidence=0.72,
        symbol_rank=5,
    )
    admitted, _ = assign_shared_leverage(
        [ada, doge],
        available_balance=Decimal("100"),
    )
    expires = _ms_bangkok(2026, 10, 27, 12, 25)
    blocks = [format_signal_message(c.sized, expires_at_ms=expires, balance=Decimal("100")) for c in admitted]
    text = "\n\n".join(blocks)
    assert "ADAUSDT" in text and "DOGEUSDT" in text
    assert text.count("Leverage ที่ต้องใช้: 2x (isolated)") == 2
    assert "margin ≈" in text
    # Visible in pytest -s / CI logs for the PR report.
    print(text)
