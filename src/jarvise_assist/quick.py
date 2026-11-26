"""Quick (scalping) assist: settings, registration, fill tagging, report.

No orders. Binance access is read-only GET and only when a registration or
open quick trade exists. Never writes assist_settings or assist_edge.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from jarvise_assist import ASSIST_SYMBOLS
from jarvise_assist.futures import FuturesPosition
from jarvise_assist.messages import fmt_price, fmt_qty
from jarvise_assist.order_lines import format_closing_order_line
from jarvise_assist.pause import bangkok_day_start_ms, format_paused_until, main_week_loss_pause_until
from jarvise_assist.quick_errors import (
    e_confirm_stale,
    e_confirm_stale_ttl,
    e_setting_reject,
    e_setting_risk_over_cap,
)
from jarvise_assist.quick_tags import quick_symbols
from jarvise_assist.real_trades import _fee_usdt_and_other, fetch_all_user_trades, fetch_income_range
from jarvise_assist.sizing import portfolio_risk_cap_pct
from jarvise_assist.store import ensure_settings, list_explore_symbols

logger = logging.getLogger(__name__)
_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")

SendFn = Callable[..., Any]

REG_TTL_MS = 10 * 60_000
REG_EXPIRE_GRACE_MS = 2 * 60_000
# Same bound as execute.RECOVER_MAX_AGE_MS (2h). Unresolved exec-linked
# registrations stay open for fill reconciliation even when the position is
# already flat (emergency-close fills often lack clientOrderId).
UNRESOLVED_EXEC_KEEP_MS = 2 * 60 * 60_000
QUICK_SAMPLE_TARGET = 25

QUICK_USAGE = "พิมพ์: q BTC long 67000 (stop เป็นราคา) หรือ q BTC short 0.4% (stop ห่างจากราคา %)"

RISK_ALLOWED = (0.5, 0.75, 1.0)
CAP_ALLOWED = (1.0, 1.5, 2.0)
QUICK_CONFIRM_TTL_S = 60
QUICK_CONFIRM_FUTURE_S = 5
_QUICK_OK_RE = re.compile(r"^qs:ok:(risk|cap):([^:]+):([0-9]{10})$")

_QUICK_SETTING_SIDE = frozenset({"long", "short", "l", "s", "buy", "sell"})
_QUICK_SETTING_VALUE_RE = re.compile(r"(?:[0-9]{1,3}(?:\.[0-9]+)?|\.[0-9]+)")

MSG_QUICK_LOCK_REMOVED = "ตั้งค่านี้ถูกยกเลิกแล้ว — quick ไม่มีล็อกรายวัน/แพ้ติดกัน (ยังหยุดเมื่อขาดทุนสัปดาห์เกิน 4R)"
MSG_QUICK_SETTING_REJECT = e_setting_reject()
MSG_QUICK_CONFIRM_STALE = e_confirm_stale()
MSG_QUICK_SETTING_HINT_RISK = "พิมพ์ค่าเอง: q risk 0.6"
MSG_QUICK_SETTING_HINT_CAP = "พิมพ์ค่าเอง: q cap 1.2"
MSG_QUICK_SYMBOL_BUSY = "มีไม้ quick ของ {base} เปิดอยู่"


def _cb(data: str) -> str:
    raw = data.encode("utf-8")
    if len(raw) > 64:
        raise ValueError(f"callback_data exceeds 64 bytes: {data!r} ({len(raw)})")
    return data


def _symbol_base(sym: str) -> str:
    s = str(sym).upper()
    return s[:-4] if s.endswith("USDT") else s


def quick_symbol_busy_toast(symbol: str) -> str:
    return MSG_QUICK_SYMBOL_BUSY.format(base=_symbol_base(symbol))


def build_coin_menu_keyboard(conn: Any) -> dict[str, Any]:
    """Coin-first Quick menu keyboard (core + explore + other/close)."""
    busy = quick_symbols(conn)
    rows: list[list[dict[str, str]]] = []

    def _chunk(buttons: list[dict[str, str]], n: int = 3) -> None:
        for i in range(0, len(buttons), n):
            rows.append(buttons[i : i + n])

    core_btns: list[dict[str, str]] = []
    for sym in ASSIST_SYMBOLS:
        base = _symbol_base(sym)
        if sym in busy:
            core_btns.append({"text": f"⏳ {base}", "callback_data": _cb(f"qk:bz:{sym}")})
        else:
            core_btns.append({"text": base, "callback_data": _cb(f"qk:cs:{sym}")})
    _chunk(core_btns)

    explore_btns: list[dict[str, str]] = []
    for sym in list_explore_symbols(conn):
        base = _symbol_base(sym)
        if sym in busy:
            explore_btns.append({"text": f"⏳ {base}", "callback_data": _cb(f"qk:bz:{sym}")})
        else:
            explore_btns.append({"text": f"🧪 {base}", "callback_data": _cb(f"qk:cs:{sym}")})
    if explore_btns:
        _chunk(explore_btns)

    rows.append([{"text": "✏️ เหรียญอื่น (พิมพ์รหัส)", "callback_data": _cb("qk:ot")}])
    return {"inline_keyboard": rows}


def get_quick_settings(conn: Any) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM assist_quick_settings WHERE id=1").fetchone()
    if row is None:
        conn.execute(
            """
            INSERT OR IGNORE INTO assist_quick_settings
            (id, risk_pct, day_max_loss_r, streak_lock_n, cap_pct)
            VALUES (1, 1.0, 2, 3, 1.0)
            """
        )
        conn.commit()
        row = conn.execute("SELECT * FROM assist_quick_settings WHERE id=1").fetchone()
    return dict(row)


def write_quick_settings(conn: Any, **fields: Any) -> dict[str, Any]:
    allowed = {
        "risk_pct",
        "cap_pct",
        "updated_at_ms",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for key, val in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        vals.append(val)
    if not sets:
        return get_quick_settings(conn)
    vals.append(1)
    conn.execute(f"UPDATE assist_quick_settings SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    return get_quick_settings(conn)


def create_quick_registration(
    conn: Any,
    *,
    symbol: str,
    side: str,
    planned_entry: float,
    planned_stop: float,
    qty: float,
    now_ms: int,
    live_positions: Sequence[FuturesPosition] | None = None,
    commit: bool = True,
    exec_intent_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Create an open registration. Returns (id, None) or (None, reason_code)."""
    sym = str(symbol).upper()
    side_l = str(side).lower()
    if side_l not in {"long", "short"}:
        return None, "invalid_side"
    if float(qty) <= 0:
        return None, "qty_non_positive"
    entry = float(planned_entry)
    stop = float(planned_stop)
    if side_l == "long" and stop >= entry:
        return None, "stop_wrong_side"
    if side_l == "short" and stop <= entry:
        return None, "stop_wrong_side"

    open_reg = conn.execute(
        """
        SELECT id FROM assist_quick_registration
        WHERE symbol=? AND status='open'
        """,
        (sym,),
    ).fetchone()
    if open_reg is not None:
        return None, "open_registration"

    open_trade = conn.execute(
        """
        SELECT id FROM assist_quick_trade
        WHERE symbol=? AND status='open'
        """,
        (sym,),
    ).fetchone()
    if open_trade is not None:
        return None, "open_quick_trade"

    watch = conn.execute(
        """
        SELECT 1 FROM assist_watch_position
        WHERE symbol=? AND closed_at_ms IS NULL
        """,
        (sym,),
    ).fetchone()
    if watch is not None:
        return None, "open_assist_watch"

    if live_positions is not None:
        for pos in live_positions:
            if str(pos.symbol).upper() == sym:
                return None, "live_position"

    rid = f"aqr_{uuid.uuid4().hex[:16]}"
    expires = int(now_ms) + REG_TTL_MS
    conn.execute(
        """
        INSERT INTO assist_quick_registration (
            id, symbol, side, planned_entry, planned_stop, qty,
            created_at_ms, expires_at_ms, status, trade_id, updated_at_ms,
            exec_intent_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?)
        """,
        (
            rid,
            sym,
            side_l,
            entry,
            stop,
            float(qty),
            int(now_ms),
            expires,
            int(now_ms),
            str(exec_intent_id) if exec_intent_id else None,
        ),
    )
    if commit:
        conn.commit()
    return rid, None


