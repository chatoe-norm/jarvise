"""Quick 23:30 cutoff alerts (alert-only) and entry block helpers. No orders."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from jarvise_assist.quick_errors import e_cutoff_block

logger = logging.getLogger(__name__)

_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
CUTOFF_HHMM = (0, 0)
ALERT_LEAD_MIN = 30


def _local_dt(now_ms: int) -> datetime:
    return datetime.fromtimestamp(int(now_ms) / 1000, tz=_BANGKOK)


def in_entry_block(now_ms: int) -> bool:
    """True during 23:30:00–23:59:59.999… Asia/Bangkok (same window as the alert)."""
    dt = _local_dt(now_ms)
    if dt.hour == 23 and dt.minute >= 30:
        return True
    return False


def _minutes_to_midnight_ceil(now_ms: int) -> int:
    dt = _local_dt(now_ms)
    next_midnight = datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=_BANGKOK) + timedelta(days=1)
    remain_ms = int(next_midnight.timestamp() * 1000) - int(now_ms)
    if remain_ms <= 0:
        return 0
    return max(1, math.ceil(remain_ms / 60_000))


def cutoff_block_text(now_ms: int) -> str:
    return e_cutoff_block(minutes=_minutes_to_midnight_ceil(now_ms))


def _format_hold(entry_time_ms: int | None, now_ms: int) -> str:
    if entry_time_ms is None:
        elapsed = 0
    else:
        elapsed = max(0, int(now_ms) - int(entry_time_ms))
    total_min = elapsed // 60_000
    hours = total_min // 60
    minutes = total_min % 60
    return f"{hours}:{minutes:02d}"


def _side_label(side: str) -> str:
    return "LONG" if str(side).lower() == "long" else "SHORT"


def _bangkok_day(now_ms: int) -> str:
    return _local_dt(now_ms).strftime("%Y-%m-%d")


def maybe_send_cutoff_alerts(
    conn: Any,
    *,
    now_ms: int,
    send: Any,
    sync_ok: bool,
) -> dict[str, int]:
    if not in_entry_block(now_ms):
        return {}
    day = _bangkok_day(now_ms)
    counts: dict[str, int] = {
        "trades_sent": 0,
        "trades_failed": 0,
        "regs_sent": 0,
        "regs_failed": 0,
    }

    trade_rows = conn.execute(
        "SELECT id, symbol, side, entry_time_ms FROM assist_quick_trade WHERE status='open'"
    ).fetchall()
    for row in trade_rows:
        tid = str(row["id"])
        sym = str(row["symbol"]).upper()
        side = _side_label(str(row["side"]))
        hold = _format_hold(row["entry_time_ms"], now_ms)
        text = (
            f"⏰ ใกล้ cutoff 00:00 — ไม้ quick {sym} {side} ยังเปิดอยู่ (ถือมา {hold}) "
            f"· บอทไม่ปิดให้ — ถ้าไม่อยากถือข้ามวัน ปิดเองใน Binance"
        )
        cur = conn.execute(
            """
            UPDATE assist_quick_trade
            SET cutoff_notice_day=?
            WHERE id=? AND (cutoff_notice_day IS NULL OR cutoff_notice_day != ?)
            """,
            (day, tid, day),
        )
        conn.commit()
        if cur.rowcount != 1:
            continue
        if _safe_send(send, text):
            counts["trades_sent"] += 1
        else:
            counts["trades_failed"] += 1

    reg_sql = "SELECT id, symbol, side, expires_at_ms FROM assist_quick_registration WHERE status='open'"
    for row in conn.execute(reg_sql).fetchall():
        if sync_ok and int(row["expires_at_ms"] or 0) <= int(now_ms):
            continue
        rid = str(row["id"])
        sym = str(row["symbol"]).upper()
        side = _side_label(str(row["side"]))
        if sync_ok:
            text = f"⏰ ใกล้ cutoff 00:00 — ลงทะเบียน quick {sym} {side} ยังไม่เข้า · ไม่ควรเปิดหลังเที่ยงคืน"
        else:
            text = (
                f"⏰ ใกล้ cutoff 00:00 — ลงทะเบียน quick {sym} {side} "
                f"ยืนยันสถานะไม่ได้ (อ่าน Binance ไม่ได้) · เช็กใน Binance ว่ามีไม้เปิดอยู่ไหม"
            )
        cur = conn.execute(
            """
            UPDATE assist_quick_registration
            SET cutoff_notice_day=?
            WHERE id=? AND (cutoff_notice_day IS NULL OR cutoff_notice_day != ?)
            """,
            (day, rid, day),
        )
        conn.commit()
        if cur.rowcount != 1:
            continue
        if _safe_send(send, text):
            counts["regs_sent"] += 1
        else:
            counts["regs_failed"] += 1

    return {k: v for k, v in counts.items() if v}


def _safe_send(send: Any, text: str, **kwargs: Any) -> bool:
    try:
        result = send(text, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("quick cutoff send failed: %s", type(exc).__name__)
        return False
    if result is False or result is None:
        return False
    return True
