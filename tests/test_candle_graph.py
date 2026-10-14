from __future__ import annotations

from pathlib import Path

from jarvise.charts import export_candle_graphs, render_candle_svg
from jarvise_ingest.db import open_db, upsert_market_technicals


def _bars(symbol: str, timeframe: str, n: int, *, down: bool = False) -> list[dict]:
    rows = []
    for i in range(n):
        base = 100.0 + i
        close = base - 1 if down else base + 1
        rows.append(
            {
                "symbol": symbol,
                "timestamp": 1_700_000_000_000 + i * 14_400_000,
                "timeframe": timeframe,
                "open": base,
                "high": base + 2,
                "low": base - 2,
                "close": close,
                "volume": 1.0,
            }
        )
    return rows


def test_export_writes_svg_for_stored_bars_only(tmp_path: Path) -> None:
    db = tmp_path / "jarvise.db"
    conn = open_db(db)
    upsert_market_technicals(conn, _bars("BTCUSDT", "4h", 3))
    conn.commit()
    conn.close()

    out = tmp_path / "candle-graph"
    result = export_candle_graphs(db, ["btcusdt", "ETHUSDT"], ["4h", "1d"], out_dir=out)
    assert result["ok"] is True
    written = Path(result["written"][0])
    assert written == out / "BTCUSDT" / "4h.svg"
    text = written.read_text(encoding="utf-8")
    assert text.count("<rect ") == 3
    assert text.count("<line ") == 3
    assert "#3ecf8e" in text
    assert "ETHUSDT/4h" in result["skipped"]
    assert "BTCUSDT/1d" in result["skipped"]
    assert not (out / "ETHUSDT").exists()


def test_export_missing_db_writes_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    out = tmp_path / "candle-graph"
    result = export_candle_graphs(missing, ["BTCUSDT"], ["4h"], out_dir=out)
    assert result["ok"] is False
    assert result["error"] == "missing_db"
    assert result["written"] == []
    assert not out.exists()


def test_down_candle_uses_danger_color() -> None:
    svg = render_candle_svg(
        [{"open": 10, "high": 11, "low": 8, "close": 9}],
        label="BTCUSDT 4h",
    )
    assert "#e35d6a" in svg
    assert "stop" not in svg.lower()
