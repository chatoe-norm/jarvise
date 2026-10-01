from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SpotBalance:
    venue: str
    asset: str
    free: Decimal
    locked: Decimal
    total: Decimal


@dataclass(frozen=True)
class ValuedBalance:
    """One spot balance with optional ~USD (None = unpriced)."""

    balance: SpotBalance
    usd: Decimal | None
    price_usd: Decimal | None = None


@dataclass(frozen=True)
class ValuationResult:
    """Valued rows plus total of priced assets only."""

    rows: list[ValuedBalance]
    total_usd: Decimal
    priced_count: int
    unpriced_count: int


@dataclass
class SyncResult:
    ok: bool
    dry_run: bool
    venue: str
    fetched_at_ms: int | None
    balances: list[SpotBalance]
    inserted: int
    error: str | None
