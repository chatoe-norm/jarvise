from pathlib import Path

from jarvise_ingest.db import (
    count_indicator_ready,
    open_db,
    upsert_market_technicals,
    write_indicators,
)
from jarvise_ingest.health import ingest_health

H4 = 14_400_000
T0 = 1_700_000_000_000


def _candles(conn, symbol: str, n: int, *, skip_index: int | None = None) -> None:
    rows = []
    for i in range(n):
        if i == skip_index:
            continue
        rows.append(
            {
                "symbol": symbol,
                "timestamp": T0 + i * H4,
                "timeframe": "4h",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1.0,
            }
        )
    upsert_market_technicals(conn, rows)


def test_count_indicator_ready_counts_non_null(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "h.db")
    _candles(conn, "BTCUSDT", 3)
    assert count_indicator_ready(conn, "BTCUSDT", "4h") == 0
    write_indicators(
        conn,
        [
            {"symbol": "BTCUSDT", "timeframe": "4h", "timestamp": T0 + 2 * H4,
             "atr_14": 1.0, "rsi_14": 50.0, "ema_20": 100.0, "ema_200": 100.0},
        ],
    )
    assert count_indicator_ready(conn, "BTCUSDT", "4h") == 1


def test_health_ready_and_fresh_no_alerts(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "h2.db")
    _candles(conn, "BTCUSDT", 3)
    write_indicators(
        conn,
        [
            {"symbol": "BTCUSDT", "timeframe": "4h", "timestamp": T0 + 2 * H4,
             "atr_14": 1.0, "rsi_14": 50.0, "ema_20": 100.0, "ema_200": 100.0},
        ],
    )
    newest_close = T0 + 2 * H4 + H4
    report = ingest_health(conn, ["BTCUSDT"], "4h", now_ms=newest_close + 10 * 60_000)
    assert report["ok"] is True
    assert report["alerts"] == []
    sym = report["symbols"]["BTCUSDT"]
    assert sym == {"rows": 3, "ema200_ready": 1, "newest_age_min": 10.0, "gaps": 0}
    assert "--since 2021-01-01" in report["backfill_hint"]
    assert report["paper_only"] is True


def test_health_alerts_not_ready_stale_and_gap(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "h3.db")
    _candles(conn, "BTCUSDT", 4, skip_index=1)
    newest_close = T0 + 3 * H4 + H4
    report = ingest_health(conn, ["BTCUSDT", "ETHUSDT"], "4h", now_ms=newest_close + 90 * 60_000)
    assert report["ok"] is False
    btc = report["symbols"]["BTCUSDT"]
    assert btc["ema200_ready"] == 0
    assert btc["gaps"] == 1
    assert btc["newest_age_min"] == 90.0
    assert report["symbols"]["ETHUSDT"]["rows"] == 0
    joined = "\n".join(report["alerts"])
    assert "BTCUSDT: ema_200 not ready" in joined
    assert "BTCUSDT: newest 4h candle closed 90 min ago" in joined
    assert "BTCUSDT: 1 gap(s)" in joined
    assert "ETHUSDT: no 4h candles stored" in joined
