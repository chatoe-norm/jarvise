"""Shared dashboard and report freshness thresholds."""

from __future__ import annotations

import os
from typing import Any

from jarvise_ingest.timeframes import INTERVAL_MS
from jarvise_risk.market_safety import load_market_safety_config

# Two missed 15-minute risk-monitor runs. Display only; not a market-safety age.
RISK_MONITOR_STALE_AFTER_MIN = 35.0


def paper_timeframe() -> str:
    return os.environ.get("JARVISE_PAPER_TIMEFRAME") or "4h"


def risk_stale_threshold_ms(shortest_interval_ms: int) -> int:
    """Max close-age of the newest candle before risk-monitor skips MTM/stops.

    Age is ``now - (open + INTERVAL_MS)``. ``JARVISE_RISK_STALE_MS`` overrides.
    Default is 2× the shortest stored timeframe (1h → 7_200_000 ms of close-age,
    so a 1h symbol goes stale only when the newest open is older than 3 h).
    """
    raw = (os.environ.get("JARVISE_RISK_STALE_MS") or "").strip()
    if raw:
        return int(raw)
    return int(shortest_interval_ms) * 2


def dashboard_freshness() -> dict[str, Any]:
    """Stale thresholds from market safety and the paper bar. No new policy."""
    timeframe = paper_timeframe()
    return {
        "market_safety_max_age_min": load_market_safety_config().max_age_min,
        "paper_timeframe": timeframe,
        "paper_bar_ms": INTERVAL_MS.get(timeframe),
        "risk_monitor_stale_after_min": RISK_MONITOR_STALE_AFTER_MIN,
    }
