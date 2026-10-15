#!/usr/bin/env python3
"""Jarvise Volatility Stop-Loss & Chandelier Trailing Exit Calculator.

Calculates:
1. Volatility Invalidation Stop: >= 1.5x - 3.0x ATR(14) distance.
2. Chandelier Trailing Stop:
   - Long: Highest High(n) - mult * ATR(14)
   - Short: Lowest Low(n) + mult * ATR(14)
3. Two-Stage Profit Lock Progression:
   - Milestone 1: Breakeven (+20 bps fee buffer) at 0.5R.
   - Milestone 2: 1.0R Trail from best favorable excursion at 1.0R.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

DEFAULT_ATR_MULT = 1.5
DEFAULT_ATR_PERIOD = 14
DEFAULT_FEE_BPS = 20.0
DEFAULT_BREAKEVEN_R = 0.5
DEFAULT_TRAIL_R = 1.0


@dataclass(frozen=True)
class VolatilityStopResult:
    entry_price: float
    side: str
    atr: float
    atr_mult: float
    initial_stop_price: float
    risk_distance: float
    risk_distance_pct: float
    chandelier_stop: float | None = None
    two_stage_stop: float | None = None
    stop_source: str = "initial_stop"


def calculate_atr_stop(
    *,
    entry_price: float,
    side: str,
    atr_value: float,
    multiplier: float = DEFAULT_ATR_MULT,
) -> VolatilityStopResult:
    """Calculate entry stop-loss at multiplier * ATR."""
    side_clean = side.strip().lower()
    if side_clean not in {"long", "short"}:
        raise ValueError(f"Invalid side: {side}. Must be 'long' or 'short'")
    if entry_price <= 0:
        raise ValueError(f"Entry price must be positive, got {entry_price}")
    if atr_value <= 0:
        raise ValueError(f"ATR value must be positive, got {atr_value}")
    if multiplier < 1.5:
        raise ValueError(f"Multiplier must be >= 1.5x ATR per Jarvise doctrine, got {multiplier}")

    stop_distance = atr_value * multiplier
    if side_clean == "long":
        stop_price = round(entry_price - stop_distance, 4)
    else:
        stop_price = round(entry_price + stop_distance, 4)

    risk_dist = abs(entry_price - stop_price)
    risk_pct = (risk_dist / entry_price) * 100.0

    return VolatilityStopResult(
        entry_price=entry_price,
        side=side_clean,
        atr=atr_value,
        atr_mult=multiplier,
        initial_stop_price=stop_price,
        risk_distance=round(risk_dist, 4),
        risk_distance_pct=round(risk_pct, 4),
        stop_source="atr_initial",
    )


def calculate_chandelier_stop(
    *,
    side: str,
    highest_high: float,
    lowest_low: float,
    atr_value: float,
    multiplier: float = DEFAULT_ATR_MULT,
) -> float:
    """Calculate Chandelier trailing stop."""
    side_clean = side.strip().lower()
    dist = atr_value * multiplier
    if side_clean == "long":
        return round(highest_high - dist, 4)
    elif side_clean == "short":
        return round(lowest_low + dist, 4)
    else:
        raise ValueError(f"Invalid side: {side}")


def evaluate_two_stage_lock(
    *,
    side: str,
    entry_price: float,
    initial_stop_price: float,
    current_stop_price: float,
    best_price_since_entry: float,
    breakeven_r: float = DEFAULT_BREAKEVEN_R,
    fee_bps: float = DEFAULT_FEE_BPS,
) -> tuple[float, str]:
    """Evaluate two-stage lock progression: Breakeven at 0.5R, Trail at 1.0R."""
    side_clean = side.strip().lower()
    risk = abs(entry_price - initial_stop_price)
    if risk <= 0:
        return current_stop_price, "initial_stop"

    fee_buffer = entry_price * (fee_bps / 10_000.0)

    if side_clean == "long":
        favorable = best_price_since_entry - entry_price
        # Stage 2: 1.0R Trail from best peak
        if favorable >= risk:
            candidate = round(best_price_since_entry - risk, 4)
            if candidate > current_stop_price:
                return candidate, "trail"
        # Stage 1: Breakeven (+20 bps fee buffer)
        elif favorable >= breakeven_r * risk:
            candidate = round(entry_price + fee_buffer, 4)
            if candidate > current_stop_price:
                return candidate, "breakeven"
    elif side_clean == "short":
        favorable = entry_price - best_price_since_entry
        # Stage 2: 1.0R Trail from best low
        if favorable >= risk:
            candidate = round(best_price_since_entry + risk, 4)
            if candidate < current_stop_price:
                return candidate, "trail"
        # Stage 1: Breakeven (+20 bps fee buffer)
        elif favorable >= breakeven_r * risk:
            candidate = round(entry_price - fee_buffer, 4)
            if candidate < current_stop_price:
                return candidate, "breakeven"

    return current_stop_price, "initial_stop"


def run_tests() -> int:
    """Run comprehensive test suite verifying ATR stop, Chandelier, and Two-Stage lock."""
    print("==================================================")
    print("Running Jarvise Volatility Stop-Loss & Lock Tests")
    print("==================================================")

    # Test 1: Long ATR 1.5x entry stop
    entry = 68000.0
    atr = 1200.0
    long_res = calculate_atr_stop(entry_price=entry, side="long", atr_value=atr, multiplier=1.5)
    expected_long_stop = 68000.0 - 1.5 * 1200.0  # 66200.0
    assert long_res.initial_stop_price == expected_long_stop, (
        f"Expected {expected_long_stop}, got {long_res.initial_stop_price}"
    )
    assert long_res.risk_distance == 1800.0
    print(
        f"[PASS] Test 1: Long 1.5x ATR Stop -> Entry: ${entry}, ATR: ${atr}, Stop: ${long_res.initial_stop_price} (Dist: ${long_res.risk_distance})"
    )

    # Test 2: Short ATR 1.5x entry stop
    short_res = calculate_atr_stop(entry_price=entry, side="short", atr_value=atr, multiplier=1.5)
    expected_short_stop = 68000.0 + 1.5 * 1200.0  # 69800.0
    assert short_res.initial_stop_price == expected_short_stop, (
        f"Expected {expected_short_stop}, got {short_res.initial_stop_price}"
    )
    print(
        f"[PASS] Test 2: Short 1.5x ATR Stop -> Entry: ${entry}, ATR: ${atr}, Stop: ${short_res.initial_stop_price}"
    )

    # Test 3: Reject stops tighter than 1.5x ATR
    try:
        calculate_atr_stop(entry_price=entry, side="long", atr_value=atr, multiplier=1.2)
        print("[FAIL] Test 3: Allowed < 1.5x ATR stop")
        return 1
    except ValueError as e:
        assert "Multiplier must be >= 1.5x ATR" in str(e)
        print(f"[PASS] Test 3: Properly rejected tight stop (<1.5x ATR): {e}")

    # Test 4: Chandelier Exit
    highest = 70500.0
    chand_long = calculate_chandelier_stop(
        side="long", highest_high=highest, lowest_low=67000.0, atr_value=atr, multiplier=1.5
    )
    assert chand_long == round(70500.0 - 1.5 * 1200.0, 4)  # 68700.0
    print(f"[PASS] Test 4: Long Chandelier Stop -> High: ${highest}, Stop: ${chand_long}")

    # Test 5: Two-Stage Lock — Stage 1 Breakeven (+20 bps) triggered at +0.5R
    # Risk R = 1800. At favorable travel = 950 (> 0.5R = 900), stop moves to entry + 20 bps = 68000 * 1.002 = 68136.0
    be_stop, be_source = evaluate_two_stage_lock(
        side="long",
        entry_price=entry,
        initial_stop_price=long_res.initial_stop_price,
        current_stop_price=long_res.initial_stop_price,
        best_price_since_entry=68950.0,  # +950 favorable
    )
    assert be_source == "breakeven"
    assert be_stop == 68136.0, f"Expected 68136.0, got {be_stop}"
    print(
        f"[PASS] Test 5: Stage 1 Breakeven (+20 bps fee buffer) -> Stop moved to ${be_stop} (Source: {be_source})"
    )

    # Test 6: Two-Stage Lock — Stage 2 Trailing 1.0R triggered at >= 1.0R
    # Favorable travel = 2200 (> 1.0R = 1800). Peak = 70200. New stop = 70200 - 1800 = 68400.0
    trail_stop, trail_source = evaluate_two_stage_lock(
        side="long",
        entry_price=entry,
        initial_stop_price=long_res.initial_stop_price,
        current_stop_price=be_stop,
        best_price_since_entry=70200.0,  # +2200 favorable
    )
    assert trail_source == "trail"
    assert trail_stop == 68400.0, f"Expected 68400.0, got {trail_stop}"
    print(f"[PASS] Test 6: Stage 2 Trailing 1.0R -> Stop moved to ${trail_stop} (Source: {trail_source})")

    # Test 7: Capital Preservation — Stop never loosens on retrace
    retrace_stop, retrace_source = evaluate_two_stage_lock(
        side="long",
        entry_price=entry,
        initial_stop_price=long_res.initial_stop_price,
        current_stop_price=trail_stop,
        best_price_since_entry=70200.0,  # Peak unchanged, current price pulls back
    )
    assert retrace_stop == trail_stop
    print(f"[PASS] Test 7: Invariant Check -> Stop never loosens (${retrace_stop} preserved)")

    print("==================================================")
    print("ALL 7 TESTS PASSED SUCCESSFULLY (100% GREEN)")
    print("==================================================")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvise Volatility Stop-Loss Calculator")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("--symbol", type=str, default="BTCUSDT", help="Trading asset symbol")
    parser.add_argument("--side", type=str, choices=["long", "short"], default="long", help="Trade side")
    parser.add_argument("--entry", type=float, default=68000.0, help="Entry price")
    parser.add_argument("--atr", type=float, default=1200.0, help="ATR(14) value")
    parser.add_argument("--mult", type=float, default=1.5, help="ATR multiplier (min 1.5)")

    args = parser.parse_args()

    if args.test:
        return run_tests()

    res = calculate_atr_stop(
        entry_price=args.entry,
        side=args.side,
        atr_value=args.atr,
        multiplier=args.mult,
    )
    print(f"Asset: {args.symbol}")
    print(f"Side: {res.side.upper()}")
    print(f"Entry: ${res.entry_price:,.2f}")
    print(f"ATR(14): ${res.atr:,.2f} (Mult: {res.atr_mult}x)")
    print(f"Calculated Stop-Loss: ${res.initial_stop_price:,.2f}")
    print(f"Risk Distance: ${res.risk_distance:,.2f} ({res.risk_distance_pct:.2f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
