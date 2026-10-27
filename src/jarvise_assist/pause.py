"""Losing-streak and weekly-loss pause for trade-assistant signal/morning jobs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.futures import BinanceFuturesClient
from jarvise_assist.store import ensure_settings, get_edge, write_edge, write_settings

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
WEEK_LOSS_PCT = Decimal("6")
SendFn = Callable[..., Any]

RESUME_TEXT = "▶️ กลับมาส่ง signal แล้ว"
STREAK_PAUSE_TEXT = "⏸ หยุดส่ง signal ถึงพรุ่งนี้ 08:20 — แพ้ติดกัน 3 ไม้"


def next_0820_bangkok_ms(now_ms: int) -> int:
    """Next 08:20 Asia/Bangkok strictly after now (or today if still before 08:20)."""
    local = datetime.fromtimestamp(now_ms / 1000.0, tz=BANGKOK)
    target = local.replace(hour=8, minute=20, second=0, microsecond=0)
    if local >= target:
        target = target + timedelta(days=1)
    return int(target.timestamp() * 1000)


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


def format_paused_until(paused_until_ms: int) -> str:
    local = datetime.fromtimestamp(paused_until_ms / 1000.0, tz=BANGKOK)
    return f"หยุดพักถึง {local.strftime('%H:%M')}"


def is_paused(settings: dict[str, Any], *, now_ms: int) -> bool:
    until = settings.get("paused_until_ms")
    return until is not None and int(until) > int(now_ms)


def clear_pause_manual(conn: Any) -> None:
    """Owner toggled assist on: clear pause and suppress the auto-resume line."""
    write_settings(conn, paused_until_ms=None)
    write_edge(conn, resume_alerted=True)


def maybe_auto_resume(conn: Any, *, now_ms: int, send: SendFn) -> bool:
    """Clear expired pause and send the resume line once. Returns True when resumed."""
    settings = ensure_settings(conn)
    until = settings.get("paused_until_ms")
    if until is None:
        return False
    if int(until) > int(now_ms):
        return False
    edge = get_edge(conn)
    write_settings(conn, paused_until_ms=None)
    if not edge["resume_alerted"]:
        send(RESUME_TEXT)
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


def evaluate_pause(
    conn: Any,
    *,
    futures: BinanceFuturesClient,
    now_ms: int,
    send: SendFn,
) -> dict[str, Any]:
    """Engage streak or weekly-loss pause. Call after maybe_auto_resume."""
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
    realized = [
        r
        for r in _universe_income(income, income_type="REALIZED_PNL")
        if Decimal(str(r.get("income") or "0")) != 0
    ]
    realized.sort(key=lambda r: int(r.get("time") or 0))

    # Three latest non-zero realized PnL all negative.
    if len(realized) >= 3:
        last3 = realized[-3:]
        if all(Decimal(str(r.get("income") or "0")) < 0 for r in last3):
            anchor = int(last3[-1].get("time") or now_ms)
            if edge["streak_anchor_ms"] != anchor:
                until = next_0820_bangkok_ms(now_ms)
                write_settings(conn, paused_until_ms=until)
                write_edge(conn, streak_anchor_ms=anchor, resume_alerted=False)
                send(STREAK_PAUSE_TEXT)
                return {
                    "paused": True,
                    "reason": "streak",
                    "until_ms": until,
                    "anchor_ms": anchor,
                }

    # Week net loss > 6% of reconstructed week-start balance.
    typed = [
        r for r in income if str(r.get("incomeType") or "") in {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}
    ]
    week_net = _net_income_usdt(typed)
    week_start_bal = balance - week_net
    week_id = bangkok_week_id(now_ms)
    if week_start_bal > 0 and week_net < 0:
        loss_pct = (-week_net / week_start_bal) * Decimal("100")
        if loss_pct > WEEK_LOSS_PCT and edge["week_pause_id"] != week_id:
            until = next_0820_bangkok_ms(now_ms)
            write_settings(conn, paused_until_ms=until)
            write_edge(conn, week_pause_id=week_id, resume_alerted=False)
            send(week_loss_pause_text(loss_pct))
            return {
                "paused": True,
                "reason": "week_loss",
                "until_ms": until,
                "loss_pct": float(loss_pct),
                "week_id": week_id,
            }

    return {"paused": False}
