"""Weekly loss pause at 4R × week-start risk (migration 28).

(P) tests pass on main @ c5e3063 and after the change.
(N) tests fail on c5e3063 and pass after.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.test_assist import FakeFutures, _ms_bangkok
from tests.test_assist_listen import _send_capture
from tests.test_assist_quick import _seed_quick_closed_with_exit_ids

from jarvise_assist.menu import handle_risk_callback, handle_risk_number
from jarvise_assist.pause import (
    clear_pause_manual,
    evaluate_pause,
    next_0820_bangkok_ms,
    week_loss_pause_text,
)
from jarvise_assist.store import ensure_settings, get_edge, write_edge, write_settings
from jarvise_ingest.db import SCHEMA_VERSION, open_db, schema_version

TUE = _ms_bangkok(2026, 11, 17, 15, 0)
WED_NIGHT = _ms_bangkok(2026, 11, 18, 2, 0)
WED_10 = _ms_bangkok(2026, 11, 18, 10, 0)
WED_15 = _ms_bangkok(2026, 11, 18, 15, 0)
MON_NEXT = _ms_bangkok(2026, 11, 23, 10, 0)
MON_01 = _ms_bangkok(2026, 11, 16, 1, 0)
MON_07 = _ms_bangkok(2026, 11, 16, 7, 0)
WEEK_ID = "2026-11-16"
WEEK_ID_NEXT = "2026-11-23"


def _income(net: float, *, at_ms: int, trade_id: str | None = None) -> list[dict[str, Any]]:
    row: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "incomeType": "REALIZED_PNL",
        "income": str(net),
        "time": at_ms - 1000,
    }
    if trade_id is not None:
        row["tradeId"] = trade_id
    return [row]


def _futures(net: float, *, at_ms: int, trade_id: str | None = None) -> FakeFutures:
    trading_net = Decimal(str(net))
    return FakeFutures(
        balance=Decimal("1000") + trading_net,
        income=_income(net, at_ms=at_ms, trade_id=trade_id),
    )


class CountingFutures(FakeFutures):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.balance_calls = 0
        self.income_calls = 0

    def usdt_wallet_balance(self) -> Decimal:
        self.balance_calls += 1
        return super().usdt_wallet_balance()

    def income_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.income_calls += 1
        return super().income_history(**kwargs)


def _eval(
    conn: Any,
    *,
    risk: float,
    net: float,
    now_ms: int = TUE,
    futures: FakeFutures | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    write_settings(conn, risk_pct=risk)
    sent, send = _send_capture()
    fake = futures if futures is not None else _futures(net, at_ms=now_ms)
    result = evaluate_pause(conn, futures=fake, now_ms=now_ms, send=send)
    return result, sent


# ---------------------------------------------------------------------------
# (P) Agreement / once / night / quick / Binance counts
# ---------------------------------------------------------------------------


def test_p_agreement_table(tmp_path: Path) -> None:
    cases: list[tuple[float, float, bool]] = [
        (1.0, -70, True),
        (1.0, -30, False),
        (1.5, -60, False),
        (1.5, -61, True),
        (2.0, -90, True),
        (3.0, -130, True),
        (3.0, -121, True),
        (0.5, -10, False),
    ]
    for risk, net, expect_pause in cases:
        conn = open_db(tmp_path / f"agree_{risk}_{net}.db")
        result, sent = _eval(conn, risk=risk, net=net)
        if expect_pause:
            assert result["paused"] is True
            assert result["reason"] == "week_loss"
            assert result["until_ms"] == next_0820_bangkok_ms(TUE)
            loss_pct = Decimal(str(result["loss_pct"]))
            assert len(sent) == 1
            assert sent[0]["text"] == week_loss_pause_text(loss_pct)
            trading_net = Decimal(str(net))
            expected_pct = (-trading_net / Decimal("1000")) * Decimal("100")
            assert abs(loss_pct - expected_pct) < Decimal("0.0001")
            assert get_edge(conn)["week_pause_id"] == WEEK_ID
        else:
            assert result.get("reason") != "week_loss"
            assert all("ขาดทุนสัปดาห์นี้" not in s["text"] for s in sent)
        conn.close()


def test_p_once_per_week_after_manual_resume(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "once.db")
    result, sent = _eval(conn, risk=3.0, net=-130)
    assert result["reason"] == "week_loss"
    assert any("ขาดทุนสัปดาห์นี้" in s["text"] for s in sent)
    clear_pause_manual(conn, now_ms_value=TUE)
    n = len(sent)
    result2, sent2 = _eval(conn, risk=3.0, net=-300)
    assert result2.get("reason") != "week_loss"
    assert all("ขาดทุนสัปดาห์นี้" not in s["text"] for s in sent2)
    assert len(sent2) == 0 or len(sent) == n  # no new week text
    week_texts = [s["text"] for s in sent + sent2 if "ขาดทุนสัปดาห์นี้" in s["text"]]
    assert len(week_texts) == 1
    conn.close()


def test_p_night_until_same_day_0820(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "night.db")
    result, _ = _eval(conn, risk=3.0, net=-130, now_ms=WED_NIGHT)
    assert result["reason"] == "week_loss"
    assert result["until_ms"] == next_0820_bangkok_ms(WED_NIGHT)
    assert result["until_ms"] == _ms_bangkok(2026, 11, 18, 8, 20)
    conn.close()


def test_p_quick_income_counted(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "quick.db")
    write_settings(conn, risk_pct=3.0)
    now = TUE
    income = _income(-130, at_ms=now, trade_id="99")
    _seed_quick_closed_with_exit_ids(
        conn,
        symbol="BTCUSDT",
        exit_trade_id=99,
        closed_at_ms=now - 1000,
        entry_ms=now - 10_000,
        idx=1,
    )
    sent, send = _send_capture()
    fake = FakeFutures(balance=Decimal("1000") + Decimal("-130"), income=income)
    result = evaluate_pause(conn, futures=fake, now_ms=now, send=send)
    assert result["reason"] == "week_loss"
    assert any("ขาดทุนสัปดาห์นี้" in s["text"] for s in sent)
    conn.close()


def test_p_binance_reads_once_per_call(tmp_path: Path) -> None:
    for net, expect in ((-10, False), (-130, True)):
        conn = open_db(tmp_path / f"reads_{net}.db")
        write_settings(conn, risk_pct=3.0)
        fake = CountingFutures(
            balance=Decimal("1000") + Decimal(str(net)),
            income=_income(net, at_ms=TUE),
        )
        sent, send = _send_capture()
        result = evaluate_pause(conn, futures=fake, now_ms=TUE, send=send)
        assert fake.balance_calls == 1
        assert fake.income_calls == 1
        if expect:
            assert result["reason"] == "week_loss"
        else:
            assert result.get("reason") != "week_loss"
        conn.close()


# ---------------------------------------------------------------------------
# (N) Disagreement / keys / freeze / menu / paused / legacy / migration
# ---------------------------------------------------------------------------


def test_n_disagreement_table(tmp_path: Path) -> None:
    no_pause = [(3.0, -70), (3.0, -120), (2.0, -70)]
    for risk, net in no_pause:
        conn = open_db(tmp_path / f"no_{risk}_{net}.db")
        result, sent = _eval(conn, risk=risk, net=net)
        assert result.get("reason") != "week_loss"
        assert all("ขาดทุนสัปดาห์นี้" not in s["text"] for s in sent)
        conn.close()

    for risk, net in ((1.0, -50), (0.5, -30)):
        conn = open_db(tmp_path / f"yes_{risk}_{net}.db")
        result, sent = _eval(conn, risk=risk, net=net)
        assert result["reason"] == "week_loss"
        assert len(sent) == 1
        conn.close()


def test_n_result_keys_on_fire(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "keys.db")
    result, _ = _eval(conn, risk=3.0, net=-121)
    assert result["reason"] == "week_loss"
    assert result["threshold_pct"] == 12.0
    assert result["week_risk_pct"] == 3.0
    conn.close()


def test_n_frozen_for_the_week(tmp_path: Path, monkeypatch: Any) -> None:
    from jarvise_assist import pause as pause_mod
    from jarvise_assist.pause import week_start_risk_pct  # noqa: F401 — N body import

    conn = open_db(tmp_path / "freeze.db")
    write_settings(conn, risk_pct=3.0)
    result, sent = _eval(conn, risk=3.0, net=-10)
    assert result.get("reason") != "week_loss"
    edge = get_edge(conn)
    assert edge["week_risk_id"] == WEEK_ID
    assert edge["week_risk_pct"] == 3.0

    monkeypatch.setattr(pause_mod, "now_ms", lambda: WED_10)
    sent_m, send_m = _send_capture()
    handle_risk_callback(conn, "risk:1", send=send_m)
    assert ensure_settings(conn)["risk_pct"] == 1.0
    assert get_edge(conn)["week_risk_pct"] == 3.0

    result2, _ = _eval(conn, risk=1.0, net=-80, now_ms=WED_15)
    # risk written already; keep 1.0 but frozen week risk is 3.0 → W=12%, 8% no pause
    assert result2.get("reason") != "week_loss"

    # Next Bangkok week: new income only in that week; risk still 1.0 → W=4%
    next_income = _income(-50, at_ms=MON_NEXT)
    fake = FakeFutures(balance=Decimal("1000") + Decimal("-50"), income=next_income)
    sent3, send3 = _send_capture()
    # Ensure settings risk is 1.0 for the new week snapshot
    write_settings(conn, risk_pct=1.0)
    # Clear prior week pause id so week rule can fire
    write_edge(conn, week_pause_id=None)
    write_settings(conn, paused_until_ms=None)
    result3 = evaluate_pause(conn, futures=fake, now_ms=MON_NEXT, send=send3)
    assert result3["reason"] == "week_loss"
    assert result3["threshold_pct"] == 4.0
    assert get_edge(conn)["week_risk_id"] == WEEK_ID_NEXT
    conn.close()


def test_n_menu_snapshot_while_assist_off(tmp_path: Path, monkeypatch: Any) -> None:
    from jarvise_assist import pause as pause_mod

    # Path A: risk:1 while off, no evaluate yet
    conn = open_db(tmp_path / "off_a.db")
    write_settings(conn, enabled=False, risk_pct=3.0)
    monkeypatch.setattr(pause_mod, "now_ms", lambda: MON_07)
    sent, send = _send_capture()
    handle_risk_callback(conn, "risk:1", send=send)
    edge = get_edge(conn)
    assert edge["week_risk_id"] == WEEK_ID
    assert edge["week_risk_pct"] == 3.0
    write_settings(conn, enabled=True, risk_pct=1.0)
    result, _ = _eval(conn, risk=1.0, net=-80)
    assert result.get("reason") != "week_loss"
    conn.close()

    # Path B: risk:3 → risk_yes:3 stores pre-change when no snapshot
    conn_b = open_db(tmp_path / "off_b.db")
    write_settings(conn_b, enabled=False, risk_pct=3.0)
    monkeypatch.setattr(pause_mod, "now_ms", lambda: MON_07)
    sent_b, send_b = _send_capture()
    handle_risk_callback(conn_b, "risk:3", send=send_b)
    handle_risk_callback(conn_b, "risk_yes:3", send=send_b)
    # After confirm risk is still 3; snapshot must be 3.0 (pre-change == 3)
    # Re-seed: start at 2.0 then confirm 3 so pre-change is 2.0
    conn_b2 = open_db(tmp_path / "off_b2.db")
    write_settings(conn_b2, enabled=False, risk_pct=2.0)
    monkeypatch.setattr(pause_mod, "now_ms", lambda: MON_07)
    sent_b2, send_b2 = _send_capture()
    handle_risk_callback(conn_b2, "risk:3", send=send_b2)
    handle_risk_callback(conn_b2, "risk_yes:3", send=send_b2)
    edge_b2 = get_edge(conn_b2)
    assert edge_b2["week_risk_id"] == WEEK_ID
    assert edge_b2["week_risk_pct"] == 2.0
    assert ensure_settings(conn_b2)["risk_pct"] == 3.0
    # Existing snapshot not overwritten
    handle_risk_callback(conn_b2, "risk:1", send=send_b2)
    assert get_edge(conn_b2)["week_risk_pct"] == 2.0
    conn_b.close()
    conn_b2.close()

    # Path C: handle_risk_number with awaiting_risk='number'
    conn_c = open_db(tmp_path / "off_c.db")
    write_settings(conn_c, enabled=False, risk_pct=3.0, awaiting_risk="number")
    monkeypatch.setattr(pause_mod, "now_ms", lambda: MON_07)
    sent_c, send_c = _send_capture()
    handle_risk_number(conn_c, "1", send=send_c)
    edge_c = get_edge(conn_c)
    assert edge_c["week_risk_id"] == WEEK_ID
    assert edge_c["week_risk_pct"] == 3.0
    assert ensure_settings(conn_c)["risk_pct"] == 1.0
    # Existing snapshot not overwritten
    write_settings(conn_c, awaiting_risk="number")
    handle_risk_number(conn_c, "2", send=send_c)
    assert get_edge(conn_c)["week_risk_pct"] == 3.0
    conn_c.close()


def test_n_snapshot_while_paused_or_binance_down(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "paused.db")
    write_settings(conn, risk_pct=3.0, paused_until_ms=MON_01 + 3_600_000)
    sent, send = _send_capture()
    fake = _futures(-10, at_ms=MON_01)
    result = evaluate_pause(conn, futures=fake, now_ms=MON_01, send=send)
    assert result["paused"] is True
    assert result["reason"] == "already"
    assert get_edge(conn)["week_risk_id"] == WEEK_ID
    assert get_edge(conn)["week_risk_pct"] == 3.0
    conn.close()

    conn2 = open_db(tmp_path / "down.db")
    write_settings(conn2, risk_pct=2.0)

    class BrokenIncome(FakeFutures):
        def income_history(self, **kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("binance down")

    sent2, send2 = _send_capture()
    result2 = evaluate_pause(
        conn2,
        futures=BrokenIncome(balance=Decimal("1000"), income=[]),
        now_ms=TUE,
        send=send2,
    )
    assert result2 == {"paused": False, "error": "RuntimeError"} or (
        result2.get("paused") is False and result2.get("error") == "RuntimeError"
    )
    assert get_edge(conn2)["week_risk_id"] == WEEK_ID
    assert get_edge(conn2)["week_risk_pct"] == 2.0
    conn2.close()


def test_n_legacy_risk_above_max(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "legacy.db")
    write_settings(conn, risk_pct=5.0)
    sent, send = _send_capture()
    result = evaluate_pause(conn, futures=_futures(-110, at_ms=TUE), now_ms=TUE, send=send)
    assert result.get("reason") != "week_loss"
    assert all("ขาดทุนสัปดาห์นี้" not in s["text"] for s in sent)

    write_settings(conn, paused_until_ms=None)
    conn.execute("UPDATE assist_settings SET risk_pct=5.0 WHERE id=1")
    conn.commit()
    sent2, send2 = _send_capture()
    result2 = evaluate_pause(conn, futures=_futures(-130, at_ms=TUE), now_ms=TUE, send=send2)
    assert result2["reason"] == "week_loss"
    assert result2["threshold_pct"] == 12.0
    conn.close()


def test_n_migration_28(tmp_path: Path) -> None:
    fresh = open_db(tmp_path / "fresh.db")
    assert SCHEMA_VERSION == 29
    assert schema_version(fresh) == 29
    edge = get_edge(fresh)
    assert edge["week_risk_id"] is None
    assert edge["week_risk_pct"] is None
    fresh.close()

    path = tmp_path / "from27.db"
    conn = open_db(path)
    write_edge(
        conn,
        streak_anchor_ms=111,
        week_pause_id="2026-11-09",
        day_pause_id="2026-11-14",
        resume_alerted=True,
    )
    for col in ("week_risk_pct", "week_risk_id"):
        conn.execute(f"ALTER TABLE assist_edge DROP COLUMN {col}")
    conn.execute("PRAGMA user_version = 27")
    conn.commit()
    conn.close()

    conn = open_db(path)
    assert schema_version(conn) == 29
    row = conn.execute(
        """
        SELECT streak_anchor_ms, week_pause_id, day_pause_id, resume_alerted,
               week_risk_id, week_risk_pct
        FROM assist_edge WHERE id=1
        """
    ).fetchone()
    assert row["streak_anchor_ms"] == 111
    assert row["week_pause_id"] == "2026-11-09"
    assert row["day_pause_id"] == "2026-11-14"
    assert int(row["resume_alerted"]) == 1
    assert row["week_risk_id"] is None
    assert row["week_risk_pct"] is None
    from jarvise_assist.pause import WEEK_LOSS_R, week_start_risk_pct

    assert WEEK_LOSS_R == Decimal("4")
    assert callable(week_start_risk_pct)
    conn.close()
