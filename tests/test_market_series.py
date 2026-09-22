"""Stored indicators must be a deterministic function of the stored candle series.

Before this suite, indicators were computed from whichever fetch window happened
to contain a candle, so the same candle got different values on every ingest.
"""

from pathlib import Path

import pytest

from jarvise_ingest.db import open_db, upsert_market_technicals
from jarvise_ingest.series import find_gaps, recompute_indicators

SYMBOL = "BTCUSDT"
TIMEFRAME = "1h"
EPOCH_MS = 1_700_000_000_000
STEP_MS = 3_600_000


def _candles(start: int, count: int) -> list[dict]:
    rows = []
    for i in range(start, start + count):
        close = 100.0 + i * 0.5 + (i % 7) - (i % 3)
        rows.append(
            {
                "symbol": SYMBOL,
                "timestamp": EPOCH_MS + i * STEP_MS,
                "timeframe": TIMEFRAME,
                "open": close - 0.4,
                "high": close + 1.2,
                "low": close - 1.1,
                "close": close,
                "volume": 10.0 + i,
            }
        )
    return rows


def _stored(conn, column: str, index: int):
    cur = conn.execute(
        f"SELECT {column} FROM market_technicals "  # noqa: S608 — column is a literal
        "WHERE symbol=? AND timeframe=? AND timestamp=?",
        (SYMBOL, TIMEFRAME, EPOCH_MS + index * STEP_MS),
    )
    return cur.fetchone()[0]


def test_overlapping_ingests_agree_with_one_whole_ingest(tmp_path: Path):
    split = open_db(tmp_path / "split.db")
    upsert_market_technicals(split, _candles(0, 200))
    recompute_indicators(split, SYMBOL, TIMEFRAME)
    upsert_market_technicals(split, _candles(100, 200))
    recompute_indicators(split, SYMBOL, TIMEFRAME)

    whole = open_db(tmp_path / "whole.db")
    upsert_market_technicals(whole, _candles(0, 300))
    recompute_indicators(whole, SYMBOL, TIMEFRAME)

    for index in (150, 250, 299):
        assert _stored(split, "atr_14", index) == _stored(whole, "atr_14", index)
        assert _stored(split, "rsi_14", index) == _stored(whole, "rsi_14", index)
        assert _stored(split, "ema_20", index) == _stored(whole, "ema_20", index)
    split.close()
    whole.close()


def test_appending_candles_does_not_change_earlier_values(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    upsert_market_technicals(conn, _candles(0, 200))
    recompute_indicators(conn, SYMBOL, TIMEFRAME)
    before = _stored(conn, "atr_14", 199)
    assert before is not None

    upsert_market_technicals(conn, _candles(200, 200))
    recompute_indicators(conn, SYMBOL, TIMEFRAME)

    assert _stored(conn, "atr_14", 199) == before
    conn.close()


def test_ema_200_stays_null_until_the_series_is_long_enough(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    upsert_market_technicals(conn, _candles(0, 200))
    recompute_indicators(conn, SYMBOL, TIMEFRAME)

    assert _stored(conn, "ema_200", 199) is None
    conn.close()


def test_ema_200_is_published_once_warm(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    upsert_market_technicals(conn, _candles(0, 700))
    recompute_indicators(conn, SYMBOL, TIMEFRAME)

    assert _stored(conn, "ema_200", 659) is None
    assert _stored(conn, "ema_200", 660) is not None
    conn.close()


def test_recompute_reports_rows_touched(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    upsert_market_technicals(conn, _candles(0, 120))

    assert recompute_indicators(conn, SYMBOL, TIMEFRAME) == 120
    conn.close()


def test_recompute_on_empty_series_is_a_noop(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")

    assert recompute_indicators(conn, SYMBOL, TIMEFRAME) == 0
    conn.close()


def test_recompute_refuses_a_series_with_a_missing_close(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    upsert_market_technicals(conn, _candles(0, 30))
    conn.execute(
        "UPDATE market_technicals SET close=NULL WHERE symbol=? AND timestamp=?",
        (SYMBOL, EPOCH_MS + 10 * STEP_MS),
    )
    conn.commit()

    with pytest.raises(ValueError, match="incomplete candle"):
        recompute_indicators(conn, SYMBOL, TIMEFRAME)
    conn.close()


def test_find_gaps_is_empty_for_a_contiguous_series(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    upsert_market_technicals(conn, _candles(0, 50))

    assert find_gaps(conn, SYMBOL, TIMEFRAME) == []
    conn.close()


def test_find_gaps_reports_the_missing_span(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")
    upsert_market_technicals(conn, _candles(0, 20) + _candles(25, 10))

    gaps = find_gaps(conn, SYMBOL, TIMEFRAME)

    # candles 20..24 are absent: five missing between index 19 and index 25
    assert gaps == [(EPOCH_MS + 19 * STEP_MS, EPOCH_MS + 25 * STEP_MS, 5)]
    conn.close()


def test_find_gaps_on_an_empty_series_is_empty(tmp_path: Path):
    conn = open_db(tmp_path / "t.db")

    assert find_gaps(conn, SYMBOL, TIMEFRAME) == []
    conn.close()


def test_upsert_does_not_carry_indicator_columns(tmp_path: Path):
    """OHLCV is ingest truth; indicators are derived by recompute only."""
    conn = open_db(tmp_path / "t.db")
    rows = _candles(0, 30)
    rows[-1]["atr_14"] = 999.0

    upsert_market_technicals(conn, rows)

    assert _stored(conn, "atr_14", 29) is None
    conn.close()
