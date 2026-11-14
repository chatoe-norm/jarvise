"""Monday assist weekly summary (virtual track + futures income)."""

from __future__ import annotations

import statistics
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.futures import BinanceFuturesClient, build_futures_client
from jarvise_assist.journal import format_journal_lines, summarize_journal
from jarvise_assist.quick import summarize_quick
from jarvise_assist.quick_tags import quick_income_predicate
from jarvise_assist.real_trades import (
    SPEC_START_MS,
    format_bkk_window,
    format_real_lines,
    run_real_report,
    summarize_real_vs_virtual,
)
from jarvise_assist.store import (
    count_closed_tracks_all,
    count_signals_since,
    list_closed_tracks_since,
    now_ms,
)
from jarvise_assist.track import TRACK_COHORT, resolve_open_tracks, virtual_risk_unusable
from jarvise_notify.telegram import send_telegram_message

SendFn = Callable[..., Any]
INCOME_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})
EDGE_SAMPLE_MIN = 30
WEEK_MS = 7 * 24 * 3_600_000
ANALYSIS_EXIT_REASONS = frozenset({"flat", "opposite"})


_BLOCKED_REASON_ORDER = ("risk_cap", "margin_cap", "streak_pause")


def format_weekly_summary(
    *,
    counts: dict[str, int],
    virtual: dict[str, Any],
    real: dict[str, Any],
    closed_all: int,
    held: dict[str, Any] | None = None,
    blocked: dict[str, dict[str, Any]] | None = None,
    real_vs_virtual: dict[str, Any] | None = None,
    real_report_error: str | None = None,
    journal: dict[str, Any] | None = None,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
    quick: dict[str, Any] | None = None,
) -> str:
    window = None
    if window_start_ms is not None and window_end_ms is not None:
        window = format_bkk_window(int(window_start_ms), int(window_end_ms))
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
        if window is None:
            lines.append("ไม้เสมือน: ยังไม่มีที่ปิด")
        else:
            lines.append(f"ไม้เสมือน {window}: n=0 · ยังไม่มีที่ปิด")
    else:
        wr = float(virtual.get("win_rate") or 0.0) * 100.0
        total_r = float(virtual.get("total_r") or 0.0)
        mean_r = float(virtual.get("mean_r") or 0.0)
        total_usdt = float(virtual.get("total_usdt") or 0.0)
        if window is None:
            lines.append(
                f"ไม้เสมือน: n={n} · ชนะ {wr:.0f}% · รวม {_signed(total_r)}R · {_signed(total_usdt)} USDT"
            )
        else:
            lines.append(
                f"ไม้เสมือน {window}: n={n} · ชนะ {wr:.0f}% · "
                f"รวม {_signed(total_r)}R · {_signed(total_usdt)} USDT"
            )
        lines.append(
            f"Expectancy {_signed(mean_r)}R/ไม้ · มัธยฐาน {_signed(float(virtual.get('median_r') or 0.0))}R · "
            f"ชนะเฉลี่ย {_r_or_dash(virtual.get('avg_win_r'))} · "
            f"แพ้เฉลี่ย {_r_or_dash(virtual.get('avg_loss_r'))}"
        )
        lines.append(
            f"ปิดที่ stop เดิม {int(virtual.get('exit_stop_original') or 0)} · "
            f"stop ที่ทุน {int(virtual.get('exit_stop_be') or 0)} · "
            f"stop ที่ trail {int(virtual.get('exit_stop_trail') or 0)} · "
            f"ถึง 2R {int(virtual.get('exit_target_2r') or 0)} · "
            f"ปิดตามสัญญาณ {int(virtual.get('exit_analysis') or 0)}"
        )
        funding_line = f"funding เสมือน {_signed(float(virtual.get('funding_usdt') or 0.0))} USDT"
        missing = int(virtual.get("funding_missing") or 0)
        if missing:
            funding_line += f" (ไม่มีอัตรา {missing} รอบ นับเป็น 0)"
        lines.append(funding_line)
    dropped = virtual.get("dropped_risk_n")
    if dropped is not None:
        drop_n = int(dropped)
        if window is None:
            lines.append(f"ไม้เสมือนที่ความเสี่ยง ≤ 0: n={drop_n} · ไม่นับในค่าเฉลี่ย R")
        else:
            lines.append(f"ไม้เสมือนที่ความเสี่ยง ≤ 0 {window}: n={drop_n} · ไม่นับในค่าเฉลี่ย R")
    if held is not None:
        held_n = int(held.get("n") or 0)
        if held_n == 0:
            lines.append("สัญญาณที่ถูก hold: 0 · Expectancy —")
        else:
            lines.append(f"สัญญาณที่ถูก hold: {held_n} · Expectancy {_signed(float(held.get('mean_r') or 0.0))}R")
    blocked_line = _format_blocked_line(blocked)
    if blocked_line:
        lines.append(blocked_line)
    if real_report_error:
        lines.append(f"รายงานไม้จริง: ดึงข้อมูลไม่ได้ ({real_report_error})")
    elif real_vs_virtual is not None:
        lines.extend(format_real_lines(real_vs_virtual))
    if journal is not None:
        lines.extend(format_journal_lines(journal))
    realized = float(real.get("realized_pnl") or 0.0)
    funding = float(real.get("funding") or 0.0)
    commission = float(real.get("commission") or 0.0)
    net = float(real.get("universe_usdt") or (realized + funding + commission))
    lines.append(f"กำไรปิด (REALIZED_PNL) {_signed(realized)} USDT")
    lines.append(f"funding {_signed(funding)} USDT")
    lines.append(f"ค่าธรรมเนียม (COMMISSION) {_signed(commission)} USDT")
    lines.append(f"สุทธิ {_signed(net)} USDT")
    quick_n = int((quick or {}).get("n") or 0) if quick else 0
    quick_usdt = real.get("quick_usdt")
    if quick is not None and (quick_n > 0 or quick_usdt is not None):
        sum_r = float(quick.get("sum_r") or 0.0)
        # Prefer income actually removed from main lines; fall back to summarize_quick usdt.
        if quick_usdt is not None:
            usdt = float(quick_usdt)
        else:
            usdt = float(quick.get("usdt") or 0.0)
        lines.append(f"Quick (แยกจาก assist) 7 วัน: n={quick_n} · รวม {sum_r:+.2f}R · {usdt:+.2f} USDT")
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


