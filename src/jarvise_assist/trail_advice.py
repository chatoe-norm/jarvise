"""Shared closed-1h trail advice for assist watch, menu, and move-stop tap."""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

import httpx

from jarvise_assist.sizing import round_level_to_tick
from jarvise_paper.profit_lock import trail_stop_with_source

ONE_H_MS = 3_600_000
FIFTEEN_M_MS = 900_000
_ENTRY_BAR_CACHE_MAX = 256
_ENTRY_BAR_FAIL_COOLDOWN_MS = 120_000
_ENTRY_BAR_FETCH_TIMEOUT_S = 5.0
TrailSource = Literal["breakeven", "trail"]
EntryBarTag = Literal["aligned", "open", "used", "empty"]

logger = logging.getLogger(__name__)

_ENTRY_BAR_CACHE: dict[tuple[str, int], tuple[float | None, float | None]] = {}
_ENTRY_BAR_FAIL_MEMO: dict[tuple[str, int], int] = {}


def fetch_entry_bar_15m(symbol: str, start_ms: int, until_ms: int) -> list[dict]:
    """Public GET 15m klines for the post-fill slice of the entry hour. No API key."""
    try:
        from jarvise_ingest.providers.binance_klines import fetch_klines_range

        with httpx.Client(timeout=_ENTRY_BAR_FETCH_TIMEOUT_S) as client:
            return fetch_klines_range(
                symbol,
                "15m",
                start_ms,
                until_ms=until_ms,
                page_limit=4,
                max_pages=1,
                client=client,
            )
    except Exception:
        logger.warning("assist trail_advice: entry-bar 15m fetch failed")
        return []


_ENTRY_BAR_FETCH: Callable[[str, int, int], list[dict]] = fetch_entry_bar_15m


def _remember_entry_bar_failure(cache_key: tuple[str, int], now_ms: int) -> None:
    if len(_ENTRY_BAR_FAIL_MEMO) >= _ENTRY_BAR_CACHE_MAX:
        _ENTRY_BAR_FAIL_MEMO.clear()
    _ENTRY_BAR_FAIL_MEMO[cache_key] = int(now_ms) + _ENTRY_BAR_FAIL_COOLDOWN_MS


def candle_extremes_after_entry(
    candles: list[dict[str, Any]],
    *,
    entry_time_ms: int,
    now_ms: int,
    bar_ms: int = ONE_H_MS,
) -> tuple[float | None, float | None]:
    """Best high/low of bars opened at or after entry and closed by now.

    Bars that opened before entry are excluded so pre-entry extremes cannot
    trip breakeven / 1R trail advice (same leak class as replay PR #179).
    """
    best_high: float | None = None
    best_low: float | None = None
    for row in candles:
        ts = int(row["timestamp"])
        close_ms = ts + int(bar_ms)
        if ts < int(entry_time_ms) or close_ms > int(now_ms):
            continue
        high = row.get("high")
        low = row.get("low")
        if high is not None:
            h = float(high)
            best_high = h if best_high is None else max(best_high, h)
        if low is not None:
            lo = float(low)
            best_low = lo if best_low is None else min(best_low, lo)
    return best_low, best_high


