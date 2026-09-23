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


@dataclass
class SyncResult:
    ok: bool
    dry_run: bool
    venue: str
    fetched_at_ms: int | None
    balances: list[SpotBalance]
    inserted: int
    error: str | None