def _r_or_dash(value: Any) -> str:
    if value is None:
        return "—"
    return f"{_signed(float(value))}R"


def _format_blocked_line(blocked: dict[str, dict[str, Any]] | None) -> str | None:
    """One counterfactual line for closed kind=blocked rows, or None when empty."""
    if not blocked:
        return None
    parts: list[str] = []
    for reason in _BLOCKED_REASON_ORDER:
        stats = blocked.get(reason)
        if stats is None:
            continue
        n = int(stats.get("n") or 0)
        if n <= 0:
            continue
        part = f"{reason} n={n} · Expectancy {_signed(float(stats.get('mean_r') or 0.0))}R"
        missing = int(stats.get("funding_missing") or 0)
        if missing > 0:
            part += f" (ไม่มีอัตรา {missing} รอบ)"
        parts.append(part)
    if not parts:
        return None
    return "ถ้าไม่บล็อก: " + " · ".join(parts)


def _empty_virtual(*, dropped_risk_n: int = 0) -> dict[str, Any]:
    return {
        "n": 0,
        "win_rate": 0.0,
        "total_r": 0.0,
        "mean_r": 0.0,
        "median_r": 0.0,
        "avg_win_r": None,
        "avg_loss_r": None,
        "total_usdt": 0.0,
        "funding_usdt": 0.0,
        "funding_missing": 0,
        "exit_stop_original": 0,
        "exit_stop_be": 0,
        "exit_stop_trail": 0,
        "exit_target_2r": 0,
        "exit_analysis": 0,
        "dropped_risk_n": dropped_risk_n,
    }


def summarize_virtual(rows: list[dict[str, Any]]) -> dict[str, Any]:
    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        if virtual_risk_unusable(
            entry=row.get("entry"),
            stop=row.get("stop"),
            loss_at_stop=row.get("loss_at_stop"),
        ):
            dropped += 1
            continue
        kept.append(row)
    if not kept:
        return _empty_virtual(dropped_risk_n=dropped)
    rows = kept
    nets = [float(r.get("net_r") or 0.0) for r in rows]
    pnls = [float(r.get("pnl_usdt") or 0.0) for r in rows]
    fundings = [float(r.get("funding_usdt") or 0.0) for r in rows]
    win_rs = [x for x in nets if x > 0]
    loss_rs = [x for x in nets if x < 0]
    wins = len(win_rs)
    total_r = sum(nets)
    exit_stop_original = 0
    exit_stop_be = 0
    exit_stop_trail = 0
    exit_stop_trailed = 0
    exit_target = 0
    exit_analysis = 0
    for row in rows:
        reason = str(row.get("exit_reason") or "").lower()
        if reason == "initial_stop":
            exit_stop_original += 1
        elif reason == "be_stop":
            exit_stop_be += 1
        elif reason == "trail_stop":
            exit_stop_trail += 1
        elif reason == "stop":
            # Rows closed before stop labels existed: best guess from gross R.
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
        "median_r": statistics.median(nets),
        "avg_win_r": statistics.mean(win_rs) if win_rs else None,
        "avg_loss_r": statistics.mean(loss_rs) if loss_rs else None,
        "total_usdt": sum(pnls),
        "funding_usdt": sum(fundings),
        "funding_missing": sum(int(r.get("funding_missing") or 0) for r in rows),
        "exit_stop_original": exit_stop_original,
        "exit_stop_be": exit_stop_be,
        # Legacy "stop" rows with gross R > 0 count as trail.
        "exit_stop_trail": exit_stop_trail + exit_stop_trailed,
        "exit_target_2r": exit_target,
        "exit_analysis": exit_analysis,
        "dropped_risk_n": dropped,
    }


