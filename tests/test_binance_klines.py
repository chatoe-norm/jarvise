"""Only closed candles may enter the store.

Binance returns the in-progress candle as the last element of /api/v3/klines. Its
open/high/low/close keep changing until the interval ends, so persisting it makes
every derived value non-reproducible.
"""

import json
import time

import httpx
import pytest

from jarvise_ingest.providers.binance_klines import fetch_klines


def _kline(open_time_ms: int, close_time_ms: int, close: float) -> list:
    return [
        open_time_ms,
        f"{close - 0.4:.2f}",
        f"{close + 1.2:.2f}",
        f"{close - 1.1:.2f}",
        f"{close:.2f}",
        "12.5",
        close_time_ms,
        "780000.0",
        99,
        "6.1",
        "380000.0",
        "0",
    ]


def _client(rows: list[list]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(rows))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_klines_drops_the_unclosed_candle():
    now_ms = int(time.time() * 1000)
    closed = _kline(now_ms - 7_200_000, now_ms - 3_600_000, 100.0)
    still_open = _kline(now_ms - 3_600_000, now_ms + 3_600_000, 101.0)

    candles = fetch_klines("BTCUSDT", "1h", 2, client=_client([closed, still_open]))

    assert len(candles) == 1
    assert candles[0]["timestamp"] == now_ms - 7_200_000
    assert candles[0]["close"] == 100.0


def test_fetch_klines_keeps_a_candle_that_just_closed():
    now_ms = int(time.time() * 1000)
    closed = _kline(now_ms - 7_200_000, now_ms - 60_000, 100.0)

    candles = fetch_klines("BTCUSDT", "1h", 1, client=_client([closed]))

    assert len(candles) == 1


def test_fetch_klines_errors_when_only_an_open_candle_is_available():
    now_ms = int(time.time() * 1000)
    still_open = _kline(now_ms - 60_000, now_ms + 3_540_000, 100.0)

    with pytest.raises(RuntimeError, match="no closed candles"):
        fetch_klines("BTCUSDT", "1h", 1, client=_client([still_open]))
