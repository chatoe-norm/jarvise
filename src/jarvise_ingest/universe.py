"""Named point-in-time universes for paper analytics.

`paper_core` is the default two-asset crypto set Jarvise already backfills.
`paper_liquid` is an opt-in StaticPairList-style extension (BTC/ETH/SOL/BNB).
Listing dates are conservative lower bounds used for eligibility, not exchange
IPO timestamps.
"""

from __future__ import annotations

import sqlite3

from jarvise_ingest.db import record_membership

PAPER_CORE = "paper_core"
PAPER_LIQUID = "paper_liquid"
PAPER_EQUITY = "paper_equity"

# 2021-01-01T00:00:00Z — matches the multi-year 4h backfill window.
_PAPER_CORE_LISTED_AT = 1_609_459_200_000
# SPY/QQQ long history; conservative lower bound for eligibility.
_PAPER_EQUITY_LISTED_AT = 1_262_304_000_000  # 2010-01-01 UTC

_PAPER_CORE_MEMBERS: tuple[tuple[str, str], ...] = (
    ("BTCUSDT", "crypto"),
    ("ETHUSDT", "crypto"),
)
_PAPER_LIQUID_MEMBERS: tuple[tuple[str, str], ...] = (
    ("BTCUSDT", "crypto"),
    ("ETHUSDT", "crypto"),
    ("SOLUSDT", "crypto"),
    ("BNBUSDT", "crypto"),
)
_PAPER_EQUITY_MEMBERS: tuple[tuple[str, str], ...] = (
    ("SPY", "equity"),
    ("QQQ", "equity"),
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


def seed_paper_liquid(conn: sqlite3.Connection) -> int:
    """Ensure BTC/ETH/SOL/BNB are members of paper_liquid from 2021-01-01.

    Does not rewrite paper_core. Opt in with JARVISE_PAPER_UNIVERSE=paper_liquid.
    """
    written = 0
    for symbol, asset_class in _PAPER_LIQUID_MEMBERS:
        written += record_membership(
            conn,
            universe_id=PAPER_LIQUID,
            symbol=symbol,
            asset_class=asset_class,
            listed_at=_PAPER_CORE_LISTED_AT,
        )
    return written


def seed_paper_equity(conn: sqlite3.Connection) -> int:
    """Ensure SPY / QQQ are members of paper_equity (US ETF, Stooq ingest)."""
    written = 0
    for symbol, asset_class in _PAPER_EQUITY_MEMBERS:
        written += record_membership(
            conn,
            universe_id=PAPER_EQUITY,
            symbol=symbol,
            asset_class=asset_class,
            listed_at=_PAPER_EQUITY_LISTED_AT,
        )
    return written


def seed_named_universe(conn: sqlite3.Connection, universe_id: str) -> int:
    """Seed a known universe id; unknown ids are a no-op (0 rows)."""
    if universe_id == PAPER_CORE:
        return seed_paper_core(conn)
    if universe_id == PAPER_LIQUID:
        return seed_paper_liquid(conn)
    if universe_id == PAPER_EQUITY:
        return seed_paper_equity(conn)
    return 0
