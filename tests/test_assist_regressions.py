"""Regressions found in the 2026-11-01 trade-assistant audit."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tests.test_assist import FOUR_H_MS, FakeFutures, _ms_bangkok, _seed_analysis, _seed_candle

from jarvise_assist.futures import (
    FuturesPosition,
    find_protective_stop,
    is_protective_stop,
    order_stop_price,
    protective_stop_coverage,
)
from jarvise_assist.messages import format_partial_stop_warning
from jarvise_assist.signals import run_assist_morning
from jarvise_assist.sizing import (
    SignalCandidate,
    analysis_is_fresh,
    assign_shared_leverage,
    next_assist_run_ms,
    size_signal,
)
from jarvise_assist.store import get_watch_position, insert_signal, list_held_signals
from jarvise_assist.watch import run_assist_watch
from jarvise_assist.weekly import run_assist_weekly
from jarvise_ingest.db import open_db


def _analysis_at(ts: int) -> dict[str, object]:
    return {"timestamp": ts, "action": "long"}


def test_analysis_stays_fresh_until_paper_run_replaces_it() -> None:
    # 4h bar opened 11:00 Bangkok; paper-run writes it at 16:00, replaces it at 20:00.
    bar_11 = _ms_bangkok(2026, 11, 1, 11, 0)
    bar_15 = _ms_bangkok(2026, 11, 1, 15, 0)
    # 19:37 was stale under the old 8.5h age limit (age 8.6h).
    assert analysis_is_fresh(_analysis_at(bar_11), now_ms=_ms_bangkok(2026, 11, 1, 19, 37))
    assert analysis_is_fresh(_analysis_at(bar_11), now_ms=_ms_bangkok(2026, 11, 1, 20, 5))
    # The 20:25 signal job must still require the bar written by the 20:00 run.
    assert not analysis_is_fresh(_analysis_at(bar_11), now_ms=_ms_bangkok(2026, 11, 1, 20, 25))
    assert analysis_is_fresh(_analysis_at(bar_15), now_ms=_ms_bangkok(2026, 11, 1, 20, 25))


def test_morning_batch_requires_the_0800_analysis() -> None:
    morning = _ms_bangkok(2026, 11, 2, 8, 20)
    assert analysis_is_fresh(_analysis_at(_ms_bangkok(2026, 11, 2, 3, 0)), now_ms=morning)
    assert not analysis_is_fresh(_analysis_at(_ms_bangkok(2026, 11, 1, 23, 0)), now_ms=morning)


def test_future_dated_analysis_is_not_fresh() -> None:
    now = _ms_bangkok(2026, 11, 1, 12, 0)
    assert not analysis_is_fresh(_analysis_at(now + 60_000), now_ms=now)


def _short_pos(entry: str, update_ms: int) -> FuturesPosition:
    return FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal(entry),
        mark_price=Decimal(entry),
        update_time_ms=update_ms,
    )


def test_reopened_position_gets_fresh_stop_and_advice(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "reopen.db")
    t1 = _ms_bangkok(2026, 11, 1, 12, 0)
    _seed_candle(
        conn,
        symbol="DOGEUSDT",
        ts=t1 - 2 * FOUR_H_MS,
        open_=0.08,
        high=0.081,
        low=0.079,
        close=0.08,
        atr=0.002,
    )
    _seed_analysis(conn, symbol="DOGEUSDT", action="flat", confidence=0.9, invalidation=None, ts=t1 - 60_000)

    sent: list[str] = []

    def capture(text: str) -> bool:
        sent.append(text)
        return True

    # Trade 1: short at 0.08, no stop order -> no-stop warning and flat exit advice.
    run_assist_watch(
        conn, futures=FakeFutures(positions=[_short_pos("0.08", t1 - 600_000)]), now_ms_value=t1, send=capture
    )
    first = get_watch_position(conn, "DOGEUSDT", "BOTH")
    assert first is not None and abs(first["initial_stop"] - 0.083) < 1e-9
    assert any("ไม่มี stop" in t for t in sent)
    assert any("FLAT" in t for t in sent)

    # Closed on the next poll.
    t2 = t1 + 300_000
    run_assist_watch(conn, futures=FakeFutures(), now_ms_value=t2, send=capture)

    # Trade 2: short again at 0.07 later the same day, still no stop order.
    t3 = t1 + 3_600_000
    _seed_analysis(conn, symbol="DOGEUSDT", action="flat", confidence=0.9, invalidation=None, ts=t3 - 60_000)
    sent.clear()
    run_assist_watch(
        conn, futures=FakeFutures(positions=[_short_pos("0.07", t3 - 60_000)]), now_ms_value=t3, send=capture
    )
    second = get_watch_position(conn, "DOGEUSDT", "BOTH")
    assert second is not None
    assert abs(second["initial_stop"] - 0.073) < 1e-9  # from the new entry, not 0.083
    assert second["entry_time_ms"] == t3 - 60_000
    assert second["closed_at_ms"] is None
    assert any("ไม่มี stop" in t and "0.073" in t for t in sent)
    assert any("FLAT" in t for t in sent)
    conn.close()


def test_side_flip_between_polls_resets_initial_stop(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "flip.db")
    t1 = _ms_bangkok(2026, 11, 1, 12, 0)
    _seed_candle(
        conn,
        symbol="DOGEUSDT",
        ts=t1 - 2 * FOUR_H_MS,
        open_=0.08,
        high=0.081,
        low=0.079,
        close=0.08,
        atr=0.002,
    )
    _seed_analysis(conn, symbol="DOGEUSDT", action="long", confidence=0.7, invalidation=0.077, ts=t1 - 60_000)
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[_short_pos("0.08", t1 - 600_000)]),
        now_ms_value=t1,
        send=lambda _t: True,
    )
    long_pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("100"),
        entry_price=Decimal("0.08"),
        mark_price=Decimal("0.08"),
        update_time_ms=t1 + 100_000,
    )
    sent: list[str] = []
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[long_pos]),
        now_ms_value=t1 + 300_000,
        send=lambda t: sent.append(t) or True,
    )
    row = get_watch_position(conn, "DOGEUSDT", "BOTH")
    assert row is not None and row["side"] == "long"
    assert row["initial_stop"] < 0.08  # a long stop sits below entry
    assert any("ไม่มี stop" in t and "0.077" in t for t in sent)
    conn.close()


def test_margin_shrink_floors_to_lot_size() -> None:
    sized = size_signal(
        symbol="DOGEUSDT",
        side="long",
        entry=Decimal("0.07"),
        stop=Decimal("0.069"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    cand = SignalCandidate(sized=sized, confidence=0.7, symbol_rank=0)
    kept, dropped = assign_shared_leverage([cand], available_balance=Decimal("100"))
    assert dropped == []
    qty = kept[0].sized.qty
    assert qty == Decimal("7142")  # 500 / 0.07 = 7142.86 -> whole lots
    assert kept[0].sized.notional == qty * Decimal("0.07")


def test_no_available_margin_drops_the_idea() -> None:
    sized = size_signal(
        symbol="DOGEUSDT",
        side="long",
        entry=Decimal("0.07"),
        stop=Decimal("0.069"),
        balance=Decimal("1000"),
        risk_pct=Decimal("2"),
        step_size=Decimal("1"),
    )
    assert sized is not None
    cand = SignalCandidate(sized=sized, confidence=0.7, symbol_rank=0)
    kept, dropped = assign_shared_leverage([cand], available_balance=Decimal("0"))
    assert kept == []
    assert dropped == [cand]


def test_monday_weekly_summary_covers_last_seven_days(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "weekly.db")
    monday_0830 = _ms_bangkok(2026, 11, 2, 8, 30)
    income = [
        {
            "symbol": "DOGEUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-1.25",
            "time": _ms_bangkok(2026, 10, 29, 10, 0),
        },
        {
            "symbol": "DOGEUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "-9",
            "time": _ms_bangkok(2026, 10, 25, 10, 0),
        },
    ]
    payload = run_assist_weekly(
        conn, futures=FakeFutures(income=income), now_ms_value=monday_0830, send=lambda _t: True
    )
    assert payload["real"]["universe_usdt"] == -1.25  # last Thursday counts, 8 days ago does not
    assert "7 วัน" in payload["text"]
    conn.close()


def _held_short(
    conn: object,
    *,
    symbol: str,
    entry: Decimal,
    stop: Decimal,
    created_at_ms: int,
    step_size: Decimal = Decimal("0.01"),
) -> str:
    sized = size_signal(
        symbol=symbol,
        side="short",
        entry=entry,
        stop=stop,
        balance=Decimal("1000"),
        risk_pct=Decimal("1"),
        step_size=step_size,
    )
    assert sized is not None
    return insert_signal(
        conn,
        sized,
        status="held",
        created_at_ms=created_at_ms,
        expires_at_ms=next_assist_run_ms(created_at_ms),
    )


def test_morning_expires_held_when_symbol_is_already_open(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "morning_open.db")
    night = _ms_bangkok(2026, 11, 1, 0, 25)
    morning = _ms_bangkok(2026, 11, 1, 8, 20)
    bar = _ms_bangkok(2026, 11, 1, 3, 0)
    _seed_analysis(conn, symbol="ADAUSDT", action="short", confidence=0.75, invalidation=0.42, ts=bar)
    sid = _held_short(
        conn, symbol="ADAUSDT", entry=Decimal("0.40"), stop=Decimal("0.42"), created_at_ms=night
    )
    pos = FuturesPosition(
        symbol="ADAUSDT",
        position_side="BOTH",
        position_amt=Decimal("-100"),
        entry_price=Decimal("0.40"),
        mark_price=Decimal("0.40"),
        update_time_ms=morning - 60_000,
    )
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=[pos],
        marks={"ADAUSDT": Decimal("0.40")},
        steps={"ADAUSDT": Decimal("1")},
    )
    payload = run_assist_morning(conn, futures=fake, now_ms_value=morning, send=lambda _t: True)
    assert sid in payload["expired"]
    assert payload["sent"] == []
    assert list_held_signals(conn) == []
    conn.close()


def test_morning_admits_against_open_positions_only(tmp_path: Path) -> None:
    """Two held ideas with two open positions already: both expire on the 2-idea cap."""
    conn = open_db(tmp_path / "morning_cap.db")
    night = _ms_bangkok(2026, 11, 1, 0, 25)
    morning = _ms_bangkok(2026, 11, 1, 8, 20)
    bar = _ms_bangkok(2026, 11, 1, 3, 0)
    for sym, inv in (("SOLUSDT", 110.0), ("BNBUSDT", 660.0)):
        _seed_analysis(conn, symbol=sym, action="short", confidence=0.8, invalidation=inv, ts=bar)
    sid_sol = _held_short(
        conn, symbol="SOLUSDT", entry=Decimal("100"), stop=Decimal("110"), created_at_ms=night
    )
    sid_bnb = _held_short(
        conn, symbol="BNBUSDT", entry=Decimal("600"), stop=Decimal("660"), created_at_ms=night
    )
    open_pos = [
        FuturesPosition(
            symbol="ADAUSDT",
            position_side="BOTH",
            position_amt=Decimal("-10"),
            entry_price=Decimal("0.4"),
            mark_price=Decimal("0.4"),
            update_time_ms=morning,
        ),
        FuturesPosition(
            symbol="DOGEUSDT",
            position_side="BOTH",
            position_amt=Decimal("-100"),
            entry_price=Decimal("0.08"),
            mark_price=Decimal("0.08"),
            update_time_ms=morning,
        ),
    ]
    fake = FakeFutures(
        balance=Decimal("1000"),
        positions=open_pos,
        marks={"SOLUSDT": Decimal("100"), "BNBUSDT": Decimal("600")},
        steps={"SOLUSDT": Decimal("0.01"), "BNBUSDT": Decimal("0.01")},
    )
    payload = run_assist_morning(conn, futures=fake, now_ms_value=morning, send=lambda _t: True)
    assert sid_sol in payload["expired"] and sid_bnb in payload["expired"]
    assert payload["sent"] == []
    conn.close()


def test_morning_expires_after_shared_leverage_below_min_notional(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "morning_min.db")
    night = _ms_bangkok(2026, 11, 1, 0, 25)
    morning = _ms_bangkok(2026, 11, 1, 8, 20)
    bar = _ms_bangkok(2026, 11, 1, 3, 0)
    _seed_analysis(conn, symbol="ADAUSDT", action="short", confidence=0.75, invalidation=0.401, ts=bar)
    # Tight stop → large notional; shared L with tiny available shrinks under min_notional.
    sid = _held_short(
        conn, symbol="ADAUSDT", entry=Decimal("0.40"), stop=Decimal("0.401"), created_at_ms=night
    )
    fake = FakeFutures(
        balance=Decimal("100"),
        available=Decimal("1"),
        marks={"ADAUSDT": Decimal("0.40")},
        steps={"ADAUSDT": Decimal("1")},
        min_notional={"ADAUSDT": Decimal("50")},
    )
    payload = run_assist_morning(conn, futures=fake, now_ms_value=morning, send=lambda _t: True)
    assert sid in payload["expired"]
    assert payload["sent"] == []
    conn.close()


def test_partial_stop_coverage_warns_once() -> None:
    assert (
        format_partial_stop_warning(
            symbol="DOGEUSDT",
            side="short",
            covered=Decimal("300"),
            position_qty=Decimal("676"),
            stop=Decimal("0.083"),
        )
        == "⚠️ DOGEUSDT SHORT stop คุมแค่ 300/676 — ตั้ง stop ให้ครบทั้งไม้ (Reduce-Only, Mark Price) ที่ 0.083"
    )
    order = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "stopPrice": "0.083",
        "origQty": "300",
    }
    assert (
        protective_stop_coverage(
            [order],
            symbol="DOGEUSDT",
            position_side="BOTH",
            trade_side="short",
            position_qty=Decimal("676"),
            step_size=Decimal("1"),
        )
        == "partial"
    )
    assert (
        protective_stop_coverage(
            [{**order, "origQty": "676"}],
            symbol="DOGEUSDT",
            position_side="BOTH",
            trade_side="short",
            position_qty=Decimal("676"),
            step_size=Decimal("1"),
        )
        == "full"
    )
    assert (
        protective_stop_coverage(
            [{**order, "closePosition": True}],
            symbol="DOGEUSDT",
            position_side="BOTH",
            trade_side="short",
            position_qty=Decimal("676"),
            step_size=Decimal("1"),
        )
        == "full"
    )


def test_partial_stop_sends_thai_warning(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "partial_stop.db")
    t1 = _ms_bangkok(2026, 11, 1, 12, 0)
    _seed_candle(
        conn,
        symbol="DOGEUSDT",
        ts=t1 - 2 * FOUR_H_MS,
        open_=0.08,
        high=0.081,
        low=0.079,
        close=0.08,
        atr=0.002,
    )
    _seed_analysis(conn, symbol="DOGEUSDT", action="flat", confidence=0.9, invalidation=None, ts=t1 - 60_000)
    pos = FuturesPosition(
        symbol="DOGEUSDT",
        position_side="BOTH",
        position_amt=Decimal("-676"),
        entry_price=Decimal("0.08"),
        mark_price=Decimal("0.08"),
        update_time_ms=t1 - 600_000,
    )
    order = {
        "symbol": "DOGEUSDT",
        "type": "STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "stopPrice": "0.083",
        "origQty": "300",
    }
    sent: list[str] = []
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[pos], orders=[order], steps={"DOGEUSDT": Decimal("1")}),
        now_ms_value=t1,
        send=lambda t: sent.append(t) or True,
    )
    assert any("คุมแค่ 300/676" in t for t in sent)
    assert any("ตั้ง stop ให้ครบทั้งไม้" in t for t in sent)
    # Second poll does not repeat while latched.
    n = len(sent)
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[pos], orders=[order], steps={"DOGEUSDT": Decimal("1")}),
        now_ms_value=t1 + 300_000,
        send=lambda t: sent.append(t) or True,
    )
    assert not any("คุมแค่ 300/676" in t for t in sent[n:])
    conn.close()


def test_entry_price_shift_resets_watch_row(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "entry_shift.db")
    t1 = _ms_bangkok(2026, 11, 1, 12, 0)
    _seed_candle(
        conn,
        symbol="DOGEUSDT",
        ts=t1 - 2 * FOUR_H_MS,
        open_=0.08,
        high=0.081,
        low=0.079,
        close=0.08,
        atr=0.002,
    )
    _seed_analysis(conn, symbol="DOGEUSDT", action="flat", confidence=0.9, invalidation=None, ts=t1 - 60_000)
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[_short_pos("0.08", t1 - 600_000)]),
        now_ms_value=t1,
        send=lambda _t: True,
    )
    first = get_watch_position(conn, "DOGEUSDT", "BOTH")
    assert first is not None
    first_stop = first["initial_stop"]
    first_entry_time = first["entry_time_ms"]
    # Same poll window, Binance entry moved >0.05% (add / reopen).
    t2 = t1 + 60_000
    _seed_analysis(conn, symbol="DOGEUSDT", action="flat", confidence=0.9, invalidation=None, ts=t2 - 60_000)
    sent: list[str] = []
    run_assist_watch(
        conn,
        futures=FakeFutures(positions=[_short_pos("0.07", t2 - 30_000)]),
        now_ms_value=t2,
        send=lambda t: sent.append(t) or True,
    )
    second = get_watch_position(conn, "DOGEUSDT", "BOTH")
    assert second is not None
    assert abs(second["initial_stop"] - 0.073) < 1e-9
    assert second["initial_stop"] != first_stop
    assert second["entry_time_ms"] == t2 - 30_000
    assert second["entry_time_ms"] != first_entry_time
    assert any("ไม่มี stop" in t for t in sent)
    conn.close()


def test_trailing_stop_protects_without_using_activate_price() -> None:
    trailing = {
        "symbol": "ADAUSDT",
        "type": "TRAILING_STOP_MARKET",
        "side": "BUY",
        "positionSide": "BOTH",
        "reduceOnly": True,
        "activatePrice": "0.50",
        "callbackRate": "1.0",
    }
    assert is_protective_stop(trailing, position_side="BOTH", trade_side="short")
    assert order_stop_price(trailing) is None
    assert (
        find_protective_stop(
            [trailing],
            symbol="ADAUSDT",
            position_side="BOTH",
            trade_side="short",
        )
        is None
    )
    assert (
        protective_stop_coverage(
            [trailing],
            symbol="ADAUSDT",
            position_side="BOTH",
            trade_side="short",
            position_qty=Decimal("100"),
            step_size=Decimal("1"),
        )
        == "full"
    )