def entry_bar_extremes(
    *,
    symbol: str,
    entry_time_ms: int,
    now_ms: int,
    fetch: Callable[[str, int, int], list[dict]] | None = None,
) -> tuple[float | None, float | None]:
    """Low/high from closed 15m bars after the fill inside the entry hour.

    The 1h bar that contains the fill opens on the hour and is dropped by
    ``candle_extremes_after_entry`` unless the fill is exactly on the hour.
    Once that hour has closed, this fetches the public 15m slice from the
    first 15m open at or after the fill through the hour close.
    """
    bar_open = int(entry_time_ms) - int(entry_time_ms) % ONE_H_MS
    if int(entry_time_ms) == bar_open:
        return None, None
    bar_close = bar_open + ONE_H_MS
    if bar_close > int(now_ms):
        return None, None
    rem = int(entry_time_ms) % FIFTEEN_M_MS
    start = int(entry_time_ms) if rem == 0 else int(entry_time_ms) + (FIFTEEN_M_MS - rem)
    if start >= bar_close:
        return None, None

    cache_key = (symbol.upper(), int(entry_time_ms))
    cached = _ENTRY_BAR_CACHE.get(cache_key)
    if cached is not None:
        return cached
    retry_after = _ENTRY_BAR_FAIL_MEMO.get(cache_key)
    if retry_after is not None and int(now_ms) < retry_after:
        return None, None

    fetcher = fetch if fetch is not None else _ENTRY_BAR_FETCH
    try:
        rows = fetcher(symbol, start, bar_close)
    except Exception:
        logger.warning("assist trail_advice: entry-bar 15m fetch failed")
        _remember_entry_bar_failure(cache_key, now_ms)
        return None, None
    best_low: float | None = None
    best_high: float | None = None
    for row in rows:
        ts = int(row["timestamp"])
        if ts < int(entry_time_ms) or ts + FIFTEEN_M_MS > bar_close:
            continue
        high = row.get("high")
        low = row.get("low")
        if high is not None:
            h = float(high)
            best_high = h if best_high is None else max(best_high, h)
        if low is not None:
            lo = float(low)
            best_low = lo if best_low is None else min(best_low, lo)
    if best_low is None and best_high is None:
        _remember_entry_bar_failure(cache_key, now_ms)
        return None, None
    _ENTRY_BAR_FAIL_MEMO.pop(cache_key, None)
    if len(_ENTRY_BAR_CACHE) >= _ENTRY_BAR_CACHE_MAX:
        _ENTRY_BAR_CACHE.clear()
    _ENTRY_BAR_CACHE[cache_key] = (best_low, best_high)
    return best_low, best_high


def entry_bar_15m_label(
    *,
    symbol: str,
    entry_time_ms: int,
    now_ms: int,
) -> EntryBarTag:
    """Observability tag for the entry-hour 15m path (no secrets)."""
    bar_open = int(entry_time_ms) - int(entry_time_ms) % ONE_H_MS
    if int(entry_time_ms) == bar_open:
        return "aligned"
    if bar_open + ONE_H_MS > int(now_ms):
        return "open"
    if (symbol.upper(), int(entry_time_ms)) in _ENTRY_BAR_CACHE:
        return "used"
    return "empty"


def post_entry_extremes(
    candles_1h: list[dict[str, Any]],
    *,
    symbol: str,
    entry_time_ms: int,
    now_ms: int,
    fetch: Callable[[str, int, int], list[dict]] | None = None,
) -> tuple[float | None, float | None]:
    """Combine closed 1h post-entry extremes with the entry-hour 15m slice."""
    c_low, c_high = candle_extremes_after_entry(
        candles_1h,
        entry_time_ms=entry_time_ms,
        now_ms=now_ms,
        bar_ms=ONE_H_MS,
    )
    e_low, e_high = entry_bar_extremes(
        symbol=symbol,
        entry_time_ms=entry_time_ms,
        now_ms=now_ms,
        fetch=fetch,
    )
    best_low: float | None = None
    best_high: float | None = None
    for lo in (c_low, e_low):
        if lo is not None:
            best_low = lo if best_low is None else min(best_low, lo)
    for hi in (c_high, e_high):
        if hi is not None:
            best_high = hi if best_high is None else max(best_high, hi)
    return best_low, best_high


def current_trail_advice(
    *,
    side: str,
    entry: Decimal,
    initial_stop: Decimal,
    candles_1h: list[dict[str, Any]],
    entry_time_ms: int,
    now_ms: int,
    tick: Decimal,
    symbol: str,
    entry_bar_fetch: Callable[[str, int, int], list[dict]] | None = None,
) -> tuple[Decimal, TrailSource] | None:
    """Rounded trail/breakeven advice from closed 1h (+ entry-hour 15m), or None.

    Uses the frozen initial stop as the R anchor and as ``stop_price`` for
    ``trail_stop_with_source`` (never the live mark as an extreme).
    """
    best_low, best_high = post_entry_extremes(
        candles_1h,
        symbol=symbol,
        entry_time_ms=entry_time_ms,
        now_ms=now_ms,
        fetch=entry_bar_fetch,
    )
    trail_pos = {
        "side": side,
        "entry_price": float(entry),
        "initial_stop_price": float(initial_stop),
        "stop_price": float(initial_stop),
    }
    trail = trail_stop_with_source(trail_pos, best_low=best_low, best_high=best_high)
    if trail is None:
        return None
    suggested, source = trail
    if source not in {"breakeven", "trail"}:
        return None
    rounded = round_level_to_tick(Decimal(str(suggested)), tick, side=side, kind="stop")
    return rounded, source  # type: ignore[return-value]