def _opening_side(side: str) -> str:
    return "BUY" if side == "long" else "SELL"


def _closing_side(side: str) -> str:
    return "SELL" if side == "long" else "BUY"


def _weighted_avg(legs: list[tuple[float, float]]) -> tuple[float, float]:
    qty = sum(q for q, _ in legs)
    if qty <= 0:
        return 0.0, 0.0
    avg = sum(q * p for q, p in legs) / qty
    return qty, avg


def _upsert_trade(conn: Any, row: dict[str, Any]) -> None:
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    conn.execute(
        f"""
        INSERT INTO assist_quick_trade ({", ".join(cols)})
        VALUES ({placeholders})
        ON CONFLICT(id) DO UPDATE SET {updates}
        """,
        [row[c] for c in cols],
    )
    conn.commit()


def _rebuild_open_trade(
    conn: Any,
    *,
    trade_id: str,
    registration_id: str,
    symbol: str,
    side: str,
    planned_stop: float,
    fills: list[dict[str, Any]],
    first_fill: dict[str, Any],
    now_ms: int,
    client: Any,
) -> dict[str, Any]:
    """Stateless rebuild from fills after the opening fill. Returns trade info."""
    open_side = _opening_side(side)
    close_side = _closing_side(side)
    chron = sorted(fills, key=lambda r: (int(r.get("time") or 0), int(r.get("id") or 0)))
    # Only fills at/after the first fill id/time
    first_id = int(first_fill.get("id") or 0)
    first_t = int(first_fill.get("time") or 0)
    chron = [
        f
        for f in chron
        if int(f.get("time") or 0) > first_t
        or (int(f.get("time") or 0) == first_t and int(f.get("id") or 0) >= first_id)
    ]

    entry_legs: list[tuple[float, float]] = []
    exit_legs: list[tuple[float, float]] = []
    entry_ids: list[int] = []
    exit_ids: list[int] = []
    realized = 0.0
    commission = 0.0
    other_parts: list[str] = []
    net_qty = 0.0
    closed_at: int | None = None
    status = "open"

    for fill in chron:
        qty = float(fill.get("qty") or 0)
        price = float(fill.get("price") or 0)
        tid = int(fill.get("id") or 0)
        fside = str(fill.get("side") or "").upper()
        if qty <= 0 or tid <= 0:
            continue
        if fside == open_side:
            entry_legs.append((qty, price))
            entry_ids.append(tid)
            net_qty += qty
            fee, other = _fee_usdt_and_other(fill)
            commission += fee
            if other:
                other_parts.append(other)
            continue
        if fside != close_side:
            continue
        # Closing fill — may cross zero
        if net_qty <= 0:
            continue
        close_qty = min(qty, net_qty)
        fraction = close_qty / qty if qty > 0 else 1.0
        exit_legs.append((close_qty, price))
        exit_ids.append(tid)
        try:
            realized += float(fill.get("realizedPnl") or 0)
        except (TypeError, ValueError):
            pass
        fee, other = _fee_usdt_and_other(fill, fraction=fraction)
        commission += fee
        if other:
            other_parts.append(other)
        net_qty -= close_qty
        if net_qty <= 1e-12:
            net_qty = 0.0
            closed_at = int(fill.get("time") or 0)
            status = "closed"
            break

    entry_qty, entry_avg = _weighted_avg(entry_legs)
    _exit_qty, exit_avg = _weighted_avg(exit_legs) if exit_legs else (0.0, 0.0)
    entry_time = int(first_fill.get("time") or 0)
    risk_usdt = abs(entry_avg - float(planned_stop)) * entry_qty if entry_qty > 0 else 0.0
    funding = 0.0
    if status == "closed" and closed_at is not None:
        fund_rows = fetch_income_range(
            client,
            symbol=symbol,
            start_ms=entry_time + 1,
            end_ms=closed_at + 1,
            income_type="FUNDING_FEE",
        )
        for row in fund_rows:
            tms = int(row.get("time") or 0)
            if entry_time < tms <= closed_at:
                try:
                    funding += float(row.get("income") or 0)
                except (TypeError, ValueError):
                    pass

    other_asset = ";".join(other_parts) if other_parts else None
    net_usdt = realized + commission + funding
    net_r: float | None
    fee_r: float | None
    if other_asset or risk_usdt <= 0:
        net_r = None
        fee_r = None
    else:
        net_r = net_usdt / risk_usdt
        fee_r = (-commission) / risk_usdt

    payload = {
        "id": trade_id,
        "registration_id": registration_id,
        "symbol": symbol,
        "side": side,
        "planned_stop": float(planned_stop),
        "entry_time_ms": entry_time,
        "entry_qty": entry_qty,
        "entry_avg": entry_avg,
        "closed_at_ms": closed_at,
        "exit_avg": exit_avg if exit_legs else None,
        "entry_trade_ids": json.dumps(entry_ids),
        "exit_trade_ids": json.dumps(exit_ids),
        "realized_pnl_usdt": realized if status == "closed" else None,
        "commission_usdt": commission,
        "commission_other_asset": other_asset,
        "funding_usdt": funding if status == "closed" else None,
        "net_usdt": net_usdt if status == "closed" else None,
        "risk_usdt": risk_usdt,
        "net_r": net_r if status == "closed" else None,
        "fee_r": fee_r if status == "closed" else None,
        "status": status,
        "updated_at_ms": int(now_ms),
    }
    # For open trades still store entry/exit ids and partial commission
    if status == "open":
        payload["commission_usdt"] = commission
        payload["realized_pnl_usdt"] = realized if exit_ids else None
    _upsert_trade(conn, payload)
    return payload


