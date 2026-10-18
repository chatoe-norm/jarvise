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


def dashboard_freshness() -> dict[str, Any]:
    """Stale thresholds from market safety and the paper bar. No new policy."""
    timeframe = paper_timeframe()
    return {
        "market_safety_max_age_min": load_market_safety_config().max_age_min,
        "paper_timeframe": timeframe,
        "paper_bar_ms": INTERVAL_MS.get(timeframe),
        "risk_monitor_stale_after_min": RISK_MONITOR_STALE_AFTER_MIN,
    }
