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
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0)
    day = _ms_bangkok(2026, 10, 27, 12, 0)
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
    _seed_analysis(conn, symbol="ETHUSDT", action="flat", confidence=0.9, invalidation=None)
    _seed_analysis(conn, symbol="SOLUSDT", action="long", confidence=0.5, invalidation=10.0)
    day = _ms_bangkok(2026, 10, 27, 12, 0)
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
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.72,
        invalidation=0.0835,
    )
    night = _ms_bangkok(2026, 10, 26, 23, 45)
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

    morning = _ms_bangkok(2026, 10, 27, 8, 0)
    # Mark still near entry (within 0.3R).
    fake.marks["DOGEUSDT"] = Decimal("0.0795")
    p2 = run_assist_morning(conn, futures=fake, now_ms_value=morning, send=capture)
    assert p2["ok"] is True
    assert len(p2["sent"]) == 1
    assert len(sent) == 1
    assert "DOGEUSDT" in sent[0]
    assert list_held_signals(conn) == []
    conn.close()


def test_morning_expires_on_chase(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "d.db")
    _seed_analysis(
        conn,
        symbol="DOGEUSDT",
        action="short",
        confidence=0.72,
        invalidation=0.0835,
    )
    night = _ms_bangkok(2026, 10, 26, 23, 45)
    fake = FakeFutures(
        marks={"DOGEUSDT": Decimal("0.0800")},
        steps={"DOGEUSDT": Decimal("1")},
    )
    run_assist_signals(conn, futures=fake, now_ms_value=night, send=lambda _t: True)
    # Price ran more than 0.3R toward target (down for short).
    fake.marks["DOGEUSDT"] = Decimal("0.0780")
    morning = _ms_bangkok(2026, 10, 27, 8, 0)
    sent: list[str] = []

    def capture(text: str) -> bool:
        sent.append(text)
        return True

    p = run_assist_morning(conn, futures=fake, now_ms_value=morning, send=capture)
    assert p["expired"]
    assert sent == []
    conn.close()


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


def test_key_unsafe_refuses_run(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "e.db")
    fake = FakeFutures(key_safe=False)
    sent: list[str] = []

    def capture(text: str) -> bool:
        sent.append(text)
        return True

    payload = run_assist_signals(
        conn,
        futures=fake,
        now_ms_value=_ms_bangkok(2026, 10, 27, 12, 0),
        send=capture,
    )
    assert payload["ok"] is False
    assert payload["error"] == "key_unsafe"
    assert sent == []
    conn.close()


def test_permission_error_reported_once(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "f.db")
    ensure_settings(conn)
    fake = FakeFutures(permission_on_balance=FuturesPermissionError("code=-2015"))
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
    assert p1["error"] == "permission_error"
    assert len(sent) == 1
    assert get_edge(conn)["permission_error_reported"] is True
    p2 = run_assist_signals(
        conn,
        futures=fake,
        now_ms_value=_ms_bangkok(2026, 10, 27, 12, 5),
        send=capture,
    )
    assert p2["error"] == "permission_error"
    assert len(sent) == 1  # no second alert
    conn.close()


def test_payload_never_contains_api_key(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "g.db")
    _seed_analysis(conn, symbol="BTCUSDT", action="long", confidence=0.7, invalidation=90.0)
    fake = FakeFutures(marks={"BTCUSDT": Decimal("100")}, steps={"BTCUSDT": Decimal("0.001")})
    payload = run_assist_signals(
        conn,
        futures=fake,
        now_ms_value=_ms_bangkok(2026, 10, 27, 12, 0),
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
