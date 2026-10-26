"""Trade assistant: Telegram futures advice only. Never places orders."""

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
