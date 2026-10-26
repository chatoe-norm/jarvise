"""Trade assistant PR A — sizing, floors, night hold, futures permission."""

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
    audit_key_for_assist,
)
from jarvise_assist.messages import format_signal_message
from jarvise_assist.signals import is_night_hold, run_assist_morning, run_assist_signals
from jarvise_assist.sizing import (
    chase_exceeded,
    floor_to_step,
    next_4h_close_ms,
    next_assist_run_ms,
    passes_signal_floor,
    size_signal,
)
from jarvise_assist.store import ensure_settings, get_edge, list_held_signals
from jarvise_ingest.db import open_db, upsert_analysis_output

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")


class FakeFutures:
    def __init__(
        self,
        *,
        balance: Decimal = Decimal("1000"),
        marks: dict[str, Decimal] | None = None,
        steps: dict[str, Decimal] | None = None,
        open_symbols: set[str] | None = None,
        key_safe: bool = True,
        key_error: Exception | None = None,
        read_error: Exception | None = None,
        permission_on_balance: Exception | None = None,
    ) -> None:
        self.balance = balance
        self.marks = marks or {}
        self.steps = steps or {}
        self._open = open_symbols or set()
        self.key_safe = key_safe
        self.key_error = key_error
        self.read_error = read_error
        self.permission_on_balance = permission_on_balance
        self.api_key = "TEST_KEY_SHOULD_NEVER_APPEAR"

    def assert_key_safe(self) -> dict[str, Any]:
        if self.key_error:
            raise self.key_error
        if not self.key_safe:
            raise FuturesKeyUnsafeError("enableWithdrawals=true")
        return {"ok_for_assist": True}

    def usdt_wallet_balance(self) -> Decimal:
        if self.permission_on_balance:
            raise self.permission_on_balance
        if self.read_error:
            raise self.read_error
        return self.balance

    def open_symbols(self) -> set[str]:
        if self.read_error:
            raise self.read_error
        return set(self._open)

    def position_risk(self, *, symbol: str | None = None) -> list[FuturesPosition]:
        out = []
        for sym in self._open:
            if symbol and sym != symbol:
                continue
            out.append(
                FuturesPosition(
                    symbol=sym,
                    position_side="BOTH",
                    position_amt=Decimal("1"),
                    entry_price=Decimal("1"),
                    mark_price=self.marks.get(sym, Decimal("1")),
                )
            )
        return out

    def mark_price(self, symbol: str) -> Decimal:
        return self.marks.get(symbol.upper(), Decimal("100"))

    def step_size(self, symbol: str) -> Decimal:
        return self.steps.get(symbol.upper(), Decimal("1"))


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
    }
    settings = ensure_settings(conn)
    assert settings["risk_pct"] == 2.0
    assert settings["enabled"] is True
    conn.close()


def test_next_4h_close_ms() -> None:
    # 2026-10-27 10:00 UTC → next boundary 12:00 UTC
    ts = int(datetime(2026, 10, 27, 10, 0, tzinfo=UTC).timestamp() * 1000)
    nxt = next_4h_close_ms(ts)
    assert datetime.fromtimestamp(nxt / 1000, tz=UTC).hour == 12
