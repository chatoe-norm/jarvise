"""Losing-streak, weekly-loss, and Bangkok-day loss pause for the trade assistant."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.futures import BinanceFuturesClient
from jarvise_assist.quick_tags import quick_income_predicate
from jarvise_assist.store import (
    RISK_MAX_PCT,
    ensure_settings,
    get_edge,
    now_ms,
    write_edge,
    write_settings,
)

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
WEEK_LOSS_R = Decimal("4")
DAY_LOSS_CAP_R = Decimal("3")
DAY_LOSS_DEEPER_R = Decimal("1")
_R_STORE = Decimal("0.0001")
_R_SHOW = Decimal("0.1")
BOT_ENTRIES_ONLY_TH = "นับเฉพาะไม้ที่เข้าผ่านบอท"
CLOSE_EVENT_MERGE_MS = 120_000
TRADING_INCOME_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})
TRANSFER_INCOME_TYPES = frozenset(
    {
        "TRANSFER",
        "INTERNAL_TRANSFER",
        "CROSS_COLLATERAL_TRANSFER",
        "STRATEGY_UMFUTURES_TRANSFER",
    }
)
SendFn = Callable[..., Any]

RESUME_TEXT = "▶️ กลับมาส่ง signal แล้ว"
STREAK_PAUSE_TEXT = "⏸ หยุดส่ง signal ถึงพรุ่งนี้ 08:20 — แพ้ติดกัน 3 ไม้"


def week_start_risk_pct(conn: Any, *, now_ms_value: int | None = None) -> Decimal:
    """Frozen assist risk_pct for the Bangkok week (week-loss threshold = 4R).

    Snapshots on first call of the week. Local SQLite only; never writes assist_settings.
    """
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    week_id = bangkok_week_id(ts)
    edge = get_edge(conn)
    if edge["week_risk_id"] == week_id and edge["week_risk_pct"] is not None:
        return Decimal(str(edge["week_risk_pct"]))
    pct = min(float(ensure_settings(conn)["risk_pct"]), RISK_MAX_PCT)
    write_edge(conn, week_risk_id=week_id, week_risk_pct=pct)
    return Decimal(str(pct))


def next_0820_bangkok_ms(now_ms: int) -> int:
    """Next 08:20 Asia/Bangkok strictly after now (or today if still before 08:20)."""
    local = datetime.fromtimestamp(now_ms / 1000.0, tz=BANGKOK)
    target = local.replace(hour=8, minute=20, second=0, microsecond=0)
    if local >= target:
        target = target + timedelta(days=1)
    return int(target.timestamp() * 1000)


def bangkok_day_id(now_ms: int) -> str:
    """Calendar day in Asia/Bangkok, YYYY-MM-DD. The boundary is 00:00."""
    local = datetime.fromtimestamp(now_ms / 1000.0, tz=BANGKOK)
    return local.strftime("%Y-%m-%d")


def bangkok_day_start_ms(now_ms: int) -> int:
    """00:00 Asia/Bangkok on the calendar day that contains now."""
    local = datetime.fromtimestamp(now_ms / 1000.0, tz=BANGKOK)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def bangkok_week_id(now_ms: int) -> str:
    """ISO-like week id in Asia/Bangkok (Monday-start)."""
    local = datetime.fromtimestamp(now_ms / 1000.0, tz=BANGKOK)
    monday = local - timedelta(days=local.weekday())
    return monday.strftime("%Y-%m-%d")


def monday_bangkok_ms(now_ms: int) -> int:
    local = datetime.fromtimestamp(now_ms / 1000.0, tz=BANGKOK)
    monday = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
    return int(monday.timestamp() * 1000)


def week_loss_pause_text(pct: Decimal) -> str:
    return f"⏸ หยุดส่ง signal ถึงพรุ่งนี้ 08:20 — ขาดทุนสัปดาห์นี้ {float(pct):.1f}%"


def format_signed_r(net: Decimal) -> str:
    """One decimal. Negatives keep the minus sign; positives get a plus."""
    shown = net.quantize(_R_SHOW, rounding=ROUND_HALF_UP)
    if shown > 0:
        return f"+{shown}R"
    return f"{shown}R"


def _bangkok_hhmm(ms: int) -> str:
    local = datetime.fromtimestamp(ms / 1000.0, tz=BANGKOK)
    return local.strftime("%H:%M")


def day_loss_pause_text(net: Decimal, *, until_ms: int) -> str:
    """End clock comes from the real pause time, the same way format_paused_until does."""
    clock = _bangkok_hhmm(until_ms)
    return f"⏸ หยุดส่ง signal ถึง {clock} — วันนี้ {format_signed_r(net)} ({BOT_ENTRIES_ONLY_TH})"


def day_loss_deeper_pause_text(net: Decimal, *, until_ms: int) -> str:
    clock = _bangkok_hhmm(until_ms)
    return (
        f"⏸ หยุดส่ง signal ถึง {clock} — วันนี้ {format_signed_r(net)} ลึกกว่าตอนเปิดต่ออีก 1R ({BOT_ENTRIES_ONLY_TH})"
    )


def day_loss_resume_text(net: Decimal) -> str:
    """Closed bot-entered trades only. Open positions are not in this number."""
    shown = format_signed_r(net)
    note = f"จากไม้ที่ปิดแล้ว ({BOT_ENTRIES_ONLY_TH})"
    if net < 0:
        return f"วันนี้ติดลบแล้ว {shown} {note}"
    return f"วันนี้สุทธิ {shown} {note}"


def _quantize_r(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(_R_STORE, rounding=ROUND_HALF_UP)


def format_paused_until(paused_until_ms: int) -> str:
    return f"หยุดพักถึง {_bangkok_hhmm(paused_until_ms)}"


def is_paused(settings: dict[str, Any], *, now_ms: int) -> bool:
    until = settings.get("paused_until_ms")
    return until is not None and int(until) > int(now_ms)


def clear_pause_manual(conn: Any, *, now_ms_value: int | None = None) -> str | None:
    """Owner toggled assist on: clear pause and suppress the auto-resume line.

    When today already fired the -3R cap, the return value warns with today's
    closed bot-entered net R. An active pause (paused_until_ms > now) stores
    that net R and clears the deeper marker, so the next -1R can pause again.
    A toggle while no pause is active does not move the stored level.
    """
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    until = ensure_settings(conn).get("paused_until_ms")
    was_paused = until is not None and int(until) > ts
    write_settings(conn, paused_until_ms=None, pause_reason=None)
    write_edge(conn, resume_alerted=True)
    edge = get_edge(conn)
    day_id = bangkok_day_id(ts)
    if edge.get("day_pause_id") != day_id:
        return None
    net = day_bot_net_r(conn, now_ms=ts)
    if was_paused:
        write_edge(
            conn,
            day_resume_net_r=float(net),
            day_resume_id=day_id,
            day_deeper_pause_id=None,
        )
    return day_loss_resume_text(net)


def maybe_auto_resume(conn: Any, *, now_ms: int, send: SendFn) -> bool:
    """Clear expired pause and send the resume line once. Returns True when resumed.

    A same-day day-loss pause that ends at 08:20 also arms the next -1R
    threshold, the same store clear_pause_manual uses for an active pause.
    A pause from an earlier day, or a streak/week pause with no day-loss
    pause today, does not move that level.
    """
    settings = ensure_settings(conn)
    until = settings.get("paused_until_ms")
    if until is None:
        return False
    if int(until) > int(now_ms):
        return False
    edge = get_edge(conn)
    write_settings(conn, paused_until_ms=None, pause_reason=None)
    day_id = bangkok_day_id(now_ms)
    note: str | None = None
    if edge.get("day_pause_id") == day_id:
        net = day_bot_net_r(conn, now_ms=now_ms)
        write_edge(
            conn,
            day_resume_net_r=float(net),
            day_resume_id=day_id,
            day_deeper_pause_id=None,
        )
        note = day_loss_resume_text(net)
    if not edge["resume_alerted"]:
        send(RESUME_TEXT if note is None else f"{RESUME_TEXT} — {note}")
        write_edge(conn, resume_alerted=True)
        return True
    write_edge(conn, resume_alerted=True)
    return False


def _universe_income(
    rows: list[dict[str, Any]],
    *,
    income_type: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        if sym and sym not in ASSIST_SYMBOLS:
            continue
        if income_type is not None and str(row.get("incomeType") or "") != income_type:
            continue
        out.append(row)
    return out


def _net_income_usdt(rows: list[dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        try:
            total += Decimal(str(row.get("income") or "0"))
        except Exception:  # noqa: BLE001
            continue
    return total


def _close_events(
    realized: list[dict[str, Any]],
    *,
    merge_ms: int = CLOSE_EVENT_MERGE_MS,
) -> list[dict[str, Any]]:
    """Group REALIZED_PNL rows into close events (same symbol within merge_ms)."""
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(realized, key=lambda r: (str(r.get("symbol") or ""), int(r.get("time") or 0))):
        sym = str(row.get("symbol") or "").upper()
        by_symbol.setdefault(sym, []).append(row)

    events: list[dict[str, Any]] = []
    for sym, rows in by_symbol.items():
        group: list[dict[str, Any]] = []
        group_end = 0
        for row in rows:
            t = int(row.get("time") or 0)
            if group and t - group_end > merge_ms:
                events.append(
                    {
                        "symbol": sym,
                        "sum": _net_income_usdt(group),
                        "time": group_end,
                    }
                )
                group = []
            group.append(row)
            group_end = t
        if group:
            events.append(
                {
                    "symbol": sym,
                    "sum": _net_income_usdt(group),
                    "time": group_end,
                }
            )
    events.sort(key=lambda e: int(e["time"]))
    return events


def day_bot_net_r(conn: Any, *, now_ms: int) -> Decimal:
    """Sum planned-R for bot entries whose exit falls in today's Bangkok day.

    The day is `exit_time_ms` in [today 00:00, now), not the sync time.
    Hand-opened trades have no intent and are left out. A bot entry the owner
    closes by hand still has an intent, so that loss counts. Wins offset losses.
    """
    start = bangkok_day_start_ms(now_ms)
    rows = conn.execute(
        """
        SELECT net_r_real
        FROM assist_real_trade
        WHERE status = 'closed'
          AND side IN ('long', 'short')
          AND loss_at_stop > 0
          AND intent_id IS NOT NULL
          AND length(trim(intent_id)) > 0
          AND exit_time_ms >= ?
          AND exit_time_ms < ?
          AND net_r_real IS NOT NULL
        """,
        (start, int(now_ms)),
    ).fetchall()
    total = Decimal("0")
    for row in rows:
        raw = row["net_r_real"] if hasattr(row, "keys") else row[0]
        total += Decimal(str(raw))
    return _quantize_r(total)


def _engage_day_pause(
    conn: Any,
    *,
    now_ms: int,
    send: SendFn,
    net: Decimal,
    day_id: str,
    deeper: bool,
) -> dict[str, Any]:
    until = next_0820_bangkok_ms(now_ms)
    reason = "day_loss_deeper" if deeper else "day_loss"
    write_settings(conn, paused_until_ms=until, pause_reason=reason)
    if deeper:
        write_edge(conn, day_deeper_pause_id=day_id, resume_alerted=False)
        send(day_loss_deeper_pause_text(net, until_ms=until))
    else:
        write_edge(conn, day_pause_id=day_id, resume_alerted=False)
        send(day_loss_pause_text(net, until_ms=until))
    return {
        "paused": True,
        "reason": reason,
        "until_ms": until,
        "net_r": float(net),
        "day_id": day_id,
    }


def evaluate_day_loss_pause(
    conn: Any,
    *,
    now_ms: int,
    send: SendFn,
) -> dict[str, Any]:
    """Pause on the Bangkok day's closed bot-entered net R. Local DB only.

    Call after streak and week-loss, and again after a watch journal sync.
    The -3R cap fires once per Bangkok day and lasts until the next 08:20.
    The day sum still resets at 00:00. Each manual resume from an active pause
    arms a new threshold 1R below the net R at that resume. Yesterday's stored
    day id does not apply today. Does not close positions.
    """
    settings = ensure_settings(conn)
    if is_paused(settings, now_ms=now_ms):
        return {"paused": True, "reason": "already", "until_ms": settings["paused_until_ms"]}

    day_id = bangkok_day_id(now_ms)
    net = day_bot_net_r(conn, now_ms=now_ms)
    edge = get_edge(conn)
    resume_net = edge.get("day_resume_net_r")
    if (
        edge.get("day_pause_id") == day_id
        and edge.get("day_resume_id") == day_id
        and resume_net is not None
        and edge.get("day_deeper_pause_id") != day_id
    ):
        threshold = _quantize_r(resume_net) - DAY_LOSS_DEEPER_R
        if net <= threshold:
            return _engage_day_pause(
                conn,
                now_ms=now_ms,
                send=send,
                net=net,
                day_id=day_id,
                deeper=True,
            )

    if edge.get("day_pause_id") != day_id and net <= -DAY_LOSS_CAP_R:
        return _engage_day_pause(
            conn,
            now_ms=now_ms,
            send=send,
            net=net,
            day_id=day_id,
            deeper=False,
        )

    return {"paused": False, "net_r": float(net), "day_id": day_id}


def evaluate_pause(
    conn: Any,
    *,
    futures: BinanceFuturesClient,
    now_ms: int,
    send: SendFn,
) -> dict[str, Any]:
    """Engage streak, weekly-loss, then day-loss pause. Call after maybe_auto_resume."""
    week_risk = week_start_risk_pct(conn, now_ms_value=now_ms)
    settings = ensure_settings(conn)
    if is_paused(settings, now_ms=now_ms):
        return {"paused": True, "reason": "already", "until_ms": settings["paused_until_ms"]}

    week_start = monday_bangkok_ms(now_ms)
    try:
        balance = futures.usdt_wallet_balance()
        income = futures.income_history(start_ms=week_start, end_ms=now_ms, limit=1000)
    except Exception as exc:  # noqa: BLE001
        return {"paused": False, "error": type(exc).__name__}

    edge = get_edge(conn)
    is_quick = quick_income_predicate(conn, now_ms=now_ms)
    realized = [
        r
        for r in _universe_income(income, income_type="REALIZED_PNL")
        if Decimal(str(r.get("income") or "0")) != 0 and not is_quick(r)
    ]
    events = _close_events(realized)

    # Three latest close events all negative (a split fill is one event).
    if len(events) >= 3:
        last3 = events[-3:]
        if all(Decimal(str(e["sum"])) < 0 for e in last3):
            anchor = int(last3[-1]["time"] or now_ms)
            if edge["streak_anchor_ms"] != anchor:
                until = next_0820_bangkok_ms(now_ms)
                write_settings(conn, paused_until_ms=until, pause_reason="streak")
                write_edge(conn, streak_anchor_ms=anchor, resume_alerted=False)
                send(STREAK_PAUSE_TEXT)
                return {
                    "paused": True,
                    "reason": "streak",
                    "until_ms": until,
                    "anchor_ms": anchor,
                }

    # Week trading net loss > 4R × week-start risk of reconstructed week-start balance.
    trading = [r for r in income if str(r.get("incomeType") or "") in TRADING_INCOME_TYPES]
    transfers = [r for r in income if str(r.get("incomeType") or "") in TRANSFER_INCOME_TYPES]
    trading_net = _net_income_usdt(trading)
    transfer_net = _net_income_usdt(transfers)
    week_start_bal = balance - trading_net - transfer_net
    week_id = bangkok_week_id(now_ms)
    threshold_pct = WEEK_LOSS_R * week_risk
    if week_start_bal > 0 and trading_net < 0:
        loss_pct = (-trading_net / week_start_bal) * Decimal("100")
        if loss_pct > threshold_pct and edge["week_pause_id"] != week_id:
            until = next_0820_bangkok_ms(now_ms)
            write_settings(conn, paused_until_ms=until, pause_reason="week_loss")
            write_edge(conn, week_pause_id=week_id, resume_alerted=False)
            send(week_loss_pause_text(loss_pct))
            return {
                "paused": True,
                "reason": "week_loss",
                "until_ms": until,
                "loss_pct": float(loss_pct),
                "week_id": week_id,
                "threshold_pct": float(threshold_pct),
                "week_risk_pct": float(week_risk),
            }

    return evaluate_day_loss_pause(conn, now_ms=now_ms, send=send)
