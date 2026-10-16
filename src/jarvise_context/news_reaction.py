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
    "geopolitics": "ภูมิรัฐศาสตร์",
    "fed": "ธนาคารกลางสหรัฐ (Fed)",
    "people": "บุคคลสำคัญและนโยบาย",
    "digest": "ภาพรวมข่าวสาร",
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


_THEME_FALLBACK_CONTEXT: dict[str, str] = {
    "chips_ai": "ตลาดสะท้อนแรงเก็งกำไรในกลุ่มเทคโนโลยีและ AI ที่ค่อนข้างทรงตัว/บวก",
    "fed": "สภาพคล่องมหภาค (Macro liquidity) ไม่ได้ถูกกดดันเพิ่มเติม",
    "geopolitics": "ยังไม่เกิดเหตุการณ์รุนแรงฉับพลันที่ทำให้เกิดแรงเทขายสินทรัพย์เสี่ยง (Risk-off)",
    "people": "ความเห็นเฉพาะบุคคล สะท้อนมุมมองเชิงบวกของผู้ถือครองสถาบัน",
    "digest": "ภาพรวมข่าวสารและพัฒนาการของตลาดในช่วงเวลาดังกล่าว",
}

_THEME_HEADLINE_MAP: dict[str, list[tuple[tuple[str, ...], str]]] = {
    "chips_ai": [
        (
            ("nvidia", "gemini"),
            "ข่าว NVIDIA มอบงบ 250 ล้านเหรียญวิจัยความปลอดภัย AI และ Google เปิดตัว Gemini 4 Argon",
        ),
        (("nvidia",), "ข่าว NVIDIA ลงทุนด้าน AI safety และโครงสร้างพื้นฐานประมวลผล"),
        (("gemini", "google"), "Google เปิดตัวโมเดลเรือธง Gemini 4 Argon"),
        (("meta",), "Meta เผยโมเดล AI รุ่นใหม่เทียบชั้น OpenAI/Anthropic"),
    ],
    "fed": [
        (("cpi", "bowman"), "เจ้าหน้าที่ Fed ส่งสัญญาณไม่รีบร้อนปรับดอกเบี้ย ตลาดรอตัวเลขเงินเฟ้อ CPI"),
        (("bowman", "rate moves"), "เจ้าหน้าที่ Fed (Bowman) ส่งสัญญาณไม่จำเป็นต้องรีบขึ้นหรือปรับดอกเบี้ย"),
        (("cpi", "dollar"), "ดอลลาร์อ่อนค่า ตลาดจับตาตัวเลขเงินเฟ้อ CPI และแถลงการณ์ Fed"),
    ],
    "geopolitics": [
        (("hormuz", "iran"), "สถานการณ์ช่องแคบฮอร์มุซและแรงกดดันต่ออิหร่าน"),
        (("china", "russia"), "สหรัฐฯ ประเมินความสัมพันธ์จีน-รัสเซียและผลกระทบต่อภูมิรัฐศาสตร์โลก"),
    ],
    "people": [
        (("saylor", "trump"), "Michael Saylor มอง Bitcoin ผ่านจุดต่ำสุดแล้ว และประเด็นเหรียญมีม $TRUMP"),
        (("saylor",), "Michael Saylor มอง Bitcoin ผ่านจุดต่ำสุดแล้วและมีโอกาสกลับขึ้นสู่ $100k"),
        (("trump",), "ประเด็นงานเลี้ยงผู้ถือเหรียญมีม $TRUMP และความเคลื่อนไหวทางการเมือง"),
    ],
}


def format_theme_executive_line(
    theme: str,
    items: list[dict[str, Any]],
    stat_note: str | None = None,
) -> str | None:
    if not items:
        return None
    label = THEME_LABELS.get(theme, theme)
    combined_text = " ".join(f"{it.get('title', '')} {it.get('comment', '')}".lower() for it in items)
    headline = ""
    for keywords, mapped_text in _THEME_HEADLINE_MAP.get(theme, []):
        if any(kw in combined_text for kw in keywords):
            headline = mapped_text
            break
    if not headline:
        raw_titles = [str(it.get("title") or "").strip() for it in items if it.get("title")]
        headline = ", ".join(raw_titles[:2]) if raw_titles else label
    context = _THEME_FALLBACK_CONTEXT.get(theme, "บริบทสนับสนุนการวิเคราะห์")
    stat_suffix = f" ({stat_note})" if stat_note else ""
    return f"{label}: {headline} → {context}{stat_suffix}"


def context_lines(
    summary: dict[str, Any],
    digest_items: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Owner-facing lines. They describe the study and do not recommend a trade."""
    lines = ["บริบทข่าว ไม่ใช่สัญญาณ"]
    combined = summary.get("combined") or {}
    if int(combined.get("event_days") or 0) == 0 and not digest_items:
        lines.append("ยังไม่มีวันเหตุการณ์ให้เทียบกับกราฟ")
        return lines

    if digest_items:
        items_by_theme: dict[str, list[dict[str, Any]]] = {}
        for it in digest_items:
            items_by_theme.setdefault(it.get("theme", "digest"), []).append(it)

        stats_by_theme: dict[str, str] = {}
        for row in summary.get("themes") or []:
            th = str(row.get("theme") or "")
            symbols = row.get("symbols") or {}
            btc = symbols.get("BTCUSDT", {}).get("horizon_1d", {})
            if btc.get("n", 0) > 0 and btc.get("median_pct") is not None:
                sign = "+" if float(btc["median_pct"]) > 0 else ""
                stats_by_theme[th] = f"BTC วันถัดไป มัธยฐาน {sign}{btc['median_pct']}%"

        for theme in ("chips_ai", "fed", "geopolitics", "people", "digest"):
            t_items = items_by_theme.get(theme)
            if not t_items:
                continue
            line = format_theme_executive_line(theme, t_items, stats_by_theme.get(theme))
            if line:
                lines.append(line)
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
