"""Supported candle timeframes and their durations in milliseconds."""

from __future__ import annotations

INTERVAL_MS: dict[str, int] = {
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

ALLOWED_INTERVALS = frozenset(INTERVAL_MS)