def sync_quick_trades(
    conn: Any,
    client: Any,
    *,
    now_ms: int,
    send: SendFn,
) -> dict[str, Any]:
    """Tag fills onto registrations and rebuild open trades. Return {} if idle."""
    open_regs = conn.execute("SELECT * FROM assist_quick_registration WHERE status='open'").fetchall()
    open_trades = conn.execute("SELECT * FROM assist_quick_trade WHERE status='open'").fetchall()
    if not open_regs and not open_trades:
        return {}

    info: dict[str, Any] = {"tagged": 0, "expired": 0, "updated": 0, "closed": []}

    tagged_trade_ids: set[str] = set()
    for reg in open_regs:
        rid = str(reg["id"])
        sym = str(reg["symbol"]).upper()
        side = str(reg["side"])
        created = int(reg["created_at_ms"])
        expires = int(reg["expires_at_ms"])
        exec_intent_id = None
        try:
            raw_eid = reg["exec_intent_id"] if "exec_intent_id" in reg.keys() else None
            exec_intent_id = str(raw_eid) if raw_eid else None
        except Exception:  # noqa: BLE001
            exec_intent_id = None

        # Exec-linked: tag by bot market orderId only (never time-match).
        bot_order_id: str | None = None
        bot_client_id: str | None = None
        if exec_intent_id:
            # Same formula as execute.client_order_id — avoid importing execute (cycle).
            short = str(exec_intent_id).replace("aei_", "")[:12]
            bot_client_id = f"jv-{short}-mkt"[:36]
            log = conn.execute(
                """
                SELECT order_or_algo_id, client_order_id, step FROM assist_exec_log
                WHERE intent_id=? AND step IN (
                    'market', 'market_query', 'recover_market_query',
                    'recovery', 'entry_price_query', 'mkt_order_resolve'
                )
                ORDER BY created_at_ms DESC
                """,
                (exec_intent_id,),
            ).fetchall()
            for row in log:
                oid = str(row["order_or_algo_id"] or "") or None
                if oid:
                    bot_order_id = oid
                    break
                cid = str(row["client_order_id"] or "")
                if cid:
                    bot_client_id = cid

        # Broaden fetch window for exec-linked (fill may precede created_at on clock skew).
        fetch_start = created - (3_600_000 if exec_intent_id else 0)
        fills = fetch_all_user_trades(client, sym, start_ms=fetch_start, end_ms=int(now_ms) + 1)

        if exec_intent_id:
            # Resolve order id via fill clientOrderId when log only has the deterministic cid.
            if not bot_order_id and bot_client_id:
                for f in fills:
                    if str(f.get("clientOrderId") or "") == bot_client_id:
                        bot_order_id = str(f.get("orderId") or "") or None
                        if bot_order_id:
                            break
            if bot_order_id:
                after = [f for f in fills if str(f.get("orderId") or "") == bot_order_id]
            elif bot_client_id:
                after = [f for f in fills if str(f.get("clientOrderId") or "") == bot_client_id]
            else:
                # Unresolved — wait; do not time-tag. Expiry grace still applies below.
                after = []
            after.sort(key=lambda r: (int(r.get("time") or 0), int(r.get("id") or 0)))
        else:
            # First fill after created decides: wrong side → expire; opening → tag or grace.
            after = [f for f in fills if int(f.get("time") or 0) >= created]
            after.sort(key=lambda r: (int(r.get("time") or 0), int(r.get("id") or 0)))

        if not after:
            # Exec-linked with no bot orderId yet: keep for reconciliation up to
            # UNRESOLVED_EXEC_KEEP_MS from created_at. Flat is not no-fill evidence
            # (emergency-close fills often lack clientOrderId until resolve).
            if exec_intent_id and not bot_order_id:
                if int(now_ms) <= created + UNRESOLVED_EXEC_KEEP_MS:
                    continue
                # Bound elapsed with still-unresolved orderId → expire as no_fill.
            elif int(now_ms) <= expires + REG_EXPIRE_GRACE_MS:
                continue
            elif exec_intent_id:
                # Have orderId but no matching fills yet, past grace: keep while open.
                still_open = False
                try:
                    pos_fn = getattr(client, "position_risk", None)
                    if callable(pos_fn):
                        try:
                            live = pos_fn(symbol=sym)
                        except TypeError:
                            live = pos_fn()
                        still_open = any(
                            str(getattr(p, "symbol", "") or "").upper() == sym for p in (live or [])
                        )
                except Exception:  # noqa: BLE001
                    # Unreadable position → do not expire (fail closed for attribution).
                    still_open = True
                if still_open:
                    continue
            conn.execute(
                """
                UPDATE assist_quick_registration
                SET status='expired', updated_at_ms=?
                WHERE id=?
                """,
                (int(now_ms), rid),
            )
            conn.commit()
            info["expired"] += 1
            _send_reg_expire_notice(
                send,
                sym=sym,
                side=side,
                kind="no_fill",
            )
            continue

        first = after[0]
        first_side = str(first.get("side") or "").upper()
        first_time = int(first.get("time") or 0)
        if first_side != _opening_side(side):
            conn.execute(
                """
                UPDATE assist_quick_registration
                SET status='expired', updated_at_ms=?
                WHERE id=?
                """,
                (int(now_ms), rid),
            )
            conn.commit()
            info["expired"] += 1
            opp = "SHORT" if side == "long" else "LONG"
            _send_reg_expire_notice(send, sym=sym, side=side, kind="wrong_side", opposite=opp)
            continue

        # Time-window late_fill only for record-only (non-exec) registrations.
        if not exec_intent_id and first_time > expires:
            if int(now_ms) > expires + REG_EXPIRE_GRACE_MS:
                conn.execute(
                    """
                    UPDATE assist_quick_registration
                    SET status='expired', updated_at_ms=?
                    WHERE id=?
                    """,
                    (int(now_ms), rid),
                )
                conn.commit()
                info["expired"] += 1
                _send_reg_expire_notice(send, sym=sym, side=side, kind="late_fill")
            continue

        trade_id = f"aqt_{uuid.uuid4().hex[:16]}"
        # Reuse existing trade id if somehow present
        existing = conn.execute(
            "SELECT id FROM assist_quick_trade WHERE registration_id=?", (rid,)
        ).fetchone()
        if existing is not None:
            trade_id = str(existing["id"])
        rebuilt = _rebuild_open_trade(
            conn,
            trade_id=trade_id,
            registration_id=rid,
            symbol=sym,
            side=side,
            planned_stop=float(reg["planned_stop"]),
            fills=fills,
            first_fill=first,
            now_ms=now_ms,
            client=client,
        )
        conn.execute(
            """
            UPDATE assist_quick_registration
            SET status='tagged', trade_id=?, updated_at_ms=?
            WHERE id=?
            """,
            (trade_id, int(now_ms), rid),
        )
        conn.commit()
        tagged_trade_ids.add(trade_id)
        info["tagged"] += 1
        # Suppress tag notice for exec-linked (its copy would be false).
        if not exec_intent_id:
            _send_tag_notice(conn, send, registration_id=rid, client=client, now_ms=now_ms)
        if str(rebuilt.get("status")) == "closed":
            raw_closed = rebuilt.get("closed_at_ms")
            try:
                closed_at = int(raw_closed) if raw_closed is not None else None
            except (TypeError, ValueError):
                closed_at = None
            if closed_at is not None and closed_at <= 0:
                closed_at = None
            info["closed"].append(
                {
                    "symbol": sym,
                    "side": side,
                    "closed_at_ms": closed_at,
                    "trade_id": trade_id,
                    "exec_linked": bool(exec_intent_id),
                    "exec_intent_id": exec_intent_id,
                    "registration_id": rid,
                }
            )
            _send_close_notice(conn, send, trade_id=trade_id, client=client, payload=rebuilt, now_ms=now_ms)

    # Rebuild already-open trades (skip ones tagged earlier in this run)
    open_trades = conn.execute("SELECT * FROM assist_quick_trade WHERE status='open'").fetchall()
    for trade in open_trades:
        trade_id = str(trade["id"])
        if trade_id in tagged_trade_ids:
            continue
        was_open = str(trade["status"]) == "open"
        sym = str(trade["symbol"]).upper()
        side = str(trade["side"])
        entry_ms = int(trade["entry_time_ms"] or 0)
        fills = fetch_all_user_trades(client, sym, start_ms=entry_ms, end_ms=int(now_ms) + 1)
        # Reconstruct a synthetic first_fill from stored entry
        entry_ids = json.loads(trade["entry_trade_ids"] or "[]")
        first_id = int(entry_ids[0]) if entry_ids else 0
        first_fill = next(
            (f for f in fills if int(f.get("id") or 0) == first_id),
            None,
        )
        if first_fill is None:
            # Fallback: earliest opening-side fill
            cand = [f for f in fills if str(f.get("side") or "").upper() == _opening_side(side)]
            cand.sort(key=lambda r: (int(r.get("time") or 0), int(r.get("id") or 0)))
            if not cand:
                continue
            first_fill = cand[0]
        rebuilt = _rebuild_open_trade(
            conn,
            trade_id=trade_id,
            registration_id=str(trade["registration_id"]),
            symbol=sym,
            side=side,
            planned_stop=float(trade["planned_stop"] or 0),
            fills=fills,
            first_fill=first_fill,
            now_ms=now_ms,
            client=client,
        )
        info["updated"] += 1
        if was_open and str(rebuilt.get("status")) == "closed":
            raw_closed = rebuilt.get("closed_at_ms")
            try:
                closed_at = int(raw_closed) if raw_closed is not None else None
            except (TypeError, ValueError):
                closed_at = None
            if closed_at is not None and closed_at <= 0:
                closed_at = None
            reg_row = conn.execute(
                "SELECT exec_intent_id FROM assist_quick_registration WHERE id=?",
                (str(trade["registration_id"]),),
            ).fetchone()
            eid = str(reg_row["exec_intent_id"]) if reg_row and reg_row["exec_intent_id"] else None
            exec_linked = bool(eid)
            info["closed"].append(
                {
                    "symbol": sym,
                    "side": side,
                    "closed_at_ms": closed_at,
                    "trade_id": trade_id,
                    "exec_linked": exec_linked,
                    "exec_intent_id": eid,
                    "registration_id": str(trade["registration_id"]),
                }
            )
            _send_close_notice(conn, send, trade_id=trade_id, client=client, payload=rebuilt, now_ms=now_ms)

    return {k: v for k, v in info.items() if v}


