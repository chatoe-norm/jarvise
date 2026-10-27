"""Monday assist weekly summary (virtual track + futures income)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.futures import BinanceFuturesClient, build_futures_client
from jarvise_assist.pause import monday_bangkok_ms
from jarvise_assist.store import (
    count_closed_tracks_all,
    count_signals_since,
    list_closed_tracks_since,
    now_ms,
)
from jarvise_assist.track import resolve_open_tracks
from jarvise_notify.telegram import send_telegram_message

SendFn = Callable[..., Any]
INCOME_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})
EDGE_SAMPLE_MIN = 30


def format_weekly_summary(
    *,
    counts: dict[str, int],
    virtual: dict[str, Any],
    real: dict[str, Any],
    closed_all: int,
) -> str:
    lines = [
        "📊 ผลสัปดาห์นี้ (assist)",
        (
            f"สัญญาณ: ส่ง {counts.get('sent', 0)} · "
            f"ข้าม {counts.get('skipped', 0)} · "
            f"เข้าแล้ว {counts.get('taken', 0)}"
        ),
    ]
    n = int(virtual.get("n") or 0)
    if n == 0:
        lines.append("ไม้เสมือน: ยังไม่มีที่ปิด")
    else:
        wr = float(virtual.get("win_rate") or 0.0) * 100.0
        total_r = float(virtual.get("total_r") or 0.0)
        mean_r = float(virtual.get("mean_r") or 0.0)
        total_usdt = float(virtual.get("total_usdt") or 0.0)
        lines.append(
            f"ไม้เสมือน: n={n} · ชนะ {wr:.0f}% · "
            f"รวม {_signed(total_r)}R · เฉลี่ย {_signed(mean_r)}R · "
            f"{_signed(total_usdt)} USDT"
        )
    uni = float(real.get("universe_usdt") or 0.0)
    other = float(real.get("other_usdt") or 0.0)
    realized_n = int(real.get("realized_n") or 0)
    lines.append(
        f"จริง (futures): จักรวาล {_signed(uni)} USDT · อื่น {_signed(other)} USDT · "
        f"REALIZED_PNL≠0 {realized_n} แถว"
    )
    if closed_all < EDGE_SAMPLE_MIN:
        lines.append(f"ยังไม่พิสูจน์ edge — ตัวอย่าง {closed_all} ไม้")
    return "\n".join(lines)


def _signed(value: float) -> str:
    if value > 0:
        return f"+{value:.2f}"
    return f"{value:.2f}"


def summarize_virtual(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "win_rate": 0.0, "total_r": 0.0, "mean_r": 0.0, "total_usdt": 0.0}
    nets = [float(r.get("net_r") or 0.0) for r in rows]
    pnls = [float(r.get("pnl_usdt") or 0.0) for r in rows]
    wins = sum(1 for x in nets if x > 0)
    total_r = sum(nets)
    return {
        "n": len(rows),
        "win_rate": wins / len(rows),
        "total_r": total_r,
        "mean_r": total_r / len(rows),
        "total_usdt": sum(pnls),
    }


def summarize_income(rows: list[dict[str, Any]]) -> dict[str, Any]:
    uni = Decimal("0")
    other = Decimal("0")
    realized_n = 0
    for row in rows:
        itype = str(row.get("incomeType") or "")
        if itype not in INCOME_TYPES:
            continue
        try:
            amount = Decimal(str(row.get("income") or "0"))
        except Exception:  # noqa: BLE001
            continue
        sym = str(row.get("symbol") or "").upper()
        if itype == "REALIZED_PNL" and amount != 0:
            realized_n += 1
        if sym in ASSIST_SYMBOLS:
            uni += amount
        else:
            other += amount
    return {
        "universe_usdt": float(uni),
        "other_usdt": float(other),
        "realized_n": realized_n,
    }


def run_assist_weekly(
    conn: Any,
    *,
    futures: BinanceFuturesClient | None = None,
    now_ms_value: int | None = None,
    send: SendFn | None = None,
) -> dict[str, Any]:
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    send_fn = send or send_telegram_message
    resolve_open_tracks(conn, now_ms=ts)
    week_start = monday_bangkok_ms(ts)
    counts = count_signals_since(conn, since_ms=week_start)
    # Count "sent" as rows that left as sent (created this week with status sent/taken/skipped).
    # Use status counts; also add taken/skipped that were created as sent.
    virtual_rows = list_closed_tracks_since(conn, since_ms=week_start)
    virtual = summarize_virtual(virtual_rows)
    closed_all = count_closed_tracks_all(conn)

    client = futures if futures is not None else build_futures_client()
    real: dict[str, Any] = {"universe_usdt": 0.0, "other_usdt": 0.0, "realized_n": 0}
    if client is not None:
        try:
            income = client.income_history(start_ms=week_start, end_ms=ts, limit=1000)
            real = summarize_income(income)
        except Exception as exc:  # noqa: BLE001
            real["error"] = type(exc).__name__

    text = format_weekly_summary(counts=counts, virtual=virtual, real=real, closed_all=closed_all)
    sent_ok = bool(send_fn(text))
    return {
        "ok": True,
        "paper_only": True,
        "at_ms": ts,
        "text": text,
        "telegram": sent_ok,
        "counts": counts,
        "virtual": virtual,
        "real": real,
        "closed_all": closed_all,
    }
