"""Paginated history fetch.

One /api/v3/klines request caps at 1000 candles, which is 41 days at 1h and 166
days at 4h. Multi-year history needs `startTime` paging.
"""

import json
import time

import httpx
import pytest

from jarvise_ingest.providers.binance_klines import fetch_klines_range
from jarvise_ingest.timeframes import INTERVAL_MS

STEP = INTERVAL_MS["1h"]


def _kline(open_time_ms: int, step_ms: int, close: float) -> list:
    return [
        open_time_ms,
        f"{close - 0.4:.2f}",
        f"{close + 1.2:.2f}",
        f"{close - 1.1:.2f}",
        f"{close:.2f}",
        "12.5",
        open_time_ms + step_ms - 1,
        "780000.0",
        99,
        "6.1",
        "380000.0",
        "0",
    ]


class _Exchange:
    """Serves a fixed universe of klines the way Binance pages them."""

    def __init__(self, first_open_ms: int, count: int, step_ms: int = STEP):
        self.rows = [
            _kline(first_open_ms + i * step_ms, step_ms, 100.0 + i)
            for i in range(count)
        ]
        self.requests: list[dict] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            self.requests.append(params)
            start = int(params.get("startTime", 0))
            limit = int(params.get("limit", 500))
            page = [row for row in self.rows if row[0] >= start][:limit]
            return httpx.Response(200, content=json.dumps(page))

        return httpx.Client(transport=httpx.MockTransport(handler))


def _past(count: int) -> int:
    """Open time of the first candle in a run of `count` already-closed candles."""
    return int(time.time() * 1000) - (count + 1) * STEP


def test_range_fetch_pages_until_the_history_is_exhausted():
    first = _past(2500)
    exchange = _Exchange(first, 2500)

    candles = fetch_klines_range(
        "BTCUSDT", "1h", first, client=exchange.client(), page_limit=1000
    )

    assert len(candles) == 2500
    assert len(exchange.requests) == 3
    assert candles[0]["timestamp"] == first
    assert candles[-1]["timestamp"] == first + 2499 * STEP


def test_range_fetch_advances_start_time_between_pages():
    first = _past(1500)
    exchange = _Exchange(first, 1500)

    fetch_klines_range(
        "BTCUSDT", "1h", first, client=exchange.client(), page_limit=1000
    )

    starts = [int(r["startTime"]) for r in exchange.requests]
    assert starts == [first, first + 1000 * STEP]


def test_range_fetch_returns_no_duplicate_timestamps():
    first = _past(2500)
    exchange = _Exchange(first, 2500)

    candles = fetch_klines_range(
        "BTCUSDT", "1h", first, client=exchange.client(), page_limit=1000
    )

    stamps = [c["timestamp"] for c in candles]
    assert len(set(stamps)) == len(stamps)


def test_range_fetch_stops_at_until():
    first = _past(2500)
    exchange = _Exchange(first, 2500)
    until = first + 1200 * STEP

    candles = fetch_klines_range(
        "BTCUSDT", "1h", first, until_ms=until, client=exchange.client(), page_limit=1000
    )

    assert all(c["timestamp"] < until for c in candles)
    assert len(candles) == 1200


def test_range_fetch_still_drops_the_unclosed_candle():
    now_ms = int(time.time() * 1000)
    first = now_ms - 3 * STEP
    exchange = _Exchange(first, 4)  # the 4th candle closes in the future

    candles = fetch_klines_range(
        "BTCUSDT", "1h", first, client=exchange.client(), page_limit=1000
    )

    assert len(candles) == 3


def test_range_fetch_errors_when_the_window_holds_no_closed_candle():
    now_ms = int(time.time() * 1000)
    exchange = _Exchange(now_ms, 1)

    with pytest.raises(RuntimeError, match="no closed candles"):
        fetch_klines_range("BTCUSDT", "1h", now_ms, client=exchange.client())


def test_range_fetch_rejects_an_unsupported_timeframe():
    with pytest.raises(ValueError, match="unsupported timeframe"):
        fetch_klines_range("BTCUSDT", "3m", _past(10))


def test_range_fetch_gives_up_rather_than_paging_forever():
    first = _past(5000)
    exchange = _Exchange(first, 5000)

    with pytest.raises(RuntimeError, match="too many pages"):
        fetch_klines_range(
            "BTCUSDT",
            "1h",
            first,
            client=exchange.client(),
            page_limit=1000,
            max_pages=2,
        )
