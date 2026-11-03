"""Monday assist weekly summary (virtual track + futures income)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.futures import BinanceFuturesClient, build_futures_client
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
WEEK_MS = 7 * 24 * 3_600_000
ANALYSIS_EXIT_REASONS = frozenset({"flat", "opposite"})


def format_weekly_summary(
    *,
    counts: dict[str, int],
    virtual: dict[str, Any],
    real: dict[str, Any],
    closed_all: int,
) -> str:
    lines = [
        "📊 ผล 7 วันที่ผ่านมา (assist)",
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
        lines.append(
            f"ปิดที่ stop เดิม {int(virtual.get('exit_stop_original') or 0)} · "
            f"stop ที่เลื่อนแล้ว {int(virtual.get('exit_stop_trailed') or 0)} · "
            f"ถึง 2R {int(virtual.get('exit_target_2r') or 0)} · "
            f"ปิดตามสัญญาณ {int(virtual.get('exit_analysis') or 0)}"
        )
    realized = float(real.get("realized_pnl") or 0.0)
    funding = float(real.get("funding") or 0.0)
    commission = float(real.get("commission") or 0.0)
    net = float(real.get("universe_usdt") or (realized + funding + commission))
    lines.append(f"กำไรปิด (REALIZED_PNL) {_signed(realized)} USDT")
    lines.append(f"funding {_signed(funding)} USDT")
    lines.append(f"ค่าธรรมเนียม (COMMISSION) {_signed(commission)} USDT")
    lines.append(f"สุทธิ {_signed(net)} USDT")
    other = float(real.get("other_usdt") or 0.0)
    if other != 0.0:
        lines.append(f"เหรียญนอกรายการ {_signed(other)} USDT")
    if closed_all < EDGE_SAMPLE_MIN:
        lines.append(f"ยังไม่พิสูจน์ edge — ตัวอย่าง {closed_all} ไม้")
    return "\n".join(lines)


def _signed(value: float) -> str:
    if value > 0:
        return f"+{value:.2f}"
    return f"{value:.2f}"


def summarize_virtual(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "win_rate": 0.0,
            "total_r": 0.0,
            "mean_r": 0.0,
            "total_usdt": 0.0,
            "exit_stop_original": 0,
            "exit_stop_trailed": 0,
            "exit_target_2r": 0,
            "exit_analysis": 0,
        }
    nets = [float(r.get("net_r") or 0.0) for r in rows]
    pnls = [float(r.get("pnl_usdt") or 0.0) for r in rows]
    wins = sum(1 for x in nets if x > 0)
    total_r = sum(nets)
    exit_stop_original = 0
    exit_stop_trailed = 0
    exit_target = 0
    exit_analysis = 0
    for row in rows:
        reason = str(row.get("exit_reason") or "").lower()
        if reason == "stop":
            gross = float(row.get("gross_r") or 0.0)
            if gross > 0:
                exit_stop_trailed += 1
            else:
                exit_stop_original += 1
        elif reason == "target_2r":
            exit_target += 1
        elif reason in ANALYSIS_EXIT_REASONS:
            exit_analysis += 1
    return {
        "n": len(rows),
        "win_rate": wins / len(rows),
        "total_r": total_r,
        "mean_r": total_r / len(rows),
        "total_usdt": sum(pnls),
        "exit_stop_original": exit_stop_original,
        "exit_stop_trailed": exit_stop_trailed,
        "exit_target_2r": exit_target,
        "exit_analysis": exit_analysis,
    }


def summarize_income(rows: list[dict[str, Any]]) -> dict[str, Any]:
    realized = Decimal("0")
    funding = Decimal("0")
    commission = Decimal("0")
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
        if sym not in ASSIST_SYMBOLS:
            other += amount
            continue
        if itype == "REALIZED_PNL":
            realized += amount
        elif itype == "FUNDING_FEE":
            funding += amount
        elif itype == "COMMISSION":
            commission += amount
    uni = realized + funding + commission
    return {
        "realized_pnl": float(realized),
        "funding": float(funding),
        "commission": float(commission),
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
    # The job runs Monday 08:30. "Since this Monday 00:00" would cover 8.5 hours,
    # so report the full seven days before the run.
    week_start = ts - WEEK_MS
    counts = count_signals_since(conn, since_ms=week_start)
    # Count "sent" as rows that left as sent (created this week with status sent/taken/skipped).
    # Use status counts; also add taken/skipped that were created as sent.
    virtual_rows = list_closed_tracks_since(conn, since_ms=week_start)
    virtual = summarize_virtual(virtual_rows)
    closed_all = count_closed_tracks_all(conn)

    client = futures if futures is not None else build_futures_client()
    real: dict[str, Any] = {
        "realized_pnl": 0.0,
        "funding": 0.0,
        "commission": 0.0,
        "universe_usdt": 0.0,
        "other_usdt": 0.0,
        "realized_n": 0,
    }
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
