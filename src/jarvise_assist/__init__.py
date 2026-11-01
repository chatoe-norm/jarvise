"""Trade assistant: Telegram futures advice; orders only after owner confirm when gated."""

from __future__ import annotations

__all__ = ["ASSIST_SYMBOLS", "ASSIST_TIMEFRAME"]

ASSIST_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "XRPUSDT",
)
ASSIST_TIMEFRAME = "4h"