def _send_reg_expire_notice(
    send: SendFn,
    *,
    sym: str,
    side: str,
    kind: str,
    opposite: str | None = None,
) -> None:
    from jarvise_assist.quick_card import quick_mode_tag

    label = _side_menu_label(side)
    try:
        if kind == "no_fill":
            text = f"⌛ ลงทะเบียน quick {sym} {label} หมดเวลา — ไม่พบไม้ภายใน 10 นาที (ไม่นับเป็น quick)"
        elif kind == "wrong_side" and opposite:
            text = (
                f"⌛ ลงทะเบียน quick {sym} {label} ถูกยกเลิก — ไม้แรกเป็นฝั่ง {opposite} จึงนับเป็นไม้ปกติ (ระบบหลักเฝ้าให้)"
            )
        else:
            text = f"⌛ ลงทะเบียน quick {sym} {label} หมดเวลา — ไม้ที่เปิดหลังหมดเวลาไม่นับเป็น quick"
        send("\n".join([text, quick_mode_tag()]))
    except Exception:  # noqa: BLE001
        logger.exception("quick expire notice failed")


def _send_tag_notice(
    conn: Any,
    send: SendFn,
    *,
    registration_id: str,
    client: Any,
    now_ms: int,
) -> None:
    from jarvise_assist.quick_card import quick_mode_tag

    try:
        cur = conn.execute(
            """
            UPDATE assist_quick_registration
            SET notice_sent_at_ms=?
            WHERE id=? AND notice_sent_at_ms IS NULL
            """,
            (int(now_ms), registration_id),
        )
        if cur.rowcount == 0:
            return
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.exception("quick tag notice mark failed")
        return
    try:
        trade = conn.execute(
            "SELECT * FROM assist_quick_trade WHERE registration_id=?",
            (registration_id,),
        ).fetchone()
        reg = conn.execute(
            "SELECT * FROM assist_quick_registration WHERE id=?", (registration_id,)
        ).fetchone()
    except Exception:  # noqa: BLE001
        logger.exception("quick tag notice load failed")
        return
    if trade is None or reg is None:
        return
    try:
        sym = str(trade["symbol"])
        label = _side_menu_label(str(trade["side"]))
        risk = float(trade["risk_usdt"] or 0)
        body = "\n".join(
            [
                (
                    f"✅ จับไม้ quick ได้แล้ว — {sym} {label} "
                    f"เข้า {fmt_price(Decimal(str(trade['entry_avg'])))} · "
                    f"qty {fmt_qty(Decimal(str(trade['entry_qty'] or 0)))} · "
                    f"stop ที่ลงทะเบียน {fmt_price(Decimal(str(reg['planned_stop'])))}"
                ),
                f"ความเสี่ยงจริง ≈ {fmt_price(Decimal(str(risk)))} USDT · บอทไม่ได้วาง stop — ตั้ง stop ในแอปเอง",
                quick_mode_tag(),
            ]
        )
        send(body)
    except Exception:  # noqa: BLE001
        logger.exception("quick tag notice failed")


