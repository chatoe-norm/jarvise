"""Quick same-side open cap (SQLite only)."""

from __future__ import annotations

from typing import Any


def _norm_side(side: str) -> str:
    s = str(side).lower()
    if s not in {"long", "short"}:
        raise ValueError(f"invalid side: {side!r}")
    return s


def count_open_same_side(conn: Any, side: str) -> int:
    side_l = _norm_side(side)
    trades = conn.execute(
        """
        SELECT COUNT(*) FROM assist_quick_trade
        WHERE status='open' AND lower(side)=?
        """,
        (side_l,),
    ).fetchone()[0]
    regs = conn.execute(
        """
        SELECT COUNT(*) FROM assist_quick_registration
        WHERE status='open' AND side=?
        """,
        (side_l,),
    ).fetchone()[0]
    return int(trades) + int(regs)


def same_side_cap_reached(count: int, cap: int = 2) -> bool:
    return count >= cap


def same_side_cap_text(side: str, count: int) -> str:
    side_l = _norm_side(side)
    label = "LONG" if side_l == "long" else "SHORT"
    return f"ไม่ลงทะเบียน — ฝั่ง {label} เปิดอยู่แล้ว {count} ไม้ (เพดานฝั่งละ 2) · ปิดหรือให้ทะเบียนหมดอายุก่อน"
