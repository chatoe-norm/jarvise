"""Regressions found in the 2026-11-01 trade-assistant audit."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tests.test_assist import FOUR_H_MS, FakeFutures, _ms_bangkok, _seed_analysis, _seed_candle

from jarvise_assist.futures import FuturesPosition
from jarvise_assist.sizing import (
    SignalCandidate,
    analysis_is_fresh,
    assign_shared_leverage,
    size_signal,
)
from jarvise_assist.store import get_watch_position
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
