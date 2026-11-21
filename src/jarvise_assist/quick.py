"""Quick (scalping) assist: settings, registration, fill tagging, report.

No orders. Binance access is read-only GET and only when a registration or
open quick trade exists. Never writes assist_settings or assist_edge.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from jarvise_assist.futures import FuturesPosition
from jarvise_assist.messages import fmt_price, fmt_qty
from jarvise_assist.order_lines import format_closing_order_line
from jarvise_assist.pause import bangkok_day_start_ms, format_paused_until, main_week_loss_pause_until
from jarvise_assist.real_trades import _fee_usdt_and_other, fetch_all_user_trades, fetch_income_range
from jarvise_assist.sizing import portfolio_risk_cap_pct
from jarvise_assist.store import ensure_settings

logger = logging.getLogger(__name__)
_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")

SendFn = Callable[..., Any]

REG_TTL_MS = 10 * 60_000
REG_EXPIRE_GRACE_MS = 2 * 60_000
QUICK_SAMPLE_TARGET = 25

QUICK_USAGE = "พิมพ์: q BTC long 67000 (stop เป็นราคา) หรือ q BTC short 0.4% (stop ห่างจากราคา %)"

RISK_ALLOWED = (0.25, 0.5, 0.75, 1.0)
CAP_ALLOWED = (0.5, 1.0, 1.5, 2.0)
RISK_CONFIRM_AT = 1.0
CAP_CONFIRM_ABOVE = 1.0

MSG_QUICK_LOCK_REMOVED = "ตั้งค่านี้ถูกยกเลิกแล้ว — quick ไม่มีล็อกรายวัน/แพ้ติดกัน (ยังหยุดเมื่อขาดทุนสัปดาห์เกิน 4R)"


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
            created_at_ms, expires_at_ms, status, trade_id, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?)
        """,
        (rid, sym, side_l, entry, stop, float(qty), int(now_ms), expires, int(now_ms)),
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

    info: dict[str, Any] = {"tagged": 0, "expired": 0, "updated": 0}

    tagged_trade_ids: set[str] = set()
    for reg in open_regs:
        rid = str(reg["id"])
        sym = str(reg["symbol"]).upper()
        side = str(reg["side"])
        created = int(reg["created_at_ms"])
        expires = int(reg["expires_at_ms"])
        fills = fetch_all_user_trades(client, sym, start_ms=created, end_ms=int(now_ms) + 1)
        # First fill after created decides: wrong side → expire; opening → tag or grace.
        after = [f for f in fills if int(f.get("time") or 0) >= created]
        after.sort(key=lambda r: (int(r.get("time") or 0), int(r.get("id") or 0)))

        if not after:
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

        if first_time > expires:
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
        _send_tag_notice(conn, send, registration_id=rid, client=client, now_ms=now_ms)
        if str(rebuilt.get("status")) == "closed":
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
        lines = [
            (f"🏁 ไม้ quick {sym} {label} ปิดแล้ว (ปิดใน Binance) — สุทธิ {net_usdt:+.2f} USDT · {r_line}"),
        ]
        from jarvise_assist.order_lines import is_symbol_protective_leftover

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

    settings = get_quick_settings(conn)
    risk = float(settings["risk_pct"])
    cap = float(settings["cap_pct"])
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
            f"เพดานไม้ quick ที่เปิดพร้อมกัน {_fmt_pct(cap)}% · "
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

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🟢 LONG", "callback_data": "qk:sd:l"},
                {"text": "🔴 SHORT", "callback_data": "qk:sd:s"},
            ],
            [
                {"text": f"risk {_fmt_pct(v)}%", "callback_data": f"qs:risk:{_fmt_pct(v)}"}
                if abs(risk - v) >= 1e-9
                else {"text": f"✓ risk {_fmt_pct(v)}%", "callback_data": f"qs:risk:{_fmt_pct(v)}"}
                for v in RISK_ALLOWED
            ],
            [
                {"text": f"cap {_fmt_pct(v)}%", "callback_data": f"qs:cap:{_fmt_pct(v)}"}
                if abs(cap - v) >= 1e-9
                else {"text": f"✓ cap {_fmt_pct(v)}%", "callback_data": f"qs:cap:{_fmt_pct(v)}"}
                for v in CAP_ALLOWED
            ],
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
        send("ยกเลิก")
        return {"ok": True, "cancelled": True}

    parts = data.split(":")
    # qs:key:v or qs:ok:key:v
    if len(parts) == 4 and parts[0] == "qs" and parts[1] == "ok":
        key, raw = parts[2], parts[3]
        return _apply_quick_setting(conn, key, raw, send=send, now_ms=now_ms, confirmed=True)
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
) -> dict[str, Any]:
    try:
        val = float(raw)
    except ValueError:
        send("ค่าไม่ถูกต้อง")
        return {"ok": False, "rejected": True}

    if key == "risk":
        if val not in RISK_ALLOWED:
            send("รับได้แค่ 0.25 / 0.5 / 0.75 / 1")
            return {"ok": False, "rejected": True}
        if val >= RISK_CONFIRM_AT and not confirmed:
            send(
                f"ยืนยันความเสี่ยงต่อไม้ {_fmt_pct(val)}%?",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "✅ ยืนยัน", "callback_data": f"qs:ok:risk:{_fmt_pct(val)}"},
                            {"text": "↩️ ไม่เปลี่ยน", "callback_data": "qs:no"},
                        ]
                    ]
                },
            )
            return {"ok": True, "confirm": True}
        write_quick_settings(conn, risk_pct=val, updated_at_ms=now_ms)
        text, kb = render_quick_menu(conn, now_ms=now_ms)
        send(f"บันทึกความเสี่ยง {_fmt_pct(val)}% แล้ว\n{text}", reply_markup=kb)
        return {"ok": True, "risk_pct": val}

    if key == "cap":
        if val not in CAP_ALLOWED:
            send("รับได้แค่ 0.5 / 1 / 1.5 / 2")
            return {"ok": False, "rejected": True}
        if val > CAP_CONFIRM_ABOVE and not confirmed:
            main_cap = float(portfolio_risk_cap_pct(Decimal(str(ensure_settings(conn)["risk_pct"]))))
            total = main_cap + val
            send(
                f"ยืนยันเพดาน {_fmt_pct(val)}%? "
                f"ความเสี่ยงรวมสูงสุด {_fmt_pct(total)}% "
                f"(หลัก {_fmt_pct(main_cap)}% + quick {_fmt_pct(val)}%)",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "✅ ยืนยัน", "callback_data": f"qs:ok:cap:{_fmt_pct(val)}"},
                            {"text": "↩️ ไม่เปลี่ยน", "callback_data": "qs:no"},
                        ]
                    ]
                },
            )
            return {"ok": True, "confirm": True}
        write_quick_settings(conn, cap_pct=val, updated_at_ms=now_ms)
        text, kb = render_quick_menu(conn, now_ms=now_ms)
        send(f"บันทึกเพดาน {_fmt_pct(val)}% แล้ว\n{text}", reply_markup=kb)
        return {"ok": True, "cap_pct": val}

    if key in ("dl", "st"):
        send(MSG_QUICK_LOCK_REMOVED)
        return {"ok": True}

    send("คำสั่งไม่รู้จัก")
    return {"ok": False}
