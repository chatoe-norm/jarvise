"""Master live-trading gate. Default off."""

from __future__ import annotations

import os

_TRUE = frozenset({"1", "true", "yes", "on"})


def live_trading_enabled() -> bool:
    """True only when JARVISE_LIVE_TRADING is explicitly enabled."""
    raw = (os.environ.get("JARVISE_LIVE_TRADING") or "").strip().lower()
    return raw in _TRUE