def _bot_menu_quick_close(
    conn: Any,
    *,
    symbol: str,
    side: str,
    entry_time_ms: int | None,
    closed_at_ms: int | None,
) -> bool:
    """True when a Quick menu exit placed an exit_market order for this trade window."""
    if entry_time_ms is None or int(entry_time_ms) == 0:
        return False
    if closed_at_ms is None:
        return False

    rows = conn.execute(
        """
        SELECT i.preview_json FROM assist_exec_intent i
        INNER JOIN assist_exec_log l ON l.intent_id = i.id
          AND l.step = 'exit_market' AND l.http_status = 200
        WHERE i.kind = 'exit'
          AND UPPER(i.symbol) = ?
          AND LOWER(i.side) = ?
          AND i.created_at_ms >= ?
          AND i.created_at_ms <= ?
        ORDER BY i.created_at_ms DESC
        """,
        (
            symbol.upper(),
            str(side).lower(),
            int(entry_time_ms),
            int(closed_at_ms),
        ),
    ).fetchall()
    for row in rows:
        try:
            preview = json.loads(row["preview_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(preview, dict):
            continue
        if preview.get("cohort") == "quick":
            return True
    return False


def _send_close_notice(
    conn: Any,
    send: SendFn,
    *,
    trade_id: str,
    client: Any,
    payload: dict[str, Any],
    now_ms: int,
    skip_reason: str | None = None,
) -> None:
    """skip_reason reserved for M1a-2 bot closes."""
    from jarvise_assist.quick_card import quick_mode_tag

    if skip_reason is not None:
        return
    try:
        cur = conn.execute(
            """
            UPDATE assist_quick_trade
            SET close_notice_at_ms=?
            WHERE id=? AND close_notice_at_ms IS NULL
            """,
            (int(now_ms), trade_id),
        )
        if cur.rowcount == 0:
            return
        conn.commit()
    except Exception:  # noqa: BLE001
        logger.exception("quick close notice mark failed")
        return
    try:
        sym = str(payload["symbol"])
        label = _side_menu_label(str(payload["side"]))
        net_usdt = float(payload.get("net_usdt") or 0)
        net_r = payload.get("net_r")
        other = payload.get("commission_other_asset")
        risk_usdt = float(payload.get("risk_usdt") or 0)
        if net_r is None:
            if other and risk_usdt > 0:
                r_line = f"R คำนวณไม่ได้ (ค่าธรรมเนียมเป็น {other})"
            else:
                r_line = "R คำนวณไม่ได้"
        else:
            r_line = f"{float(net_r):+.2f}R (รวมค่าธรรมเนียมจริง + funding)"
        entry_ms = payload.get("entry_time_ms")
        if entry_ms is None:
            try:
                row = conn.execute(
                    "SELECT entry_time_ms FROM assist_quick_trade WHERE id=?",
                    (trade_id,),
                ).fetchone()
                entry_ms = row["entry_time_ms"] if row is not None else None
            except Exception:  # noqa: BLE001
                entry_ms = None
        via = "(ปิดใน Binance)"
        try:
            if _bot_menu_quick_close(
                conn,
                symbol=sym,
                side=str(payload["side"]),
                entry_time_ms=entry_ms,
                closed_at_ms=payload.get("closed_at_ms"),
            ):
                via = "(ปิดผ่านบอท)"
        except Exception:  # noqa: BLE001
            logger.exception("quick close notice bot detect failed")
            via = "(ปิดใน Binance)"
        lines = [
            (f"🏁 ไม้ quick {sym} {label} ปิดแล้ว {via} — สุทธิ {net_usdt:+.2f} USDT · {r_line}"),
        ]
        from jarvise_assist.execute import assist_cancel_owner_enabled, assist_execute_mode
        from jarvise_assist.order_lines import is_symbol_protective_leftover

        # Watch owns the one cancel/list attempt when flag on AND live execute.
        # Flag on + testnet/off still lists leftovers here. Flag on + live skips
        # listing here (watch owns it) — watch must still warn if it cannot
        # obtain a trade client (missing key); silence is not acceptable.
        skip_leftover_list = assist_cancel_owner_enabled() and assist_execute_mode() == "live"
        if not skip_leftover_list:
            leftovers: list[dict[str, Any]] = []
            try:
                if client is not None and hasattr(client, "all_open_orders"):
                    leftovers = [
                        o for o in client.all_open_orders(symbol=sym) if is_symbol_protective_leftover(o)
                    ]
            except Exception:  # noqa: BLE001
                lines.append("อ่านคำสั่งค้างไม่ได้ — ตรวจในแอป")
            else:
                if leftovers:
                    lines.append(f"คำสั่งที่ยังค้างบน {sym}:")
                    tick = None
                    for o in leftovers:
                        lines.append(format_closing_order_line(o, tick=tick))
                    lines.append("✋ ยังค้างอยู่ — ลบในแอป")
        lines.append(quick_mode_tag())
        send("\n".join(lines))
    except Exception:  # noqa: BLE001
        logger.exception("quick close notice failed")


def summarize_quick(
    conn: Any,
    *,
    since_ms: int | None = None,
    until_ms: int,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT * FROM assist_quick_trade
        WHERE status='closed' AND closed_at_ms IS NOT NULL
        ORDER BY closed_at_ms ASC, id ASC
        """
    ).fetchall()
    eligible: list[dict[str, Any]] = []
    no_r_n = 0
    for row in rows:
        closed = int(row["closed_at_ms"])
        if since_ms is not None and closed < int(since_ms):
            continue
        if closed > int(until_ms):
            continue
        if row["net_r"] is None or row["commission_other_asset"]:
            no_r_n += 1
            continue
        eligible.append(dict(row))

    n = len(eligible)
    if n == 0:
        return {
            "n": 0,
            "no_r_n": no_r_n,
            "win_rate": 0.0,
            "sum_r": 0.0,
            "mean_r": 0.0,
            "avg_win_r": None,
            "avg_loss_r": None,
            "mean_fee_r": 0.0,
            "max_dd_r": 0.0,
            "today_r": 0.0,
            "streak": 0,
            "usdt": 0.0,
        }

    rs = [float(r["net_r"]) for r in eligible]
    fees = [float(r["fee_r"] or 0) for r in eligible]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    sum_r = sum(rs)
    mean_r = sum_r / n
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    day_start = bangkok_day_start_ms(until_ms)
    today_r = sum(float(r["net_r"]) for r in eligible if int(r["closed_at_ms"]) >= day_start)

    # Current streak from the end: negative = losing streak length, positive = wins.
    streak = 0
    for r in reversed(rs):
        if r < 0:
            if streak > 0:
                break
            streak -= 1
        elif r > 0:
            if streak < 0:
                break
            streak += 1
        else:
            break

    usdt = sum(float(r["net_usdt"] or 0) for r in eligible)
    return {
        "n": n,
        "no_r_n": no_r_n,
        "win_rate": (len(wins) / n) if n else 0.0,
        "sum_r": sum_r,
        "mean_r": mean_r,
        "avg_win_r": (sum(wins) / len(wins)) if wins else None,
        "avg_loss_r": (sum(losses) / len(losses)) if losses else None,
        "mean_fee_r": (sum(fees) / n) if n else 0.0,
        "max_dd_r": max_dd,
        "today_r": today_r,
        "streak": streak,
        "usdt": usdt,
    }


def _fmt_pct(v: float) -> str:
    if float(v) == int(v):
        return f"{int(v)}"
    return f"{v:g}"


def _fmt_fixed(v: float | Decimal) -> str:
    s = f"{float(v):.2f}"
    return s.rstrip("0").rstrip(".")


def _parse_quick_setting_value(raw: str, *, allow_trailing_pct: bool = False) -> Decimal | None:
    s = raw.strip()
    if allow_trailing_pct and s.endswith("%"):
        s = s[:-1].strip()
    if not _QUICK_SETTING_VALUE_RE.fullmatch(s):
        return None
    try:
        val = Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    if val <= 0 or val > 100:
        return None
    return val


def _is_quick_setting_preset(key: str, val: float) -> bool:
    allowed = RISK_ALLOWED if key == "risk" else CAP_ALLOWED if key == "cap" else ()
    return any(abs(val - float(p)) < 1e-9 for p in allowed)


def parse_quick_setting_command(text: str) -> dict[str, Any] | None:
    """Parse ``q risk …`` / ``q cap …`` for manual quick settings. None → other ``q`` handlers."""
    if not re.match(r"^\s*q(?:uick)?\s+(risk|cap)\b", text or "", re.I):
        return None
    parts = (text or "").strip().split()
    if len(parts) < 2:
        return None
    head = parts[0].lower()
    if head not in {"q", "quick"}:
        return None
    tokens = parts[1:]
    key = tokens[0].lower()
    if key not in {"risk", "cap"}:
        return None
    if len(tokens) == 1:
        return {"action": "hint", "key": key}
    if len(tokens) == 2:
        return {"action": "apply", "key": key, "raw": tokens[1]}
    if tokens[1].lower() not in _QUICK_SETTING_SIDE:
        return {"action": "hint", "key": key}
    return None


def handle_quick_setting_text(
    conn: Any,
    parsed: dict[str, Any],
    *,
    send: SendFn,
    now_ms: int,
) -> dict[str, Any]:
    if parsed.get("action") == "hint":
        send(MSG_QUICK_SETTING_HINT_RISK if parsed["key"] == "risk" else MSG_QUICK_SETTING_HINT_CAP)
        return {"ok": True, "hint": True}
    return _apply_quick_setting(
        conn,
        str(parsed["key"]),
        str(parsed["raw"]),
        send=send,
        now_ms=now_ms,
        confirmed=False,
        allow_trailing_pct=True,
    )


def _quick_settings_saved_lines(
    conn: Any, *, key: str, val: float, now_ms: int
) -> tuple[str, dict[str, Any]]:
    settings = get_quick_settings(conn)
    risk = float(settings["risk_pct"])
    cap = float(settings["cap_pct"])
    warn = ""
    if risk > cap:
        warn = f"{e_setting_risk_over_cap(_fmt_fixed(risk), _fmt_fixed(cap))}\n"
    text, kb = render_quick_menu(conn, now_ms=now_ms)
    if key == "risk":
        head = f"บันทึกความเสี่ยง {_fmt_fixed(val)}% แล้ว\n{warn}{text}"
    else:
        head = f"บันทึกเพดาน {_fmt_fixed(val)}% แล้ว\n{warn}{text}"
    return head, kb


def _menu_hhmm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=_BANGKOK).strftime("%H:%M")


def _side_menu_label(side: str) -> str:
    return "LONG" if str(side).lower() == "long" else "SHORT"


def _quick_live_lines(conn: Any, *, now_ms: int) -> list[str]:
    lines: list[str] = []
    regs = conn.execute(
        "SELECT * FROM assist_quick_registration WHERE status='open' ORDER BY expires_at_ms ASC"
    ).fetchall()
    for reg in regs:
        sym = str(reg["symbol"])
        side = _side_menu_label(str(reg["side"]))
        expires = int(reg["expires_at_ms"])
        if int(now_ms) < expires:
            rem_m = max(1, int((expires - int(now_ms) + 59_999) // 60_000))
            lines.append(f"⏳ ลงทะเบียน {sym} {side} — เหลือ {rem_m} นาที (ถึง {_menu_hhmm(expires)} น.)")
        elif int(now_ms) <= expires + REG_EXPIRE_GRACE_MS:
            lines.append(f"⏳ ลงทะเบียน {sym} {side} — หมดเวลาแล้ว รอตรวจรอบสุดท้าย")
    trades = conn.execute(
        "SELECT * FROM assist_quick_trade WHERE status='open' ORDER BY entry_time_ms ASC"
    ).fetchall()
    for tr in trades:
        sym = str(tr["symbol"])
        side = _side_menu_label(str(tr["side"]))
        entry = tr["entry_avg"]
        qty = tr["entry_qty"]
        stop = tr["planned_stop"]
        entry_ms = int(tr["entry_time_ms"] or now_ms)
        held_m = max(0, int((int(now_ms) - entry_ms) // 60_000))
        lines.append(
            f"📈 ไม้ quick เปิดอยู่: {sym} {side} · เข้า {fmt_price(Decimal(str(entry or 0)))} · "
            f"qty {fmt_qty(Decimal(str(qty or 0)))} · "
            f"stop ที่ลงทะเบียน {fmt_price(Decimal(str(stop or 0)))} · "
            f"ถือมา {held_m} นาที"
        )
        lines.append("ดูกำไร/ขาดทุนใน 📋 ไม้ที่ถืออยู่")
    if lines:
        lines.append("ข้อมูลอัปเดตทุก 5 นาที")
    return lines


def render_quick_menu(conn: Any, *, now_ms: int) -> tuple[str, dict[str, Any]]:
    from jarvise_assist.quick_card import quick_mode_tag
    from jarvise_assist.quick_config import effective_quick_values, get_quick_config

    cfg = get_quick_config(conn)
    eff = effective_quick_values(cfg)
    risk = float(eff["risk_used"])
    cap = float(cfg.values["quick_risk_cap_pct"])
    # Menu buttons still mark the stored (clamped-on-read) values.
    risk_stored = float(cfg.values["quick_risk_pct"])
    cap_stored = float(cfg.values["quick_risk_cap_pct"])
    main_cap = float(portfolio_risk_cap_pct(Decimal(str(ensure_settings(conn)["risk_pct"]))))
    total = main_cap + cap
    stats = summarize_quick(conn, until_ms=now_ms)

    week_until = main_week_loss_pause_until(conn, now_ms=now_ms)
    if week_until is not None:
        status = (
            f"⏸ ลงทะเบียนไม่ได้ — ขาดทุนสัปดาห์นี้เกิน 4R · ระบบหลัก{format_paused_until(week_until, now_ms=now_ms)}"
        )
    else:
        status = "พร้อม"

    lines = [
        "⚡ Quick (scalping) — แยกจากระบบหลัก",
        quick_mode_tag(),
        *_quick_live_lines(conn, now_ms=now_ms),
        f"สถานะ: {status}",
        (
            f"ความเสี่ยงต่อไม้ {_fmt_pct(risk)}% · "
            f"เพดาน quick {_fmt_pct(cap)}% (ข้อเสนอคิดรวมค่าธรรมเนียม · ไม้ที่เปิดพร้อมกันคิดจากระยะ stop) · "
            f"ความเสี่ยงรวมสูงสุด {_fmt_pct(total)}% "
            f"(หลัก {_fmt_pct(main_cap)}% + quick {_fmt_pct(cap)}%)"
        ),
        "ไม่มีล็อกรายวัน/แพ้ติดกันของ quick · หยุดเฉพาะเมื่อขาดทุนสัปดาห์เกิน 4R (กฎระบบหลัก)",
    ]
    n = int(stats["n"])
    if n == 0:
        lines.append("ยังไม่มีไม้ quick ที่ปิด")
    else:
        wr = float(stats["win_rate"]) * 100.0
        lines.append(
            f"n={n} · ชนะ {wr:.0f}% · รวม {float(stats['sum_r']):+.2f}R · เฉลี่ย {float(stats['mean_r']):+.2f}R"
        )
        aw = stats.get("avg_win_r")
        al = stats.get("avg_loss_r")
        aw_s = f"{aw:+.2f}R" if aw is not None else "—"
        al_s = f"{al:+.2f}R" if al is not None else "—"
        lines.append(
            f"ชนะเฉลี่ย {aw_s} · แพ้เฉลี่ย {al_s} · "
            f"fee เฉลี่ย {float(stats['mean_fee_r']):+.2f}R · "
            f"max DD {float(stats['max_dd_r']):.2f}R"
        )
        lines.append(
            f"วันนี้ {float(stats['today_r']):+.2f}R · streak {int(stats['streak'])} · "
            f"no-R {int(stats['no_r_n'])}"
        )
    if n < QUICK_SAMPLE_TARGET:
        lines.append(f"ยังไม่พิสูจน์ edge — ตัวอย่าง {n}/{QUICK_SAMPLE_TARGET} ไม้")
    lines.append(QUICK_USAGE)

    risk_row: list[dict[str, str]] = []
    for v in RISK_ALLOWED:
        fmt = _fmt_fixed(v)
        if abs(risk_stored - v) < 1e-9:
            risk_row.append({"text": f"✓ risk {fmt}%", "callback_data": f"qs:risk:{fmt}"})
        else:
            risk_row.append({"text": f"risk {fmt}%", "callback_data": f"qs:risk:{fmt}"})
    if _is_quick_setting_preset("risk", risk_stored):
        risk_row.append({"text": "✏️ risk", "callback_data": "qs:risk:m"})
    else:
        risk_row.append(
            {
                "text": f"✓ ✏️ risk {_fmt_fixed(risk_stored)}%",
                "callback_data": "qs:risk:m",
            }
        )

    cap_row: list[dict[str, str]] = []
    for v in CAP_ALLOWED:
        fmt = _fmt_fixed(v)
        if abs(cap_stored - v) < 1e-9:
            cap_row.append({"text": f"✓ cap {fmt}%", "callback_data": f"qs:cap:{fmt}"})
        else:
            cap_row.append({"text": f"cap {fmt}%", "callback_data": f"qs:cap:{fmt}"})
    if _is_quick_setting_preset("cap", cap_stored):
        cap_row.append({"text": "✏️ cap", "callback_data": "qs:cap:m"})
    else:
        cap_row.append(
            {
                "text": f"✓ ✏️ cap {_fmt_fixed(cap_stored)}%",
                "callback_data": "qs:cap:m",
            }
        )

    coin_kb = build_coin_menu_keyboard(conn)
    keyboard = {
        "inline_keyboard": [
            *coin_kb["inline_keyboard"],
            risk_row,
            cap_row,
            [{"text": "⚙️ ตั้งค่า quick", "callback_data": _cb("qc:h")}],
            [{"text": "✖️ ปิด", "callback_data": _cb("qk:mx")}],
        ]
    }
    return "\n".join(lines), keyboard


def handle_quick_callback(
    conn: Any,
    data: str,
    *,
    send: SendFn,
    now_ms: int,
) -> dict[str, Any]:
    """Handle qs:* callbacks. Never calls answer (listen does that)."""
    if data == "qs:no":
        write_quick_settings(conn, updated_at_ms=now_ms)
        send("ยกเลิก")
        return {"ok": True, "cancelled": True}

    matched_ok = _QUICK_OK_RE.match(data or "")
    if matched_ok is not None:
        key = matched_ok.group(1)
        raw = matched_ok.group(2)
        ts_s = int(matched_ok.group(3))
        now_s = int(now_ms) // 1000
        if now_s - ts_s > QUICK_CONFIRM_TTL_S or ts_s > now_s + QUICK_CONFIRM_FUTURE_S:
            if now_s - ts_s > QUICK_CONFIRM_TTL_S:
                send(e_confirm_stale_ttl())
            else:
                send(MSG_QUICK_CONFIRM_STALE)
            return {"ok": False, "rejected": True, "reason": "stale"}
        updated_at_ms = get_quick_settings(conn).get("updated_at_ms")
        # A write in the same unix second as the card does not make it stale (known limit).
        if updated_at_ms is not None and int(updated_at_ms) // 1000 > ts_s:
            send(MSG_QUICK_CONFIRM_STALE)
            return {"ok": False, "rejected": True, "reason": "stale"}
        return _apply_quick_setting(conn, key, raw, send=send, now_ms=now_ms, confirmed=True)

    if data.startswith("qs:ok:"):
        send(MSG_QUICK_CONFIRM_STALE)
        return {"ok": False, "rejected": True, "reason": "stale"}

    parts = data.split(":")
    if len(parts) == 3 and parts[0] == "qs":
        key, raw = parts[1], parts[2]
        return _apply_quick_setting(conn, key, raw, send=send, now_ms=now_ms, confirmed=False)
    send("คำสั่งไม่รู้จัก")
    return {"ok": False}


def _apply_quick_setting(
    conn: Any,
    key: str,
    raw: str,
    *,
    send: SendFn,
    now_ms: int,
    confirmed: bool,
    allow_trailing_pct: bool = False,
) -> dict[str, Any]:
    if key in ("dl", "st"):
        send(MSG_QUICK_LOCK_REMOVED)
        return {"ok": True}

    if raw == "m":
        if key not in ("risk", "cap"):
            send("คำสั่งไม่รู้จัก")
            return {"ok": False}
        if confirmed:
            send(e_setting_reject(raw))
            return {"ok": False, "rejected": True}
        send(MSG_QUICK_SETTING_HINT_RISK if key == "risk" else MSG_QUICK_SETTING_HINT_CAP)
        return {"ok": True, "hint": True}

    if key not in ("risk", "cap"):
        send("คำสั่งไม่รู้จัก")
        return {"ok": False}

    # Strip a trailing % for typed values; callbacks use bare numbers.
    token = raw.strip()
    if allow_trailing_pct and token.endswith("%"):
        token = token[:-1].strip()

    from jarvise_assist.quick_config import canonical_string, parse_value
    from jarvise_assist.quick_config_ui import apply_quick_config_change

    cfg_key = "quick_risk_pct" if key == "risk" else "quick_risk_cap_pct"
    parsed, err = parse_value(cfg_key, token)
    if parsed is None:
        # Junk / ≤0 / >100 → legacy reject; (0,100] outside new bounds → bounds Thai.
        if not _QUICK_SETTING_VALUE_RE.fullmatch(token):
            send(e_setting_reject(raw))
            return {"ok": False, "rejected": True}
        try:
            d_raw = Decimal(token)
            d_q = d_raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            send(e_setting_reject(raw))
            return {"ok": False, "rejected": True}
        if d_raw <= 0 or d_q > 100:
            send(e_setting_reject(raw))
            return {"ok": False, "rejected": True}
        send(err or e_setting_reject(raw))
        return {"ok": False, "rejected": True}

    # Stale/TTL for qs:ok is checked in handle_quick_callback before this call.
    return apply_quick_config_change(
        conn,
        key,
        canonical_string(cfg_key, parsed),
        send=send,
        now_ms=now_ms,
        confirmed=confirmed,
        source="qs:callback" if confirmed else "qs:apply",
        confirm_ts=None,
    )
