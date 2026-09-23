"""Named point-in-time universes for paper analytics.

`paper_core` is the default two-asset crypto set Jarvise already backfills.
Listing dates are conservative lower bounds used for eligibility, not exchange
IPO timestamps.
"""

from __future__ import annotations

import sqlite3

from jarvise_ingest.db import record_membership

PAPER_CORE = "paper_core"

# 2021-01-01T00:00:00Z — matches the multi-year 4h backfill window.
_PAPER_CORE_LISTED_AT = 1_609_459_200_000

_PAPER_CORE_MEMBERS: tuple[tuple[str, str], ...] = (
    ("BTCUSDT", "crypto"),
    ("ETHUSDT", "crypto"),
)


def seed_paper_core(conn: sqlite3.Connection) -> int:
    """Ensure BTCUSDT / ETHUSDT are members of paper_core from 2021-01-01."""
    written = 0
    for symbol, asset_class in _PAPER_CORE_MEMBERS:
        written += record_membership(
            conn,
            universe_id=PAPER_CORE,
            symbol=symbol,
            asset_class=asset_class,
            listed_at=_PAPER_CORE_LISTED_AT,
        )
    return written
