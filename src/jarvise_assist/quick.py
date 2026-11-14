"""Quick (scalping) assist: settings, registration, fill tagging, lock, report.

No orders. Binance access is read-only GET and only when a registration or
open quick trade exists. Never writes assist_settings or assist_edge.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from jarvise_assist.pause import bangkok_day_id, bangkok_day_start_ms
from jarvise_assist.real_trades import _fee_usdt_and_other, fetch_all_user_trades, fetch_income_range
from jarvise_assist.sizing import MAX_PORTFOLIO_RISK_PCT

SendFn = Callable[..., Any]

REG_TTL_MS = 10 * 60_000
REG_EXPIRE_GRACE_MS = 2 * 60_000

RISK_ALLOWED = (0.25, 0.5, 0.75, 1.0)
CAP_ALLOWED = (0.5, 1.0, 1.5, 2.0)
DAY_LOSS_ALLOWED = (1.0, 1.5, 2.0)
STREAK_ALLOWED = (2, 3)
RISK_CONFIRM_AT = 1.0
CAP_CONFIRM_ABOVE = 1.0

LOCK_STREAK_MSG = "🔒 Quick ล็อกถึง 00:00 — แพ้ติดกัน {n} ไม้ · ระบบหลักทำงานตามปกติ"
LOCK_DAY_MSG = "🔒 Quick ล็อกถึง 00:00 — ขาดทุนวันนี้ {net:+.2f}R (เพดาน −{dl:g}R) · ระบบหลักทำงานตามปกติ"


def get_quick_settings(conn: Any) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM assist_quick_settings WHERE id=1").fetchone()
    if row is None:
        conn.execute(
            """
            INSERT OR IGNORE INTO assist_quick_settings
            (id, risk_pct, day_max_loss_r, streak_lock_n, cap_pct)
            VALUES (1, 0.5, 2, 3, 1.0)
            """
        )
        conn.commit()
        row = conn.execute("SELECT * FROM assist_quick_settings WHERE id=1").fetchone()
    return dict(row)


def write_quick_settings(conn: Any, **fields: Any) -> dict[str, Any]:
    allowed = {
        "risk_pct",
        "day_max_loss_r",
        "streak_lock_n",
        "cap_pct",
        "locked_until_ms",
        "lock_reason",
        "lock_day_id",
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


def _is_locked(settings: dict[str, Any], *, now_ms: int) -> bool:
    until = settings.get("locked_until_ms")
    return until is not None and int(until) > int(now_ms)


def _clear_lock_if_expired(conn: Any, *, now_ms: int) -> dict[str, Any]:
    settings = get_quick_settings(conn)
    until = settings.get("locked_until_ms")
    if until is not None and int(until) <= int(now_ms):
        return write_quick_settings(
            conn,
            locked_until_ms=None,
            lock_reason=None,
            lock_day_id=None,
            updated_at_ms=now_ms,
        )
    return settings


def create_quick_registration(
    conn: Any,
    *,
    symbol: str,
    side: str,
    planned_entry: float,
    planned_stop: float,
    qty: float,
    now_ms: int,
) -> tuple[str | None, str | None]:
    """Create an open registration. Returns (id, None) or (None, reason_code)."""
    settings = _clear_lock_if_expired(conn, now_ms=now_ms)
    if _is_locked(settings, now_ms=now_ms):
        return None, "locked"

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


def _next_midnight_bangkok_ms(now_ms: int) -> int:
    return bangkok_day_start_ms(now_ms) + 86_400_000


def _evaluate_quick_lock(conn: Any, *, now_ms: int, send: SendFn) -> dict[str, Any]:
    settings = _clear_lock_if_expired(conn, now_ms=now_ms)
    if _is_locked(settings, now_ms=now_ms):
        return {"locked": True, "reason": settings.get("lock_reason")}

    day_start = bangkok_day_start_ms(now_ms)
    day_id = bangkok_day_id(now_ms)
    streak_n = int(settings.get("streak_lock_n") or 3)
    day_max = float(settings.get("day_max_loss_r") or 2)

    closed = conn.execute(
        """
        SELECT net_usdt, net_r, closed_at_ms
        FROM assist_quick_trade
        WHERE status='closed' AND closed_at_ms IS NOT NULL
        ORDER BY closed_at_ms ASC, id ASC
        """
    ).fetchall()

    # Streak: trailing losses closed today (Bangkok). A win resets. Next day starts at 0.
    today_closed = [
        row for row in closed if row["closed_at_ms"] is not None and int(row["closed_at_ms"]) >= day_start
    ]
    streak = 0
    for row in reversed(today_closed):
        net = row["net_usdt"]
        if net is None:
            continue
        if float(net) < 0:
            streak += 1
        else:
            break

    if streak >= streak_n:
        until = _next_midnight_bangkok_ms(now_ms)
        write_quick_settings(
            conn,
            locked_until_ms=until,
            lock_reason="streak",
            lock_day_id=day_id,
            updated_at_ms=now_ms,
        )
        send(LOCK_STREAK_MSG.format(n=streak_n))
        return {"locked": True, "reason": "streak"}

    # Day net R (R-eligible only)
    day_r = 0.0
    for row in closed:
        if row["closed_at_ms"] is None:
            continue
        if int(row["closed_at_ms"]) < day_start or int(row["closed_at_ms"]) >= int(now_ms):
            continue
        if row["net_r"] is None:
            continue
        day_r += float(row["net_r"])

    if day_r <= -day_max:
        until = _next_midnight_bangkok_ms(now_ms)
        write_quick_settings(
            conn,
            locked_until_ms=until,
            lock_reason="day_loss",
            lock_day_id=day_id,
            updated_at_ms=now_ms,
        )
        send(LOCK_DAY_MSG.format(net=day_r, dl=day_max))
        return {"locked": True, "reason": "day_loss"}

    return {"locked": False}


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
        # No Binance calls. Still evaluate the quick lock on closed trades.
        was_locked = _is_locked(_clear_lock_if_expired(conn, now_ms=now_ms), now_ms=now_ms)
        lock_info = _evaluate_quick_lock(conn, now_ms=now_ms, send=send)
        if lock_info.get("locked") and not was_locked:
            return {"lock": lock_info}
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
            continue

        trade_id = f"aqt_{uuid.uuid4().hex[:16]}"
        # Reuse existing trade id if somehow present
        existing = conn.execute(
            "SELECT id FROM assist_quick_trade WHERE registration_id=?", (rid,)
        ).fetchone()
        if existing is not None:
            trade_id = str(existing["id"])
        _rebuild_open_trade(
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

    # Rebuild already-open trades (skip ones tagged earlier in this run)
    open_trades = conn.execute("SELECT * FROM assist_quick_trade WHERE status='open'").fetchall()
    for trade in open_trades:
        trade_id = str(trade["id"])
        if trade_id in tagged_trade_ids:
            continue
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
        _rebuild_open_trade(
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

    lock_info = _evaluate_quick_lock(conn, now_ms=now_ms, send=send)
    if lock_info.get("locked"):
        info["lock"] = lock_info
    return {k: v for k, v in info.items() if v}


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


def render_quick_menu(conn: Any, *, now_ms: int) -> tuple[str, dict[str, Any]]:
    settings = _clear_lock_if_expired(conn, now_ms=now_ms)
    risk = float(settings["risk_pct"])
    cap = float(settings["cap_pct"])
    dl = float(settings["day_max_loss_r"])
    st = int(settings["streak_lock_n"])
    total = float(MAX_PORTFOLIO_RISK_PCT) + cap
    stats = summarize_quick(conn, until_ms=now_ms)

    if _is_locked(settings, now_ms=now_ms):
        reason = str(settings.get("lock_reason") or "")
        reason_th = "แพ้ติดกัน" if reason == "streak" else "ขาดทุนวันนี้"
        status = f"🔒 ล็อกถึง 00:00 ({reason_th})"
    else:
        status = "พร้อม"

    lines = [
        "⚡ Quick (scalping) — แยกจากระบบหลัก",
        (
            f"ความเสี่ยงต่อไม้ {_fmt_pct(risk)}% · "
            f"เพดานไม้ quick ที่เปิดพร้อมกัน {_fmt_pct(cap)}% · "
            f"ความเสี่ยงรวมสูงสุด {_fmt_pct(total)}%"
        ),
        f"ขาดทุนสูงสุดต่อวัน {_fmt_pct(dl)}R · แพ้ติดกัน {st} ไม้ = ล็อกถึง 00:00",
        f"สถานะ: {status}",
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
    if n < 20:
        lines.append(f"ยังไม่พิสูจน์ edge — ตัวอย่าง {n}/20 ไม้")

    def _mark(cur: float, options: tuple[float, ...], prefix: str) -> list[dict[str, str]]:
        row: list[dict[str, str]] = []
        for v in options:
            bare = _fmt_pct(v)
            label = f"✓ {bare}R" if abs(cur - v) < 1e-9 else f"{bare}R"
            row.append({"text": label, "callback_data": f"qs:{prefix}:{_fmt_pct(v)}"})
        return row

    def _mark_st(cur: int) -> list[dict[str, str]]:
        row = []
        for v in STREAK_ALLOWED:
            label = f"✓ {v}" if cur == v else str(v)
            row.append({"text": label, "callback_data": f"qs:st:{v}"})
        return row

    keyboard = {
        "inline_keyboard": [
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
            _mark(dl, DAY_LOSS_ALLOWED, "dl"),
            _mark_st(st),
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
                            {"text": "Yes", "callback_data": f"qs:ok:risk:{_fmt_pct(val)}"},
                            {"text": "No", "callback_data": "qs:no"},
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
            total = float(MAX_PORTFOLIO_RISK_PCT) + val
            send(
                f"ยืนยันเพดาน {_fmt_pct(val)}%? ความเสี่ยงรวมสูงสุด {_fmt_pct(total)}%",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "Yes", "callback_data": f"qs:ok:cap:{_fmt_pct(val)}"},
                            {"text": "No", "callback_data": "qs:no"},
                        ]
                    ]
                },
            )
            return {"ok": True, "confirm": True}
        write_quick_settings(conn, cap_pct=val, updated_at_ms=now_ms)
        text, kb = render_quick_menu(conn, now_ms=now_ms)
        send(f"บันทึกเพดาน {_fmt_pct(val)}% แล้ว\n{text}", reply_markup=kb)
        return {"ok": True, "cap_pct": val}

    if key == "dl":
        if val not in DAY_LOSS_ALLOWED:
            send("รับได้แค่ 1 / 1.5 / 2 R")
            return {"ok": False, "rejected": True}
        write_quick_settings(conn, day_max_loss_r=val, updated_at_ms=now_ms)
        text, kb = render_quick_menu(conn, now_ms=now_ms)
        send(f"บันทึกขาดทุนสูงสุดต่อวัน {_fmt_pct(val)}R แล้ว\n{text}", reply_markup=kb)
        return {"ok": True, "day_max_loss_r": val}

    if key == "st":
        ival = int(val) if val == int(val) else -1
        if ival not in STREAK_ALLOWED:
            send("รับได้แค่ 2 / 3")
            return {"ok": False, "rejected": True}
        write_quick_settings(conn, streak_lock_n=ival, updated_at_ms=now_ms)
        text, kb = render_quick_menu(conn, now_ms=now_ms)
        send(f"บันทึกแพ้ติดกัน {ival} ไม้ แล้ว\n{text}", reply_markup=kb)
        return {"ok": True, "streak_lock_n": ival}

    send("คำสั่งไม่รู้จัก")
    return {"ok": False}
