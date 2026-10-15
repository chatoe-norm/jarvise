"""Relate dated news themes to the next closed daily candles.

Pure functions only. Callers pass candles and event days in. Nothing here
fetches news, writes SQLite, or changes an approval recommendation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import median
from typing import Any

SYMBOLS = ("BTCUSDT", "ETHUSDT")
HORIZONS = (1, 5)

THEME_LABELS = {
    "chips_ai": "ชิปและ AI",
    "geopolitics": "ภูมิศาสตร์",
    "fed": "ธนาคารกลางสหรัฐ",
    "people": "บุคคลในรายการเฝ้าดู",
    "digest": "บันทึกข่าวที่มีอยู่",
}


def day_from_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).date().isoformat()


def collapse_event_days(events: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Map each calendar day to the themes that mentioned it. One day, one set."""
    by_day: dict[str, set[str]] = {}
    for event in events:
        day = str(event.get("day") or "")
        theme = str(event.get("theme") or "")
        if not day or not theme:
            continue
        by_day.setdefault(day, set()).add(theme)
    return by_day


def _series(candles: list[dict[str, Any]]) -> tuple[list[str], dict[str, float]]:
    ordered: list[str] = []
    closes: dict[str, float] = {}
    rows = sorted(candles, key=lambda row: int(row["timestamp"]))
    for row in rows:
        day = day_from_ms(int(row["timestamp"]))
        close = row.get("close")
        if close is None:
            continue
        if day not in closes:
            ordered.append(day)
        closes[day] = float(close)
    return ordered, closes


def _horizon(ordered: list[str], closes: dict[str, float], days: set[str], step: int) -> dict[str, Any]:
    returns: list[float] = []
    pending = 0
    missing_bar = 0
    index = {day: pos for pos, day in enumerate(ordered)}
    for day in sorted(days):
        pos = index.get(day)
        if pos is None:
            missing_bar += 1
            continue
        future = pos + step
        if future >= len(ordered):
            pending += 1
            continue
        base = closes[ordered[pos]]
        nxt = closes[ordered[future]]
        if base == 0:
            pending += 1
            continue
        returns.append((nxt / base - 1.0) * 100.0)
    ups = sum(1 for value in returns if value > 0)
    return {
        "n": len(returns),
        "median_pct": round(median(returns), 2) if returns else None,
        "up_ratio": round(ups / len(returns), 2) if returns else None,
        "pending": pending,
        "missing_bar": missing_bar,
    }


def symbol_reaction(candles: list[dict[str, Any]], days: set[str]) -> dict[str, Any]:
    ordered, closes = _series(candles)
    return {f"horizon_{step}d": _horizon(ordered, closes, days, step) for step in HORIZONS}


def summarize_reactions(
    events: list[dict[str, Any]],
    candles_by_symbol: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """One observation per calendar day. A day in two themes is not counted twice."""
    by_day = collapse_event_days(events)
    themes = sorted({theme for themes_on_day in by_day.values() for theme in themes_on_day})
    theme_rows = []
    for theme in themes:
        days = {day for day, names in by_day.items() if theme in names}
        theme_rows.append(_row(theme, days, candles_by_symbol))
    return {
        "not_a_trade_signal": True,
        "event_days": len(by_day),
        "themes": theme_rows,
        "combined": _row("combined", set(by_day), candles_by_symbol),
    }


def _row(theme: str, days: set[str], candles_by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    label = "รวมทุกธีม" if theme == "combined" else THEME_LABELS.get(theme, theme)
    symbols = {symbol: symbol_reaction(candles_by_symbol.get(symbol) or [], days) for symbol in SYMBOLS}
    return {"theme": theme, "label": label, "event_days": len(days), "symbols": symbols}


def context_lines(summary: dict[str, Any]) -> list[str]:
    """Owner-facing lines. They describe the study and do not recommend a trade."""
    lines = ["บริบทข่าว ไม่ใช่สัญญาณ"]
    combined = summary.get("combined") or {}
    if int(combined.get("event_days") or 0) == 0:
        lines.append("ยังไม่มีวันเหตุการณ์ให้เทียบกับกราฟ")
        return lines
    lines.append(_metric_line(str(combined.get("label") or "รวมทุกธีม"), combined))
    for row in summary.get("themes") or []:
        if int(row.get("event_days") or 0) == 0:
            continue
        lines.append(_metric_line(str(row.get("label") or row.get("theme")), row))
    return lines


def _metric_line(label: str, row: dict[str, Any]) -> str:
    parts = [label]
    symbols = row.get("symbols") or {}
    for symbol, short in (("BTCUSDT", "BTC"), ("ETHUSDT", "ETH")):
        horizons = symbols.get(symbol) or {}
        one = horizons.get("horizon_1d") or {}
        five = horizons.get("horizon_5d") or {}
        parts.append(f"{short} วันถัดไป {_pct(one)} · 5 วัน {_pct(five)}")
    return " — ".join(parts)


def _pct(horizon: dict[str, Any]) -> str:
    n = int(horizon.get("n") or 0)
    pending = int(horizon.get("pending") or 0)
    if n == 0 and pending:
        return f"ยังวัดไม่ได้ {pending} วัน"
    if n == 0:
        return "ยังวัดไม่ได้"
    median_pct = float(horizon["median_pct"])
    up = horizon.get("up_ratio")
    up_text = "" if up is None else f" ขึ้น {round(float(up) * 100):.0f}%"
    sign = "+" if median_pct > 0 else ""
    return f"มัธยฐาน {sign}{median_pct}% จาก {n} วัน{up_text}"
