"""Adjustable owner dep/wd settings (env, default, hard bounds).

Pattern as ``JARVISE_APPROVAL_TIMEOUT_MIN``: unset or unparseable falls back to the
default; a parsed value is clamped into ``[min, max]``. Read at call time so a
restart is enough after an ``.env`` change. No secrets.
"""

from __future__ import annotations

import os
from typing import Final

HOUR_MS: Final = 3_600_000
DAY_MS: Final = 86_400_000

# Live ``p owner dep|wd``: max age of the Binance transfer at preview/confirm.
OWNER_TX_MAX_AGE_HOURS_ENV: Final = "JARVISE_OWNER_TX_MAX_AGE_HOURS"
OWNER_TX_MAX_AGE_HOURS_DEFAULT: Final = 6
OWNER_TX_MAX_AGE_HOURS_MIN: Final = 1
OWNER_TX_MAX_AGE_HOURS_MAX: Final = 48

# Historical CLI ``assist partner-record-owner-tx``: max lookback of --effective-at-ms.
OWNER_HIST_MAX_LOOKBACK_DAYS_ENV: Final = "JARVISE_OWNER_HIST_MAX_LOOKBACK_DAYS"
OWNER_HIST_MAX_LOOKBACK_DAYS_DEFAULT: Final = 30
OWNER_HIST_MAX_LOOKBACK_DAYS_MIN: Final = 1
OWNER_HIST_MAX_LOOKBACK_DAYS_MAX: Final = 365


def bounded_int_env(name: str, *, default: int, lo: int, hi: int) -> int:
    """Integer env setting: unset/invalid -> ``default``; else clamped to ``[lo, hi]``."""
    if not lo <= default <= hi:
        raise ValueError("default outside bounds")
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(lo, min(hi, value))


def owner_tx_max_age_hours() -> int:
    return bounded_int_env(
        OWNER_TX_MAX_AGE_HOURS_ENV,
        default=OWNER_TX_MAX_AGE_HOURS_DEFAULT,
        lo=OWNER_TX_MAX_AGE_HOURS_MIN,
        hi=OWNER_TX_MAX_AGE_HOURS_MAX,
    )


def owner_tx_max_age_ms() -> int:
    return owner_tx_max_age_hours() * HOUR_MS


def owner_hist_max_lookback_days() -> int:
    return bounded_int_env(
        OWNER_HIST_MAX_LOOKBACK_DAYS_ENV,
        default=OWNER_HIST_MAX_LOOKBACK_DAYS_DEFAULT,
        lo=OWNER_HIST_MAX_LOOKBACK_DAYS_MIN,
        hi=OWNER_HIST_MAX_LOOKBACK_DAYS_MAX,
    )


def owner_hist_max_lookback_ms() -> int:
    return owner_hist_max_lookback_days() * DAY_MS