def summarize_income(
    rows: list[dict[str, Any]],
    is_quick: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    realized = Decimal("0")
    funding = Decimal("0")
    commission = Decimal("0")
    other = Decimal("0")
    realized_n = 0
    quick_usdt = Decimal("0")
    quick_n = 0
    for row in rows:
        itype = str(row.get("incomeType") or "")
        if itype not in INCOME_TYPES:
            continue
        try:
            amount = Decimal(str(row.get("income") or "0"))
        except Exception:  # noqa: BLE001
            continue
        if is_quick is not None and is_quick(row):
            quick_usdt += amount
            quick_n += 1
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
    out: dict[str, Any] = {
        "realized_pnl": float(realized),
        "funding": float(funding),
        "commission": float(commission),
        "universe_usdt": float(uni),
        "other_usdt": float(other),
        "realized_n": realized_n,
    }
    if quick_n >= 1:
        out["quick_usdt"] = float(quick_usdt)
        out["quick_n"] = quick_n
    return out


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
    virtual_rows = list_closed_tracks_since(conn, since_ms=week_start, cohort=TRACK_COHORT, kind="sent")
    virtual = summarize_virtual(virtual_rows)
    held_rows = list_closed_tracks_since(conn, since_ms=week_start, cohort=TRACK_COHORT, kind="held")
    held = summarize_virtual(held_rows)
    blocked_rows = list_closed_tracks_since(conn, since_ms=week_start, cohort=TRACK_COHORT, kind="blocked")
    blocked_by_reason: dict[str, dict[str, Any]] = {}
    for reason in _BLOCKED_REASON_ORDER:
        group = [r for r in blocked_rows if str(r.get("block_reason") or "") == reason]
        if group:
            blocked_by_reason[reason] = summarize_virtual(group)
    closed_all = count_closed_tracks_all(conn, cohort=TRACK_COHORT, kind="sent")

    client = futures if futures is not None else build_futures_client()
    real: dict[str, Any] = {
        "realized_pnl": 0.0,
        "funding": 0.0,
        "commission": 0.0,
        "universe_usdt": 0.0,
        "other_usdt": 0.0,
        "realized_n": 0,
    }
    is_quick = quick_income_predicate(conn, now_ms=ts)
    if client is not None:
        try:
            income = client.income_history(start_ms=week_start, end_ms=ts, limit=1000)
            real = summarize_income(income, is_quick=is_quick)
        except Exception as exc:  # noqa: BLE001
            real["error"] = type(exc).__name__

    real_vs_virtual: dict[str, Any] | None = None
    real_report_error: str | None = None
    if client is not None:
        try:
            report = run_real_report(conn, client, since_ms=SPEC_START_MS, until_ms=ts)
            real_vs_virtual = report["summary"]
        except Exception as exc:  # noqa: BLE001 — never block the weekly summary
            real_report_error = type(exc).__name__
            real_vs_virtual = summarize_real_vs_virtual(conn, since_ms=SPEC_START_MS, until_ms=ts)
    else:
        real_vs_virtual = summarize_real_vs_virtual(conn, since_ms=SPEC_START_MS, until_ms=ts)

    journal = summarize_journal(conn, since_ms=week_start, until_ms=ts)
    quick_stats = summarize_quick(conn, since_ms=week_start, until_ms=ts)
    text = format_weekly_summary(
        counts=counts,
        virtual=virtual,
        real=real,
        closed_all=closed_all,
        held=held,
        blocked=blocked_by_reason or None,
        real_vs_virtual=None if real_report_error else real_vs_virtual,
        real_report_error=real_report_error,
        journal=journal,
        window_start_ms=week_start,
        window_end_ms=ts,
        quick=quick_stats
        if int(quick_stats.get("n") or 0) > 0 or real.get("quick_usdt") is not None
        else None,
    )
    sent_ok = bool(send_fn(text))
    return {
        "ok": True,
        "paper_only": True,
        "at_ms": ts,
        "text": text,
        "telegram": sent_ok,
        "counts": counts,
        "virtual": virtual,
        "held": held,
        "blocked": blocked_by_reason,
        "real": real,
        "real_vs_virtual": real_vs_virtual,
        "real_report_error": real_report_error,
        "journal": journal,
        "closed_all": closed_all,
    }
