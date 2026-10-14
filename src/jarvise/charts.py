"""SVG snapshots of closed candles already stored in SQLite.

These files are a picture for review. Analyzer and paper keep reading
``market_technicals``. This module does not fetch prices.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from jarvise_ingest.db import load_recent_ohlcv, open_db

CHART_BARS = 120
_UP = "#3ecf8e"
_DOWN = "#e35d6a"
_MUTED = "#8b9aab"
_W = 640
_H = 160
_PAD_TOP = 10
_PAD_RIGHT = 52
_PAD_BOTTOM = 22
_PAD_LEFT = 8


def analytics_db_path() -> Path:
    """Same database the ingest job writes. Override with ``JARVISE_DB``."""
    raw = (os.environ.get("JARVISE_DB") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "data" / "analytics" / "jarvise.db"


def candle_graph_dir(db_path: Path) -> Path:
    """Picture folder beside the database file."""
    return db_path.parent / "candle-graph"


def render_candle_svg(bars: list[dict[str, Any]], *, label: str) -> str:
    """Draw wick and body for closed bars. No stop line."""
    if not bars:
        return ""
    highs = [float(bar["high"]) for bar in bars]
    lows = [float(bar["low"]) for bar in bars]
    lo = min(lows)
    hi = max(highs)
    span = hi - lo or 1.0
    pad = span * 0.04
    lo -= pad
    hi += pad
    inner_w = _W - _PAD_LEFT - _PAD_RIGHT
    inner_h = _H - _PAD_TOP - _PAD_BOTTOM
    slot = inner_w / len(bars)

    def y(price: float) -> float:
        return _PAD_TOP + ((hi - price) / (hi - lo)) * inner_h

    parts: list[str] = []
    for index, bar in enumerate(bars):
        open_px = float(bar["open"])
        close_px = float(bar["close"])
        cx = _PAD_LEFT + slot * (index + 0.5)
        color = _UP if close_px >= open_px else _DOWN
        body_top = y(max(open_px, close_px))
        body_bot = y(min(open_px, close_px))
        width = max(2.0, min(slot * 0.65, 8.0))
        height = max(1.0, body_bot - body_top)
        parts.append(
            "<g>"
            f'<line x1="{cx:.2f}" x2="{cx:.2f}" y1="{y(float(bar["high"])):.2f}" '
            f'y2="{y(float(bar["low"])):.2f}" stroke="{color}" stroke-width="1"/>'
            f'<rect x="{cx - width / 2:.2f}" y="{body_top:.2f}" width="{width:.2f}" '
            f'height="{height:.2f}" fill="{color}"/>'
            "</g>"
        )
    last = float(bars[-1]["close"])
    safe = escape(label)
    title = escape(f"{label} · close {last:.2f} · {len(bars)} bars")
    body = "\n".join(parts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_W} {_H}" role="img" '
        f'aria-label="Closed candles {safe}">\n'
        f"<title>{title}</title>\n"
        f"{body}\n"
        f'<text x="{_W - _PAD_RIGHT + 4}" y="{y(hi) + 4:.2f}" fill="{_MUTED}" '
        f'font-size="10">{hi:.0f}</text>\n'
        f'<text x="{_W - _PAD_RIGHT + 4}" y="{y(lo):.2f}" fill="{_MUTED}" '
        f'font-size="10">{lo:.0f}</text>\n'
        "</svg>\n"
    )


def export_candle_graphs(
    db_path: Path,
    symbols: list[str],
    timeframes: list[str],
    *,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Write one SVG per symbol and timeframe that has stored bars.

    A missing database or an empty series writes nothing. Callers keep their
    own exit code if this fails.
    """
    root = out_dir or candle_graph_dir(db_path)
    written: list[str] = []
    skipped: list[str] = []
    if not db_path.is_file():
        return {"ok": False, "written": written, "skipped": skipped, "dir": str(root), "error": "missing_db"}
    conn = open_db(db_path)
    try:
        for raw_symbol in symbols:
            symbol = raw_symbol.strip().upper()
            if not symbol:
                continue
            for timeframe in timeframes:
                bars = load_recent_ohlcv(conn, symbol, timeframe, limit=CHART_BARS)
                key = f"{symbol}/{timeframe}"
                if not bars:
                    skipped.append(key)
                    continue
                path = root / symbol / f"{timeframe}.svg"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(render_candle_svg(bars, label=f"{symbol} {timeframe}"), encoding="utf-8")
                written.append(str(path))
    finally:
        conn.close()
    return {"ok": True, "written": written, "skipped": skipped, "dir": str(root)}
