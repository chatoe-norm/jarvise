"""Quick Plan A1: bot-placed Quick entry helpers (flag-gated, default off).

No exchange writes live here except what callers invoke. Pure/flag helpers plus
confirm-card / guard / universe / night / snapshot / cap checks used by
``qk:ex``, ``ey:`` (origin=quick), and ``run_entry_sequence``.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.messages import _fmt_price_tick, fmt_price, fmt_qty
from jarvise_assist.pause import is_paused
from jarvise_assist.quick_cap import count_open_same_side, same_side_cap_reached
from jarvise_assist.quick_config import (
    UNIVERSE_ORDER,
    effective_quick_values,
    get_quick_config,
)
from jarvise_assist.quick_cutoff import in_entry_block
from jarvise_assist.quick_regime import quick_side_risk, quick_trend_4h
from jarvise_assist.quick_tags import quick_symbols
from jarvise_assist.signals import build_existing_ideas
from jarvise_assist.sizing import (
    SignalCandidate,
    assign_shared_leverage,
    below_min_notional,
    floor_to_step,
    portfolio_risk_cap_pct,
    real_stop_loss_usdt,
    round_level_to_tick,
    size_signal,
)
from jarvise_assist.store import (
    ensure_settings,
    list_explore_symbols,
    symbol_has_in_flight_exec,
)

# Re-export so execute.py never names quick_cutoff (menu_reduce pin).
in_entry_block_for_bot = in_entry_block

logger = logging.getLogger(__name__)

_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")

BTN_QUICK_BOT_ENTER = "⚡ ให้บอทเข้าไม้"
MSG_CFG_CHANGED = "ค่าตั้งเปลี่ยน — กด 🔄 อัปเดตราคา"
MSG_QUICK_NO_TP1 = "ไม่วาง TP1 — ไม้ครึ่งต่ำกว่า minQty/minNotional, SL คุมเต็มจำนวน"
MSG_QUICK_TP1_OFF = "ไม่วาง TP1 — ตั้งปิดที่ TP1 ไว้ 0%, SL คุมเต็มจำนวน"
MSG_QUICK_HALF_NOTE = "หลังปิดครึ่งด้วยมือ คำสั่ง TP ของบอทอาจปิดส่วนที่เหลือที่ TP1 ได้"

# Keys frozen into preview.cfg and re-checked for stricter drift at confirm.
_SNAPSHOT_KEYS: tuple[str, ...] = (
    "quick_risk_pct",
    "quick_risk_cap_pct",
    "quick_counter_risk_pct",
    "quick_combined_risk_warn_pct",
    "quick_exec_universe",
    "quick_tp1_r",
    "quick_tp1_close_pct",
    "quick_night_start",
    "quick_night_end",
    "quick_confirm_ttl_s",
    "quick_chase_r",
    "quick_stop_buffer_pct",
    "quick_stop_buffer_ticks",
    "quick_max_fee_r",
    "quick_max_leverage",
    "quick_same_side_cap",
    "quick_fee_bps_per_side",
)


def quick_execute_enabled() -> bool:
    """JARVISE_QUICK_EXECUTE=on and EXECUTE in {testnet, live}. Ignores main enabled (D5)."""
    if (os.environ.get("JARVISE_QUICK_EXECUTE") or "off").strip().lower() != "on":
        return False
    mode = (os.environ.get("JARVISE_ASSIST_EXECUTE") or "off").strip().lower()
    return mode in {"testnet", "live"}


def _hhmm_minutes(hhmm: str) -> int:
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def in_quick_bot_night(
    now_ms: int,
    *,
    start: str = "23:30",
    end: str = "07:30",
) -> bool:
    """True when Asia/Bangkok local time is inside [start, end) (midnight wrap OK)."""
    local = datetime.fromtimestamp(int(now_ms) / 1000.0, tz=_BANGKOK)
    cur = local.hour * 60 + local.minute
    s = _hhmm_minutes(start)
    e = _hhmm_minutes(end)
    if s == e:
        return False
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e


def universe_line_for_mode(mode: str) -> str:
    if mode == "core":
        return "บอทเข้าไม้ได้เฉพาะ 7 เหรียญหลัก"
    if mode == "any":
        return "บอทเข้าไม้ได้ทุกเหรียญ USDT-M ที่ถูกต้อง"
    return "บอทเข้าไม้ได้เฉพาะ 7 เหรียญหลัก + เหรียญทดลอง"


def symbol_in_quick_exec_universe(conn: Any, symbol: str, *, universe: str | None = None) -> bool:
    sym = str(symbol).upper()
    mode = universe
    if mode is None:
        mode = str(get_quick_config(conn).values.get("quick_exec_universe") or "core_explore")
    mode = str(mode)
    if mode == "any":
        return True
    if mode == "core":
        return sym in ASSIST_SYMBOLS
    # core_explore (default)
    if sym in ASSIST_SYMBOLS:
        return True
    return sym in set(list_explore_symbols(conn))


def snapshot_cfg(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _SNAPSHOT_KEYS:
        if key in values:
            v = values[key]
            out[key] = str(v) if isinstance(v, Decimal) else v
    # Effective clamps also frozen for display / re-size.
    eff = effective_quick_values(values)
    out["risk_used"] = str(eff["risk_used"])
    out["counter_used"] = str(eff["counter_used"])
    out["lev_used"] = int(eff["lev_used"])
    return out


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def cfg_became_stricter(frozen: dict[str, Any], live: dict[str, Any]) -> bool:
    """True when any live value moved in the stricter direction vs the preview snapshot."""
    live_eff = effective_quick_values(live)
    # Lower risk / counter / cap
    for key, live_key in (
        ("quick_risk_pct", "quick_risk_pct"),
        ("quick_risk_cap_pct", "quick_risk_cap_pct"),
        ("quick_counter_risk_pct", "quick_counter_risk_pct"),
        ("risk_used", "risk_used"),
        ("counter_used", "counter_used"),
    ):
        if key not in frozen:
            continue
        live_v = live_eff[live_key] if live_key in live_eff else live.get(live_key)
        if live_v is None:
            continue
        if _dec(live_v) < _dec(frozen[key]):
            return True
    # Shorter TTL
    if "quick_confirm_ttl_s" in frozen:
        if int(live["quick_confirm_ttl_s"]) < int(frozen["quick_confirm_ttl_s"]):
            return True
    # Tighter chase / buffer / feer / lev / sides
    if "quick_chase_r" in frozen and _dec(live["quick_chase_r"]) < _dec(frozen["quick_chase_r"]):
        return True
    if "quick_max_fee_r" in frozen and _dec(live["quick_max_fee_r"]) < _dec(frozen["quick_max_fee_r"]):
        return True
    if "quick_stop_buffer_pct" in frozen and _dec(live["quick_stop_buffer_pct"]) > _dec(
        frozen["quick_stop_buffer_pct"]
    ):
        return True
    if "quick_stop_buffer_ticks" in frozen and int(live["quick_stop_buffer_ticks"]) > int(
        frozen["quick_stop_buffer_ticks"]
    ):
        return True
    if "quick_max_leverage" in frozen and int(live_eff["lev_used"]) < int(
        frozen.get("lev_used") or frozen["quick_max_leverage"]
    ):
        return True
    if "quick_same_side_cap" in frozen and int(live["quick_same_side_cap"]) < int(
        frozen["quick_same_side_cap"]
    ):
        return True
    if "quick_fee_bps_per_side" in frozen and _dec(live["quick_fee_bps_per_side"]) > _dec(
        frozen["quick_fee_bps_per_side"]
    ):
        return True
    # Night window: any newly blocked minute is stricter (coverage, not just width).
    if "quick_night_start" in frozen and "quick_night_end" in frozen:
        frozen_block = night_blocked_minutes(str(frozen["quick_night_start"]), str(frozen["quick_night_end"]))
        live_block = night_blocked_minutes(str(live["quick_night_start"]), str(live["quick_night_end"]))
        if live_block - frozen_block:
            return True
    # Universe narrower: core < core_explore < any
    if "quick_exec_universe" in frozen:
        try:
            fi = UNIVERSE_ORDER.index(str(frozen["quick_exec_universe"]))
            li = UNIVERSE_ORDER.index(str(live["quick_exec_universe"]))
        except ValueError:
            return True
        if li < fi:
            return True
    return False


def quick_chase_exceeded(
    *,
    side: str,
    entry: Decimal,
    stop: Decimal,
    mark: Decimal,
    chase_r: Decimal,
) -> bool:
    r = abs(entry - stop)
    if r == 0:
        return True
    limit = chase_r * r
    side_l = side.lower()
    if side_l == "long":
        return mark > entry + limit
    if side_l == "short":
        return mark < entry - limit
    return True


def quick_stop_buffer_ok(
    *,
    side: str,
    mark: Decimal,
    stop: Decimal,
    tick: Decimal,
    buffer_pct: Decimal,
    buffer_ticks: int,
) -> bool:
    min_gap = max(mark * (buffer_pct / Decimal("100")), tick * Decimal(int(buffer_ticks)))
    if side == "long":
        return mark - stop >= min_gap
    return stop - mark >= min_gap


def sum_quick_open_risk_usdt(conn: Any) -> Decimal:
    """Sum open Quick trade + registration price-risk at stop (no fees).

    Extracted from ``build_quick_card`` — keep the arithmetic identical.
    """
    quick_open = Decimal("0")
    for row in conn.execute("SELECT risk_usdt FROM assist_quick_trade WHERE status='open'").fetchall():
        quick_open += Decimal(str(row[0] or 0))
    for row in conn.execute(
        """
        SELECT planned_entry, planned_stop, qty FROM assist_quick_registration
        WHERE status='open'
        """
    ).fetchall():
        pe, ps, q = Decimal(str(row[0])), Decimal(str(row[1])), Decimal(str(row[2]))
        quick_open += abs(pe - ps) * q
    return quick_open


def check_quick_and_portfolio_caps(
    conn: Any,
    *,
    balance: Decimal,
    new_risk: Decimal,
    positions: list[Any] | None,
    orders: list[Any] | None,
    now_ms: int,
    cap_pct: Decimal,
    main_risk_pct: Decimal | None = None,
) -> str | None:
    """Return a reason code when Quick or portfolio cap is breached, else None.

    Confirm guards re-check through this helper; ``build_quick_card`` still
    formats its own Thai refusal lines from the same sums.
    """
    if balance <= 0:
        return "size_failed"
    main_pct = (
        Decimal(str(main_risk_pct))
        if main_risk_pct is not None
        else Decimal(str(ensure_settings(conn)["risk_pct"]))
    )
    portfolio_cap = portfolio_risk_cap_pct(main_pct)
    main_ideas = build_existing_ideas(
        conn,
        positions=list(positions or []),
        orders=list(orders or []),
        balance=balance,
        risk_pct=main_pct,
        now_ms=now_ms,
        include_sent_held=False,
    )
    main_open = sum((i.risk_usdt for i in main_ideas), Decimal("0"))
    quick_open = sum_quick_open_risk_usdt(conn)
    cap_usdt = balance * (cap_pct / Decimal("100"))
    if quick_open + new_risk > cap_usdt:
        return "quick_cap"
    if main_open + quick_open + new_risk > balance * (portfolio_cap / Decimal("100")):
        return "portfolio_cap"
    return None


def combined_same_side_risk_usdt(
    conn: Any,
    *,
    side: str,
    new_risk: Decimal,
    positions: list[Any],
    orders: list[Any],
    balance: Decimal,
    now_ms: int,
) -> Decimal:
    """Main + Quick open risk in the same direction, plus this new Quick risk."""
    main_risk_pct = Decimal(str(ensure_settings(conn)["risk_pct"]))
    main_ideas = build_existing_ideas(
        conn,
        positions=positions,
        orders=orders,
        balance=balance,
        risk_pct=main_risk_pct,
        now_ms=now_ms,
        include_sent_held=False,
    )
    side_l = side.lower()
    total = new_risk
    for idea in main_ideas:
        if str(idea.side).lower() == side_l:
            total += idea.risk_usdt
    for row in conn.execute(
        """
        SELECT side, risk_usdt FROM assist_quick_trade WHERE status='open'
        """
    ).fetchall():
        if str(row[0]).lower() == side_l:
            total += Decimal(str(row[1] or 0))
    for row in conn.execute(
        """
        SELECT side, planned_entry, planned_stop, qty
        FROM assist_quick_registration WHERE status='open'
        """
    ).fetchall():
        if str(row[0]).lower() != side_l:
            continue
        total += abs(Decimal(str(row[1])) - Decimal(str(row[2]))) * Decimal(str(row[3]))
    return total


def tp1_leg_qty(
    exec_qty: Decimal,
    *,
    step: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
    mark: Decimal,
    close_pct: Decimal,
) -> tuple[Decimal | None, str | None]:
    """Return (tp_qty, None) when both legs legal, else (None, reason).

    ``close_pct`` 0 → no TP1. 100 → full qty at TP1 when that leg is legal.
    """
    if close_pct <= 0:
        return None, "tp1pct_0"
    share = min(Decimal("100"), close_pct) / Decimal("100")
    close = floor_to_step(exec_qty * share, step)
    leave = exec_qty - close
    if close <= 0:
        return None, "dust_close"
    if below_min_notional(qty=close, notional=close * mark, min_qty=min_qty, min_notional=min_notional):
        return None, "dust_close"
    if leave > 0 and below_min_notional(
        qty=leave, notional=leave * mark, min_qty=min_qty, min_notional=min_notional
    ):
        return None, "dust_leave"
    return close, None


def build_bot_plan(
    conn: Any,
    *,
    symbol: str,
    side: str,
    entry: Decimal,
    stop: Decimal,
    balance: Decimal,
    available: Decimal,
    step: Decimal,
    tick: Decimal,
    min_qty: Decimal,
    min_notional: Decimal,
    now_ms: int,
    positions: list[Any] | None = None,
    orders: list[Any] | None = None,
    explore_conn: Any | None = None,
) -> dict[str, Any] | None:
    """Size a Quick bot entry from live config. None when size fails."""
    cfg_view = get_quick_config(conn)
    values = cfg_view.values
    eff = effective_quick_values(cfg_view)
    trend = quick_trend_4h(conn, symbol, now_ms=now_ms, explore_conn=explore_conn)
    is_counter, risk_pct = quick_side_risk(trend, side, eff)
    fee_bps = Decimal(str(values["quick_fee_bps_per_side"]))
    lev_cap = int(eff["lev_used"])
    sized = size_signal(
        symbol=symbol,
        side=side,
        entry=entry,
        stop=stop,
        balance=balance,
        risk_pct=risk_pct,
        step_size=step,
        fee_bps_per_side=fee_bps,
    )
    if sized is None:
        return None
    # Clamp leverage to Quick lev_used (already ≤ 5).
    leverage = min(int(sized.leverage), lev_cap)
    if leverage < 1:
        leverage = 1
    uncapped = int(math.ceil(float(sized.notional / balance))) if balance > 0 else leverage
    if uncapped > lev_cap:
        max_notional = balance * Decimal(lev_cap)
        qty = floor_to_step(max_notional / entry, step)
        if qty <= 0:
            return None
        sized = replace(
            sized,
            qty=qty,
            notional=qty * entry,
            loss_at_stop=qty * abs(entry - stop),
            leverage=lev_cap,
            leverage_capped=True,
            uncapped_leverage=uncapped,
        )
        leverage = lev_cap
    else:
        sized = replace(sized, leverage=leverage)

    # Margin: #392 0.95-available + open-fee buffer at clamped leverage.
    cand = SignalCandidate(sized=sized, confidence=1.0, symbol_rank=0)
    admitted, dropped = assign_shared_leverage([cand], available_balance=available, max_leverage=leverage)
    if dropped or not admitted:
        return None
    sized = admitted[0].sized
    leverage = int(sized.leverage)
    # Margin shrink can drop below exchange minimums — refuse before registration.
    if below_min_notional(
        qty=sized.qty,
        notional=sized.qty * entry,
        min_qty=min_qty,
        min_notional=min_notional,
    ):
        return None

    tp1_r = Decimal(str(values["quick_tp1_r"]))
    tp1_pct = Decimal(str(values["quick_tp1_close_pct"]))
    r_dist = abs(entry - stop)
    if side.lower() == "long":
        t1 = entry + tp1_r * r_dist
    else:
        t1 = entry - tp1_r * r_dist
    t1 = round_level_to_tick(t1, tick, side=side, kind="target")
    tp_half_qty, tp_refuse = tp1_leg_qty(
        sized.qty,
        step=step,
        min_qty=min_qty,
        min_notional=min_notional,
        mark=t1,
        close_pct=tp1_pct,
    )

    loss_fee = real_stop_loss_usdt(qty=sized.qty, entry=entry, stop=stop, fee_bps_per_side=fee_bps)
    warn_pct = Decimal(str(values["quick_combined_risk_warn_pct"]))
    combined = combined_same_side_risk_usdt(
        conn,
        side=side,
        new_risk=sized.loss_at_stop,
        positions=list(positions or []),
        orders=list(orders or []),
        balance=balance,
        now_ms=now_ms,
    )
    combined_warn = balance > 0 and (combined / balance * Decimal("100")) > warn_pct

    return {
        "qty": sized.qty,
        "entry": entry,
        "stop": stop,
        "t1": t1,
        "tp_half_qty": tp_half_qty,
        "tp_refuse": tp_refuse,
        "leverage": leverage,
        "loss_at_stop": sized.loss_at_stop,
        "loss_at_stop_incl_fees": loss_fee,
        "risk_pct_used": risk_pct,
        "counter_trend": is_counter,
        "trend": trend,
        "combined_warn": combined_warn,
        "fee_bps": fee_bps,
        "cfg": snapshot_cfg(values),
        "sized": sized,
    }


def unfinished_entry_for_card(conn: Any, card_id: str, *, now_ms: int | None = None) -> str | None:
    """Return intent id of an unfinished entry for this card, if any.

    Elapsed ``preview`` intents are ignored so a refreshed card is not stuck on
    ``double_tap``. Confirmed / unknown / recovering still block.
    """
    from jarvise_assist.store import now_ms as _store_now_ms

    ts = int(now_ms if now_ms is not None else _store_now_ms())
    rows = conn.execute(
        """
        SELECT id, preview_json, status, expires_at_ms FROM assist_exec_intent
        WHERE kind='entry' AND status IN ('preview', 'confirmed', 'unknown', 'recovering')
        """
    ).fetchall()
    import json

    for row in rows:
        status = str(row["status"] or "")
        if status == "preview" and int(row["expires_at_ms"] or 0) <= ts:
            continue
        try:
            preview = json.loads(str(row["preview_json"] or "{}"))
        except Exception:  # noqa: BLE001
            continue
        if str(preview.get("card_id") or "") == str(card_id) and str(preview.get("origin") or "") == "quick":
            return str(row["id"])
    return None


def quick_entry_proven_unfilled(conn: Any, intent_id: str) -> bool:
    """True only when the market entry never filled (safe to expire registration).

    Filled entries (stop/TP/emergency after fill) and unresolved unknowns keep
    the registration so sync can tag the Quick loss.
    """
    rows = conn.execute(
        """
        SELECT step, order_or_algo_id FROM assist_exec_log
        WHERE intent_id=?
        ORDER BY created_at_ms ASC
        """,
        (str(intent_id),),
    ).fetchall()
    steps = {str(r["step"] or "") for r in rows}
    fill_evidence = {
        "stop",
        "tp",
        "emergency_close",
        "cls",
        "exit_market",
        "recover_slr",
        "dup_stop_cancel",
        "orphan_cancel",
    }
    if steps & fill_evidence:
        return False
    market_steps = {
        "market",
        "market_attempt",
        "market_query",
        "recover_market_query",
        "recovery",
        "entry_price_query",
    }
    if not (steps & market_steps):
        return True
    intent = conn.execute(
        "SELECT status, result_text FROM assist_exec_intent WHERE id=?",
        (str(intent_id),),
    ).fetchone()
    if intent is not None and str(intent["status"] or "") in {"unknown", "recovering", "confirmed"}:
        return False
    # Terminal recovery not_found (deterministic client-id NOT_FOUND × passes
    # while flat) with no fill-evidence steps → entry never filled; expire.
    if intent is not None and str(intent["status"] or "") == "not_found":
        return True
    text = str(intent["result_text"] or "") if intent is not None else ""
    if "เข้าไม้ไม่สำเร็จ" in text or "ยังไม่ได้ส่งคำสั่ง" in text:
        return True
    # Ambiguous market attempt without fill-evidence steps — keep registration.
    return False


def expire_quick_registration_for_intent(conn: Any, intent_id: str, *, now_ms: int) -> None:
    """Expire an open registration linked to this exec intent (clear fail / recovery)."""
    try:
        conn.execute(
            """
            UPDATE assist_quick_registration
            SET status='expired', updated_at_ms=?
            WHERE exec_intent_id=? AND status='open'
            """,
            (int(now_ms), str(intent_id)),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.exception("expire quick registration for intent failed")


def maybe_expire_quick_registration(conn: Any, intent_id: str, *, now_ms: int) -> None:
    """Expire only when the entry is proven unfilled."""
    if not quick_entry_proven_unfilled(conn, intent_id):
        return
    expire_quick_registration_for_intent(conn, intent_id, now_ms=now_ms)


def any_non_jv_open_order(orders: list[Any], symbol: str) -> bool:
    """True when any non-jv- order exists on the symbol (reduce-only included)."""
    sym = str(symbol).upper()
    for order in orders:
        if str(order.get("symbol") or "").upper() != sym:
            continue
        cid = str(order.get("clientAlgoId") or order.get("clientOrderId") or "")
        if cid.startswith("jv-"):
            continue
        return True
    return False


def format_quick_confirm_text(
    plan: dict[str, Any], *, symbol: str, side: str, ttl_s: int, tick: Decimal
) -> str:
    side_l = "LONG" if side.lower() == "long" else "SHORT"
    qty = Decimal(str(plan["qty"]))
    entry = Decimal(str(plan["entry"]))
    stop = Decimal(str(plan["stop"]))
    t1 = Decimal(str(plan["t1"]))
    lev = int(plan["leverage"])
    loss = Decimal(str(plan["loss_at_stop_incl_fees"]))
    risk = Decimal(str(plan["risk_pct_used"]))
    lines = [
        f"⚡ ยืนยันบอทเข้าไม้ · {symbol} · {side_l}",
        f"qty {fmt_qty(qty)} · entry {_fmt_price_tick(entry, tick)} · stop {_fmt_price_tick(stop, tick)}",
    ]
    tp_qty = plan.get("tp_half_qty")
    if tp_qty is not None:
        lines.append(f"TP1 {_fmt_price_tick(t1, tick)} × {fmt_qty(Decimal(str(tp_qty)))}")
    elif plan.get("tp_refuse") == "tp1pct_0":
        lines.append(MSG_QUICK_TP1_OFF)
    else:
        lines.append(MSG_QUICK_NO_TP1)
    lines.append(f"leverage {lev}x isolated · ถ้าชน stop ≈ {fmt_price(loss)} USDT (รวมค่าธรรมเนียม)")
    lines.append(f"risk {fmt_price(risk)}%")
    if plan.get("counter_trend"):
        trend = str(plan.get("trend") or "unknown")
        if trend == "unknown":
            lines.append(f"🔴 ไม่ทราบเทรนด์ · บอทใช้ risk {fmt_price(risk)}%")
        else:
            lines.append(f"🔴 สวนเทรนด์ · บอทใช้ risk {fmt_price(risk)}%")
    if plan.get("combined_warn"):
        lines.append("⚠ ความเสี่ยงทางเดียวกัน (หลัก+quick) เกินระดับเตือน")
    if tp_qty is not None:
        lines.append(MSG_QUICK_HALF_NOTE)
    lines.append(f"ยืนยันภายใน {int(ttl_s)} วินาที")
    return "\n".join(lines)


def pause_blocks_bot(conn: Any, *, now_ms: int) -> bool:
    return is_paused(ensure_settings(conn), now_ms=now_ms)


def quick_same_side_cap_text(side: str, count: int, cap: int) -> str:
    """Bot path (qk:ex) refusal with the configured ``quick_same_side_cap``."""
    label = "LONG" if str(side).lower() == "long" else "SHORT"
    return f"ไม่เข้าไม้ — ฝั่ง {label} เปิดอยู่แล้ว {int(count)} ไม้ (เพดานฝั่งละ {int(cap)}) · ปิดหรือให้ทะเบียนหมดอายุก่อน"


def same_side_blocks(conn: Any, side: str, *, cap: int | None = None) -> bool:
    """Bot path: use configured ``quick_same_side_cap`` (or an explicit stricter cap)."""
    if cap is None:
        cap = int(get_quick_config(conn).values["quick_same_side_cap"])
    return same_side_cap_reached(count_open_same_side(conn, side), cap=int(cap))


def has_open_watch(conn: Any, symbol: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM assist_watch_position
        WHERE symbol=? AND closed_at_ms IS NULL
        """,
        (str(symbol).upper(),),
    ).fetchone()
    return row is not None


def symbol_busy_for_bot(conn: Any, symbol: str, *, exclude_id: str | None = None) -> str | None:
    """Return a reason when the symbol cannot take a new bot Quick entry.

    ``exclude_id`` skips the current claimed intent so confirm does not refuse itself.
    """
    sym = str(symbol).upper()
    if sym in quick_symbols(conn):
        return "quick_open"
    if has_open_watch(conn, sym):
        return "open_watch"
    if symbol_has_in_flight_exec(conn, sym, exclude_id=exclude_id):
        return "in_flight"
    return None


def night_blocked_minutes(start: str, end: str) -> set[int]:
    """Set of Asia/Bangkok minute-of-day values blocked by [start, end)."""
    s = _hhmm_minutes(start)
    e = _hhmm_minutes(end)
    if s == e:
        return set()
    if s < e:
        return set(range(s, e))
    return set(range(s, 24 * 60)) | set(range(0, e))


def cfg_val(frozen: dict[str, Any], key: str, live: Any) -> Any:
    """Frozen snapshot value when present (incl. 0); else live. Never treat 0 as missing."""
    if key in frozen and frozen[key] is not None:
        return frozen[key]
    return live
