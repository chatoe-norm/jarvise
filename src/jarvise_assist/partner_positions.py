"""Read-only open-positions card for bound partners (on request only).

Decimal only. Calls only position_risk and symbol_filters — never open-order
or exchange-order endpoints. Shared cache: success 30 s, failure 10 s.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
_OK_TTL = 30_000
_ERR_TTL = 10_000
_MAX_ROWS = 10
_ENTRY_MATCH = Decimal("0.0005")
_PCT_Q = Decimal("0.01")
_ERR_TEXT = "อ่านไม้ไม่ได้ตอนนี้ ลองใหม่อีกครั้ง"
_EMPTY_TEXT = "ตอนนี้ไม่มีไม้ที่เปิดอยู่"
_FOOTER = "ดูอย่างเดียว · % คือราคาที่ขยับจากราคาเข้า ไม่ใช่กำไรของพอร์ต"

# (text, cached_at, ok)
_cache: tuple[str, int, bool] | None = None


def reset_positions_cache() -> None:
    """Test hook: clear the shared positions render cache."""
    global _cache
    _cache = None


def _stamp(when: int) -> str:
    dt = datetime.fromtimestamp(when / 1000, tz=BANGKOK)
    return dt.strftime("%d/%m %H:%M")


def _tick_decimals(tick: Decimal) -> int:
    t = tick.normalize()
    exp = t.as_tuple().exponent
    if not isinstance(exp, int):
        return 0
    return max(0, -exp)


def _fmt_price(client: Any, symbol: str, value: Decimal) -> str:
    try:
        tick = client.symbol_filters(symbol).tick_size
        if tick <= 0:
            raise ValueError("bad tick")
        n = (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        rounded = n * tick
        decimals = _tick_decimals(tick)
        quant = Decimal("1").scaleb(-decimals) if decimals > 0 else Decimal("1")
        shown = rounded.quantize(quant, rounding=ROUND_HALF_UP)
        return format(shown, "f")
    except Exception:  # noqa: BLE001
        text = format(value.normalize(), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"


def _pct_line(entry: Decimal, mark: Decimal, *, long: bool) -> str:
    if entry <= 0 or mark <= 0:
        return "• ยังไม่ปิด —"
    if long:
        raw = (mark - entry) / entry * Decimal("100")
    else:
        raw = (entry - mark) / entry * Decimal("100")
    q = raw.quantize(_PCT_Q, rounding=ROUND_HALF_UP)
    if q == 0:
        return "• ยังไม่ปิด ≈ +0.00% จากราคาเข้า"
    body = format(q, "f")
    if q > 0:
        body = f"+{body}"
    return f"• ยังไม่ปิด ≈ {body}% จากราคาเข้า"


def _open_line(conn: sqlite3.Connection, pos: Any) -> str | None:
    try:
        row = conn.execute(
            """
            SELECT side, entry, entry_time_ms FROM assist_watch_position
            WHERE symbol=? AND position_side=? AND closed_at_ms IS NULL
            """,
            (str(pos.symbol).upper(), str(pos.position_side).upper()),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    side = str(row["side"] or "")
    if side != pos.side:
        return None
    raw_t = row["entry_time_ms"]
    if raw_t is None:
        return None
    try:
        row_entry = Decimal(str(row["entry"]))
    except Exception:  # noqa: BLE001
        return None
    if row_entry <= 0:
        return None
    if abs(pos.entry_price - row_entry) / row_entry > _ENTRY_MATCH:
        return None
    return f"• เปิด ~{_stamp(int(raw_t))}"


def _side_label(pos: Any) -> str:
    return "LONG" if pos.side == "long" else "SHORT"


def _render_body(conn: sqlite3.Connection, client: Any, positions: list[Any], *, now_ms: int) -> str:
    ordered = sorted(positions, key=lambda p: (str(p.symbol).upper(), str(p.position_side).upper()))
    shown = ordered[:_MAX_ROWS]
    extra = len(ordered) - len(shown)
    lines = [f"📊 ไม้ที่เปิดอยู่ · {_stamp(int(now_ms))}"]
    for i, pos in enumerate(shown, start=1):
        sym = str(pos.symbol).upper()
        entry = pos.entry_price
        mark = pos.mark_price
        long = pos.side == "long"
        lines.append(f"{i}. {sym} · {_side_label(pos)}")
        lines.append(f"• เข้า {_fmt_price(client, sym, entry)} · ตอนนี้ {_fmt_price(client, sym, mark)}")
        lines.append(_pct_line(entry, mark, long=long))
        open_line = _open_line(conn, pos)
        if open_line is not None:
            lines.append(open_line)
    if extra > 0:
        lines.append(f"+{extra} ไม้")
    lines.append(_FOOTER)
    return "\n".join(lines)


def render_partner_positions(conn: sqlite3.Connection, client: Any, *, now_ms: int) -> str:
    """Render the partner open-positions card (shared cache across partners)."""
    global _cache
    t = int(now_ms)
    if _cache is not None:
        text, at, ok = _cache
        ttl = _OK_TTL if ok else _ERR_TTL
        if t - at < ttl:
            return text

    if client is None:
        text = _ERR_TEXT
        _cache = (text, t, False)
        return text

    try:
        positions = client.position_risk()
    except Exception:  # noqa: BLE001
        text = _ERR_TEXT
        _cache = (text, t, False)
        return text

    if not positions:
        text = _EMPTY_TEXT
        _cache = (text, t, True)
        return text

    text = _render_body(conn, client, list(positions), now_ms=t)
    _cache = (text, t, True)
    return text
