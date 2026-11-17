"""BE card copy is pinned to the wick trigger the code actually uses."""

from __future__ import annotations

from decimal import Decimal

from jarvise_assist.messages import (
    MANUAL_MOVE_LINE,
    PROGRESS_RULE_LINE,
    format_move_stop_advice,
    format_progress_alert,
)
from jarvise_assist.trail_advice import candle_extremes_after_entry
from jarvise_paper.profit_lock import DEFAULT_BREAKEVEN_R, DEFAULT_TRAIL_AFTER_R, trail_stop_with_source

ONE_H = 3_600_000


def _long_position() -> dict[str, float | str]:
    return {
        "side": "long",
        "entry_price": 100.0,
        "initial_stop_price": 97.0,
        "stop_price": 97.0,
    }


def test_constants_stay_at_the_shipped_rule() -> None:
    assert DEFAULT_BREAKEVEN_R == 0.5
    assert DEFAULT_TRAIL_AFTER_R == 1.0


def test_copy_follows_the_wick_the_code_uses_and_not_the_close() -> None:
    """A close at +0.5R with a wick that does not reach it must not trip breakeven.

    The rendered card has to describe that wick. Saying the bar closed through
    +0.5R fails this test while the code still reads high/low.
    """
    # Risk is 3. +0.5R is 101.5. The close is only 100.3 (+0.1R).
    wick_bar = [{"timestamp": 0, "high": 101.5, "low": 99.0, "close": 100.3}]
    low, high = candle_extremes_after_entry(wick_bar, entry_time_ms=0, now_ms=ONE_H, bar_ms=ONE_H)
    assert high == 101.5
    assert low == 99.0
    hit = trail_stop_with_source(_long_position(), best_low=low, best_high=high)
    assert hit is not None
    assert hit[1] == "breakeven"

    # Same function ignores the close field: a close of 101.5 with a high of 100.3
    # does not reach +0.5R.
    close_only = [{"timestamp": 0, "high": 100.3, "low": 99.5, "close": 101.5}]
    _low2, high2 = candle_extremes_after_entry(close_only, entry_time_ms=0, now_ms=ONE_H, bar_ms=ONE_H)
    assert high2 == 100.3
    assert trail_stop_with_source(_long_position(), best_low=99.0, best_high=high2) is None

    text = format_move_stop_advice(
        symbol="ADAUSDT",
        side="long",
        qty=Decimal("1"),
        old_stop=Decimal("97"),
        new_stop=Decimal("100.2"),
        mark=Decimal("101"),
        entry=Decimal("100"),
        initial_stop=Decimal("97"),
        expires_at_ms=1_700_000_000_000,
        source="breakeven",
    )
    assert "ไส้แท่ง 1h สปอตที่ปิดแล้วแตะ +0.5R" in text
    assert "หลังค่าธรรมเนียม ≈ +0.03R" in text
    assert "ห่าง mark +0.27R" in text
    assert "ล็อก ≈ +0.07R" in text
    assert "ถ้าเลื่อน" not in text
    assert "ปิดเกิน" not in text
    assert "แท่ง 1h ปิด" not in text
    assert "ราคาปิด" not in text
    assert "ปุ่มหมดอายุ" in text

    flushed = format_move_stop_advice(
        symbol="ADAUSDT",
        side="long",
        qty=Decimal("1"),
        old_stop=Decimal("97"),
        new_stop=Decimal("100.2"),
        mark=Decimal("101"),
        entry=Decimal("100"),
        initial_stop=Decimal("97"),
        expires_at_ms=1_700_000_000_000,
        source="breakeven",
        with_button=False,
    )
    assert MANUAL_MOVE_LINE in flushed
    assert "ถ้าเลื่อน ล็อก ≈" in flushed
    assert "ปุ่ม" not in flushed
    assert "หมดอายุ" not in flushed
    assert "ยังไม่ได้เลื่อน stop" in flushed
    assert "เลื่อนเองในแอป" in flushed
    assert "ไส้แท่ง 1h สปอตที่ปิดแล้วแตะ +0.5R" in flushed

    alert = format_progress_alert(
        symbol="ADAUSDT",
        side="long",
        level=Decimal("0.5"),
        r_now=Decimal("0.5"),
        mark=Decimal("101.5"),
        entry=Decimal("100"),
        stop=Decimal("97"),
        stop_r=Decimal("-1"),
        tick=Decimal("0.1"),
        with_button=False,
    )
    assert alert.splitlines()[2] == PROGRESS_RULE_LINE
    assert "ไส้แท่ง 1h สปอตที่ปิดแล้วแตะ +0.5R" in alert
    assert "trail เมื่อไส้แตะ +1R" in alert
    assert "ปิดเกิน" not in alert
    assert "แท่ง 1h ปิด" not in alert


def test_trail_card_names_the_trail_wick_not_breakeven() -> None:
    text = format_move_stop_advice(
        symbol="ADAUSDT",
        side="short",
        qty=Decimal("10"),
        old_stop=None,
        new_stop=Decimal("0.48"),
        mark=Decimal("0.45"),
        entry=Decimal("0.50"),
        initial_stop=Decimal("0.55"),
        expires_at_ms=1_762_000_000_000,
        source="trail",
    )
    assert "ไส้แท่ง 1h สปอตที่ปิดแล้วแตะ +1R" in text
    assert "+0.5R" not in text
