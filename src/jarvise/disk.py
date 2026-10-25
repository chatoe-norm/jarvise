"""Disk free-space check for ingest-health (alert before the host fills up)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from jarvise_ingest.db import set_paper_account_value

DISK_LOW_KEY = "ingest_disk_low"


def disk_min_free_pct() -> float:
    raw = (os.environ.get("JARVISE_DISK_MIN_FREE_PCT") or "").strip()
    return float(raw) if raw else 15.0


def disk_min_free_gb() -> float:
    raw = (os.environ.get("JARVISE_DISK_MIN_FREE_GB") or "").strip()
    return float(raw) if raw else 2.0


def disk_free(path: Path | str) -> dict[str, Any]:
    """Measure free space on the filesystem that holds ``path``.

    Returns bytes, percent, and ``disk_low``. On ``OSError``, returns
    ``disk_error`` and does not claim low.
    """
    target = Path(path)
    probe = target if target.exists() else target.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return {
            "disk_free_bytes": None,
            "disk_total_bytes": None,
            "disk_free_pct": None,
            "disk_low": False,
            "disk_error": f"{type(exc).__name__}: {exc}",
        }
    free = int(usage.free)
    total = int(usage.total)
    free_pct = round(100.0 * free / total, 2) if total > 0 else 0.0
    free_gb = free / (1024.0**3)
    low = free_pct < disk_min_free_pct() or free_gb < disk_min_free_gb()
    return {
        "disk_free_bytes": free,
        "disk_total_bytes": total,
        "disk_free_pct": free_pct,
        "disk_low": low,
    }


def read_disk_low_flag(conn: Any) -> bool | None:
    row = conn.execute(
        "SELECT value FROM paper_account WHERE key=?",
        (DISK_LOW_KEY,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0]) >= 0.5


def write_disk_low_flag(conn: Any, low: bool) -> None:
    set_paper_account_value(conn, DISK_LOW_KEY, 1.0 if low else 0.0)
    conn.commit()


def disk_edge_alerted(conn: Any, low_now: bool) -> str | None:
    """Return 'low' or 'ok' when a Telegram edge should fire; else None."""
    prev = read_disk_low_flag(conn)
    alerted: str | None = None
    if low_now and prev is not True:
        alerted = "low"
    elif not low_now and prev is True:
        alerted = "ok"
    write_disk_low_flag(conn, low_now)
    return alerted
