"""Owner grade card for a closed assist trade. Reads real trades. Sends no orders."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from jarvise_assist.messages import fmt_price
from jarvise_assist.real_trades import format_bkk_window, sync_real_trades
from jarvise_assist.signals import is_night_hold
from jarvise_assist.store import ensure_settings, get_edge, now_ms
from jarvise_notify.telegram import send_message

logger = logging.getLogger(__name__)

# Deploy job "deploy" completed_at for PR #293, run 42181311569, head 148ca1a.
# https://github.com/chatoe-norm/jarvise/actions/runs/42181311569
JOURNAL_SHIPPED_MS = 1_793_950_811_000  # 2026-11-06T07:40:11Z

MISTAKE_TAGS: tuple[str, ...] = (
    "chase",
    "early_exit",
    "moved_stop_away",
    "no_stop",
    "over_2_positions",
    "after_3_losses",
    "other",
)
TAG_LABELS: dict[str, str] = {
    "chase": "ไล่ราคา",
    "early_exit": "ออกก่อน",
    "moved_stop_away": "ขยับ stop ออก",
    "no_stop": "ไม่มี stop",
    "over_2_positions": "เกิน 2 ไม้",
    "after_3_losses": "หลังแพ้ 3",
    "other": "อื่นๆ",
}
CHECK_LABELS: tuple[tuple[str, str], ...] = (
    ("open_positions_ok", "เปิดไม่เกิน 2 ไม้"),
    ("signal_present", "เข้าจากสัญญาณบอท"),
    ("stop_existed", "มี stop"),
    ("moves_tightened", "เลื่อน stop เฉพาะทางแน่น"),
    ("exit_followed_plan", "ปิดตามแผน"),
    ("not_in_streak_pause", "ไม่เข้าตอนพักแพ้ติด"),
)
EXIT_LABELS: dict[str, str] = {
    "stop": "stop",
    "tp": "tp",
    "owner_exit": "ปิดตามปุ่ม",
    "emergency": "ปิดฉุกเฉิน",
    "manual": "มือ",
    "unknown": "ไม่ทราบชนิด",
}
PLAN_EXIT_KINDS = frozenset({"stop", "tp"})
ADVICE_EXIT_KINDS = frozenset({"owner_exit", "emergency"})
NOTE_MAX = 500
RETRY_MS = 6 * 60 * 60 * 1000
SYNC_PAD_MS = 60 * 60 * 1000
SYNC_GAP_MS = 30 * 60 * 1000
SYNC_MAX_ATTEMPTS = 4
MATCH_PAD_MS = 60_000

SendFn = Callable[..., Any]
SyncFn = Callable[..., Any]
EditFn = Callable[..., Any]


def net_r_of(trade: dict[str, Any]) -> float | None:
    """Stored net R. The denominator is the original loss_at_stop, already applied."""
    raw = trade.get("net_r_real")
    if raw is None:
        return None
    return float(raw)


def locked_r(
    *,
    side: str,
    entry: float,
    stop: float,
    qty: float,
    loss_at_stop: float | None,
) -> float | None:
    """USDT locked by this stop, divided by the original 1R. A move does not rescale R."""
    if loss_at_stop is None or float(loss_at_stop) == 0:
        return None
    if side == "long":
        locked = (stop - entry) * qty
    elif side == "short":
        locked = (entry - stop) * qty
    else:
        return None
    return locked / float(loss_at_stop)


def stops_only_tightened(side: str, stops: list[float]) -> bool:
    """True when each later stop is tighter than the one before it."""
    if side not in {"long", "short"}:
        return False
    for prev, nxt in zip(stops, stops[1:], strict=False):
        if side == "long" and not nxt > prev:
            return False
        if side == "short" and not nxt < prev:
            return False
    return True


def format_journal_card(
    conn: Any,
    trade: dict[str, Any],
    checks: dict[str, bool],
) -> str:
    """Thai draft card. Numbers are read from assist_real_trade, not stored here."""
    shown = dict(trade)
    if not isinstance(shown.get("_moves"), list):
        shown["_moves"] = _done_move_stops(conn, shown)
    symbol = str(shown.get("symbol") or "")
    side = str(shown.get("side") or "").lower()
    emoji = "🔺" if side == "long" else "🔻"
    label = "LONG" if side == "long" else "SHORT"
    entry = _fmt_px(shown.get("entry_fill_avg"))
    exit_px = _fmt_px(shown.get("exit_fill_avg"))
    loss = shown.get("loss_at_stop")
    loss_line = "ไม่ทราบ" if loss is None else f"{float(loss):.2f} USDT"
    net = net_r_of(shown) if loss is not None else None
    net_line = "ไม่ทราบ" if net is None else f"{_signed(net)}R"
    pnl = _net_usdt(shown)
    pnl_line = "ไม่ทราบ" if pnl is None else f"{_signed(pnl)} USDT"
    lines = [
        "📘 บันทึกไม้",
        f"{emoji} {symbol} · {label}",
        f"เข้า {entry} → ออก {exit_px}",
        f"1R = {loss_line}",
        f"ผล {net_line}",
        f"ผลเป็นเงิน {pnl_line}",
        _exit_line(conn, shown),
        _timeline_block(shown),
        _thesis_line(conn, shown),
        "ตรวจอัตโนมัติ:",
    ]
    for key, title in CHECK_LABELS:
        flag = "ผ่าน" if checks.get(key) else "ไม่ผ่าน"
        lines.append(f"· {title}: {flag}")
    return "\n".join(lines)


def grade_keyboard(entry_order_id: str) -> dict[str, Any]:
    oid = str(entry_order_id)
    return {
        "inline_keyboard": [
            [
                {"text": "✅ ตามแผน", "callback_data": f"jp:{oid}"},
                {"text": "❌ ผิดแผน", "callback_data": f"jf:{oid}"},
            ]
        ]
    }


def tag_keyboard(entry_order_id: str, selected: set[str]) -> dict[str, Any]:
    oid = str(entry_order_id)
    rows: list[list[dict[str, str]]] = []
    for tag in MISTAKE_TAGS:
        mark = "✅ " if tag in selected else ""
        rows.append([{"text": f"{mark}{TAG_LABELS[tag]}", "callback_data": f"jt:{oid}:{tag}"}])
    rows.append([{"text": "เสร็จ", "callback_data": f"jd:{oid}"}])
    return {"inline_keyboard": rows}


def collect_auto_checks(conn: Any, trade: dict[str, Any]) -> dict[str, bool]:
    """Freeze the six checks at insert time."""
    entry_ms = _int_or_none(trade.get("entry_time_ms"))
    side = str(trade.get("side") or "").lower()
    open_n = _open_positions_at(conn, entry_ms)
    signal_ok = _signal_present(conn, trade)
    stop_ok = _stop_existed(conn, trade)
    move_stops = _done_move_stops(conn, trade)
    tightened = _moves_tightened(side, trade.get("planned_stop"), move_stops)
    exit_ok = _exit_followed_plan(conn, trade)
    pause_ok = _not_in_streak_pause(conn, entry_ms)
    return {
        "open_positions_ok": open_n <= 2,
        "signal_present": signal_ok,
        "stop_existed": stop_ok,
        "moves_tightened": tightened,
        "exit_followed_plan": exit_ok,
        "not_in_streak_pause": pause_ok,
    }


def journal_on_close(
    conn: Any,
    closed: list[tuple[str, str]],
    *,
    futures: Any = None,
    now_ms_value: int | None = None,
    send: SendFn | None = None,
    sync_fn: SyncFn | None = None,
) -> dict[str, Any]:
    """Sync the close window, insert missing closed rows, send each card once.

    Exceptions stay inside this function. Watch advice, pause, and the live
    orphan cancel already ran. Night hold skips the send. A close is synced at
    most once per 30 minutes, four times, then given up.
    """
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    send_fn = send or send_message
    sync = sync_fn or sync_real_trades
    sent = _send_pending(conn, send_fn, now_ms=ts)
    due = _due_sync_closes(conn, closed, ts)
    synced = False
    sync_error: str | None = None
    inserted: list[str] = []
    if due:
        since = _since_ms(due)
        try:
            sync(conn, futures, since_ms=since, until_ms=ts)
            synced = True
        except Exception as exc:  # noqa: BLE001 — next allowed poll retries; watch must continue
            sync_error = type(exc).__name__
            logger.exception("assist journal sync failed")
        else:
            inserted = _insert_missing(conn, since_ms=since, now_ms=ts)
            sent.extend(_send_pending(conn, send_fn, now_ms=ts))
        _record_sync_attempts(conn, due, ts)
    return {
        "ok": True,
        "synced": synced,
        "sync_error": sync_error,
        "inserted": inserted,
        "sent": sent,
    }


def handle_journal_callback(
    conn: Any,
    data: str,
    *,
    edit: EditFn | None = None,
    now_ms_value: int | None = None,
    message: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one grade, tag toggle, or done tap. Owner chat is checked by the listener."""
    ts = int(now_ms_value if now_ms_value is not None else now_ms())
    kind, oid, tag = _parse_callback(data)
    if kind is None or oid is None:
        return {"ok": False, "toast": "ไม่พบรายการ"}
    row = _journal_row(conn, oid)
    if row is None:
        return {"ok": False, "toast": "ไม่พบรายการ"}
    msg = message or {}
    mid = msg.get("message_id")
    if row.get("card_message_id") is None and mid is not None:
        _set_message_id(conn, oid, int(mid))
        row["card_message_id"] = int(mid)
    selected = _parse_tags(row.get("mistake_tags"))
    grade = row.get("grade")
    toast = "บันทึกแล้ว"
    if kind == "pass":
        grade = "pass"
        selected = set()
        _write_grade(conn, oid, grade="pass", tags=None, graded_at_ms=ts)
        toast = "บันทึกว่าตามแผน"
    elif kind == "fail":
        grade = "fail"
        _write_grade(conn, oid, grade="fail", tags=_join_tags(selected), graded_at_ms=ts)
        toast = "เลือกจุดที่ผิด"
    elif kind == "tag":
        if grade != "fail" or tag not in MISTAKE_TAGS:
            return {"ok": False, "toast": "เลือกผิดแผนก่อน"}
        if tag in selected:
            selected.remove(tag)
        else:
            selected.add(tag)
        _write_grade(conn, oid, grade="fail", tags=_join_tags(selected), graded_at_ms=ts)
        toast = "อัปเดตแล้ว"
    elif kind == "done":
        toast = "บันทึกแล้ว"
    else:
        return {"ok": False, "toast": "ไม่พบรายการ"}
    _edit_card(conn, edit, oid, grade=grade, selected=selected, message=msg)
    return {"ok": True, "grade": grade, "tags": _join_tags(selected), "toast": toast}


def store_journal_note(
    conn: Any,
    *,
    message_id: int,
    text: str,
    now_ms_value: int | None = None,
) -> bool:
    """Append a reply to the card. Returns False when the message is not a journal card."""
    piece = text.strip()
    if not piece:
        return False
    row = conn.execute(
        "SELECT entry_order_id, note FROM assist_journal WHERE card_message_id=?",
        (int(message_id),),
    ).fetchone()
    if row is None:
        return False
    oid = str(row["entry_order_id"] if hasattr(row, "keys") else row[0])
    existing = row["note"] if hasattr(row, "keys") else row[1]
    prior = str(existing or "").strip()
    joined = piece if not prior else f"{prior}\n{piece}"
    if len(joined) > NOTE_MAX:
        joined = joined[-NOTE_MAX:]
    conn.execute(
        "UPDATE assist_journal SET note=? WHERE entry_order_id=?",
        (joined, oid),
    )
    conn.commit()
    return True


def summarize_journal(
    conn: Any,
    *,
    since_ms: int,
    until_ms: int,
) -> dict[str, Any]:
    """Weekly journal block. Rows with no loss_at_stop are out of the R math."""
    rows = conn.execute(
        """
        SELECT j.grade, j.mistake_tags, r.loss_at_stop, r.net_r_real, r.exit_time_ms,
               r.entry_time_ms
        FROM assist_journal j
        JOIN assist_real_trade r ON r.entry_order_id = j.entry_order_id
        WHERE r.status = 'closed' AND r.side IN ('long', 'short')
          AND (r.exit_time_ms IS NULL OR r.exit_time_ms >= ?)
          AND ((
            r.exit_time_ms IS NOT NULL AND r.exit_time_ms >= ? AND r.exit_time_ms <= ?
        ) OR (
            r.exit_time_ms IS NULL AND r.entry_time_ms >= ? AND r.entry_time_ms <= ?
        ))
        """,
        (
            int(JOURNAL_SHIPPED_MS),
            int(since_ms),
            int(until_ms),
            int(since_ms),
            int(until_ms),
        ),
    ).fetchall()
    pre_journal_n = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM assist_real_trade
            WHERE status = 'closed' AND side IN ('long', 'short')
              AND exit_time_ms IS NOT NULL
              AND exit_time_ms < ?
              AND exit_time_ms >= ? AND exit_time_ms <= ?
            """,
            (int(JOURNAL_SHIPPED_MS), int(since_ms), int(until_ms)),
        ).fetchone()[0]
    )
    known: list[float] = []
    pass_rs: list[float] = []
    fail_rs: list[float] = []
    pass_n = 0
    fail_n = 0
    ungraded = 0
    tag_counts: dict[str, int] = {tag: 0 for tag in MISTAKE_TAGS}
    for raw in rows:
        item = (
            dict(raw)
            if hasattr(raw, "keys")
            else {
                "grade": raw[0],
                "mistake_tags": raw[1],
                "loss_at_stop": raw[2],
                "net_r_real": raw[3],
                "exit_time_ms": raw[4],
            }
        )
        exit_ms = item.get("exit_time_ms")
        if exit_ms is not None and int(exit_ms) < JOURNAL_SHIPPED_MS:
            continue
        grade = item.get("grade")
        if grade == "pass":
            pass_n += 1
        elif grade == "fail":
            fail_n += 1
            for tag in _parse_tags(item.get("mistake_tags")):
                if tag in tag_counts:
                    tag_counts[tag] += 1
        else:
            ungraded += 1
        if item.get("loss_at_stop") is None or item.get("net_r_real") is None:
            continue
        net = float(item["net_r_real"])
        known.append(net)
        if grade == "pass":
            pass_rs.append(net)
        elif grade == "fail":
            fail_rs.append(net)
    graded = pass_n + fail_n
    top = _top_tag(tag_counts)
    return {
        "n": len(rows),
        "known_n": len(known),
        "sum_r": sum(known) if known else None,
        "avg_r": (sum(known) / len(known)) if known else None,
        "pass_n": pass_n,
        "fail_n": fail_n,
        "pass_rate": (pass_n / graded) if graded else None,
        "avg_r_pass": (sum(pass_rs) / len(pass_rs)) if pass_rs else None,
        "avg_r_fail": (sum(fail_rs) / len(fail_rs)) if fail_rs else None,
        "top_tag": top,
        "ungraded": ungraded,
        "pre_journal_n": pre_journal_n,
        "window_start_ms": int(since_ms),
        "window_end_ms": int(until_ms),
    }


def format_journal_lines(summary: dict[str, Any]) -> list[str]:
    n = int(summary.get("n") or 0)
    pre = int(summary.get("pre_journal_n") or 0)
    pre_line = None
    if pre:
        start = summary.get("window_start_ms")
        end = summary.get("window_end_ms")
        if start is not None and end is not None:
            window = format_bkk_window(int(start), int(end))
            pre_line = f"ก่อนมีบันทึก {window}: n={pre}"
        else:
            pre_line = f"ก่อนมีบันทึก: n={pre}"
    if n == 0 and pre_line is None:
        return ["บันทึกไม้: ยังไม่มีที่ปิด"]
    if n == 0 and pre_line is not None:
        return [pre_line]
    sum_r = summary.get("sum_r")
    avg_r = summary.get("avg_r")
    sum_txt = "—" if sum_r is None else f"{_signed(float(sum_r))}R"
    avg_txt = "—" if avg_r is None else f"{_signed(float(avg_r))}R"
    rate = summary.get("pass_rate")
    rate_txt = "—" if rate is None else f"{float(rate) * 100:.0f}%"
    top = summary.get("top_tag") or "—"
    lines = [
        f"บันทึกไม้: n={n} · รวม {sum_txt} · เฉลี่ย {avg_txt}",
        (
            f"ตามแผน {rate_txt} · ตามแผนเฉลี่ย {_r_or_dash(summary.get('avg_r_pass'))} · "
            f"ผิดแผนเฉลี่ย {_r_or_dash(summary.get('avg_r_fail'))}"
        ),
        f"ผิดบ่อยสุด: {top} · ยังไม่ให้เกรด {int(summary.get('ungraded') or 0)}",
    ]
    if pre_line is not None:
        lines.append(pre_line)
    return lines


def _send_pending(conn: Any, send: SendFn, *, now_ms: int) -> list[str]:
    if is_night_hold(now_ms=int(now_ms)):
        return []
    rows = conn.execute(
        """
        SELECT j.entry_order_id
        FROM assist_journal j
        JOIN assist_real_trade r ON r.entry_order_id = j.entry_order_id
        WHERE j.card_message_id IS NULL
          AND r.status = 'closed' AND r.side IN ('long', 'short')
        ORDER BY j.created_at_ms
        """
    ).fetchall()
    sent: list[str] = []
    for raw in rows:
        oid = str(raw["entry_order_id"] if hasattr(raw, "keys") else raw[0])
        if _send_one(conn, oid, send):
            sent.append(oid)
    return sent


def _send_one(conn: Any, entry_order_id: str, send: SendFn) -> bool:
    trade = _trade_row(conn, entry_order_id)
    journal = _journal_row(conn, entry_order_id)
    if trade is None or journal is None or journal.get("card_message_id") is not None:
        return False
    try:
        checks = json.loads(str(journal.get("auto_checks_json") or "{}"))
    except json.JSONDecodeError:
        checks = {}
    text = format_journal_card(conn, trade, checks if isinstance(checks, dict) else {})
    try:
        mid = send(text, reply_markup=grade_keyboard(entry_order_id))
    except Exception:  # noqa: BLE001 — a dead send must not stop the watch loop
        logger.exception("assist journal send failed for %s", entry_order_id)
        return False
    if mid is None:
        return False
    _set_message_id(conn, entry_order_id, int(mid))
    return True


def _insert_missing(conn: Any, *, since_ms: int, now_ms: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT entry_order_id, intent_id, signal_id, symbol, side, entry_time_ms,
               entry_qty, entry_fill_avg, planned_stop, loss_at_stop, exit_time_ms,
               exit_fill_avg, exit_kind, realized_pnl_usdt, commission_usdt,
               funding_usdt, net_r_real, status
        FROM assist_real_trade
        WHERE entry_time_ms >= ? AND status = 'closed' AND side IN ('long', 'short')
        """,
        (int(since_ms),),
    ).fetchall()
    inserted: list[str] = []
    for raw in rows:
        trade = dict(raw)
        oid = str(trade["entry_order_id"])
        if _journal_row(conn, oid) is not None:
            continue
        checks = collect_auto_checks(conn, trade)
        conn.execute(
            """
            INSERT INTO assist_journal (
                entry_order_id, auto_checks_json, grade, mistake_tags, note,
                card_message_id, created_at_ms, graded_at_ms
            ) VALUES (?, ?, NULL, NULL, NULL, NULL, ?, NULL)
            """,
            (oid, json.dumps(checks, sort_keys=True), int(now_ms)),
        )
        conn.commit()
        inserted.append(oid)
    return inserted


def _closes_needing_journal(
    conn: Any,
    closed: list[tuple[str, str]],
    now_ms: int,
) -> list[dict[str, Any]]:
    """Watch closes still inside the retry window with no cardable journal row."""
    seen: set[tuple[str, str]] = set()
    for symbol, position_side in closed:
        seen.add((str(symbol).upper(), str(position_side).upper()))
    fresh = conn.execute(
        """
        SELECT symbol, position_side
        FROM assist_watch_position
        WHERE closed_at_ms IS NOT NULL AND closed_at_ms >= ?
        """,
        (int(now_ms) - RETRY_MS,),
    ).fetchall()
    for raw in fresh:
        row = dict(raw)
        seen.add((str(row["symbol"]).upper(), str(row["position_side"]).upper()))
    pending: list[dict[str, Any]] = []
    for symbol, position_side in seen:
        watch = conn.execute(
            """
            SELECT entry_time_ms, closed_at_ms FROM assist_watch_position
            WHERE symbol=? AND position_side=?
            """,
            (symbol, position_side),
        ).fetchone()
        if watch is None:
            continue
        item = dict(watch)
        closed_ms = _int_or_none(item.get("closed_at_ms"))
        entry_ms = _int_or_none(item.get("entry_time_ms"))
        if closed_ms is None or closed_ms < int(now_ms) - RETRY_MS:
            continue
        if _symbol_has_journal(conn, symbol, entry_ms, closed_ms):
            continue
        pending.append(
            {
                "symbol": symbol,
                "position_side": position_side,
                "closed_at_ms": closed_ms,
                "entry_ms": entry_ms,
            }
        )
    return pending


def _due_sync_closes(
    conn: Any,
    closed: list[tuple[str, str]],
    now_ms: int,
) -> list[dict[str, Any]]:
    """Closes allowed to sync now. The fifth look logs once and does not sync."""
    due: list[dict[str, Any]] = []
    for item in _closes_needing_journal(conn, closed, now_ms):
        state = _sync_state(conn, item)
        if int(state["attempts"]) >= SYNC_MAX_ATTEMPTS:
            if not state["gave_up_logged"]:
                logger.warning(
                    "assist journal sync gave up symbol=%s position_side=%s closed_at_ms=%s",
                    item["symbol"],
                    item["position_side"],
                    item["closed_at_ms"],
                )
                _mark_sync_gave_up(conn, item)
            continue
        last = state["last_attempt_ms"]
        if last is not None and int(now_ms) - int(last) < SYNC_GAP_MS:
            continue
        due.append(item)
    return due


def _since_ms(closes: list[dict[str, Any]]) -> int:
    times = [
        int(item["entry_ms"]) if item["entry_ms"] is not None else int(item["closed_at_ms"])
        for item in closes
    ]
    return min(times) - SYNC_PAD_MS


def _sync_state(conn: Any, item: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT attempts, last_attempt_ms, gave_up_logged
        FROM assist_journal_sync
        WHERE symbol=? AND position_side=? AND closed_at_ms=?
        """,
        (item["symbol"], item["position_side"], int(item["closed_at_ms"])),
    ).fetchone()
    if row is None:
        return {"attempts": 0, "last_attempt_ms": None, "gave_up_logged": False}
    found = dict(row)
    last = found.get("last_attempt_ms")
    return {
        "attempts": int(found.get("attempts") or 0),
        "last_attempt_ms": None if last is None else int(last),
        "gave_up_logged": bool(int(found.get("gave_up_logged") or 0)),
    }


def _record_sync_attempts(conn: Any, closes: list[dict[str, Any]], now_ms: int) -> None:
    for item in closes:
        conn.execute(
            """
            INSERT INTO assist_journal_sync (
                symbol, position_side, closed_at_ms, attempts, last_attempt_ms, gave_up_logged
            ) VALUES (?, ?, ?, 1, ?, 0)
            ON CONFLICT(symbol, position_side, closed_at_ms) DO UPDATE SET
                attempts = attempts + 1,
                last_attempt_ms = excluded.last_attempt_ms
            """,
            (item["symbol"], item["position_side"], int(item["closed_at_ms"]), int(now_ms)),
        )
    conn.commit()


def _mark_sync_gave_up(conn: Any, item: dict[str, Any]) -> None:
    conn.execute(
        """
        UPDATE assist_journal_sync SET gave_up_logged=1
        WHERE symbol=? AND position_side=? AND closed_at_ms=?
        """,
        (item["symbol"], item["position_side"], int(item["closed_at_ms"])),
    )
    conn.commit()


def _symbol_has_journal(
    conn: Any,
    symbol: str,
    entry_ms: int | None,
    closed_ms: int,
) -> bool:
    if entry_ms is None:
        lo, hi = int(closed_ms) - RETRY_MS, int(closed_ms) + MATCH_PAD_MS
    else:
        lo, hi = int(entry_ms) - MATCH_PAD_MS, int(closed_ms) + MATCH_PAD_MS
    row = conn.execute(
        """
        SELECT 1 FROM assist_real_trade r
        JOIN assist_journal j ON j.entry_order_id = r.entry_order_id
        WHERE r.symbol=? AND r.entry_time_ms BETWEEN ? AND ?
          AND r.status = 'closed' AND r.side IN ('long', 'short')
        LIMIT 1
        """,
        (symbol.upper(), lo, hi),
    ).fetchone()
    return row is not None


def _open_positions_at(conn: Any, entry_ms: int | None) -> int:
    if entry_ms is None:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) FROM assist_watch_position
        WHERE entry_time_ms IS NOT NULL AND entry_time_ms <= ?
          AND (closed_at_ms IS NULL OR closed_at_ms > ?)
        """,
        (int(entry_ms), int(entry_ms)),
    ).fetchone()
    return int(row[0] if row is not None else 0)


def _signal_present(conn: Any, trade: dict[str, Any]) -> bool:
    """A stored signal, or a one-tap intent whose preview names an analysis."""
    if trade.get("signal_id"):
        return True
    preview = _intent_preview(conn, trade.get("intent_id"))
    if preview is None:
        return False
    return bool(preview.get("analysis_id"))


def _stop_existed(conn: Any, trade: dict[str, Any]) -> bool:
    """Fail only when a no-stop warning was written during the trade."""
    entry_ms = _int_or_none(trade.get("entry_time_ms"))
    exit_ms = _int_or_none(trade.get("exit_time_ms"))
    if entry_ms is None or exit_ms is None:
        return True
    row = conn.execute(
        """
        SELECT 1 FROM assist_watch_advice
        WHERE symbol=? AND kind='no_stop'
          AND created_at_ms >= ? AND created_at_ms <= ?
        LIMIT 1
        """,
        (str(trade.get("symbol") or "").upper(), int(entry_ms), int(exit_ms)),
    ).fetchone()
    return row is None


def _done_move_stops(conn: Any, trade: dict[str, Any]) -> list[dict[str, Any]]:
    entry_ms = _int_or_none(trade.get("entry_time_ms"))
    exit_ms = _int_or_none(trade.get("exit_time_ms"))
    if entry_ms is None:
        return []
    hi = int(exit_ms) + MATCH_PAD_MS if exit_ms is not None else int(now_ms())
    rows = conn.execute(
        """
        SELECT preview_json, created_at_ms FROM assist_exec_intent
        WHERE kind='move_stop' AND status='done' AND symbol=? AND side=?
          AND created_at_ms >= ? AND created_at_ms <= ?
        ORDER BY created_at_ms
        """,
        (
            str(trade.get("symbol") or "").upper(),
            str(trade.get("side") or "").lower(),
            int(entry_ms) - MATCH_PAD_MS,
            hi,
        ),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        try:
            preview = json.loads(str(item.get("preview_json") or "{}"))
        except json.JSONDecodeError:
            preview = {}
        stop = preview.get("stop")
        if stop is None:
            continue
        out.append({"stop": float(stop), "created_at_ms": int(item["created_at_ms"])})
    return out


def _moves_tightened(side: str, planned: Any, moves: list[dict[str, Any]]) -> bool:
    if not moves:
        return True
    if planned is None:
        return False
    stops = [float(planned), *[float(m["stop"]) for m in moves]]
    return stops_only_tightened(side, stops)


def _exit_followed_plan(conn: Any, trade: dict[str, Any]) -> bool:
    kind = str(trade.get("exit_kind") or "")
    if kind in PLAN_EXIT_KINDS:
        return True
    if kind not in ADVICE_EXIT_KINDS:
        return False
    entry_ms = _int_or_none(trade.get("entry_time_ms")) or 0
    exit_ms = _int_or_none(trade.get("exit_time_ms"))
    hi = int(exit_ms) if exit_ms is not None else int(now_ms())
    row = conn.execute(
        """
        SELECT 1 FROM assist_watch_advice
        WHERE symbol=? AND kind='exit' AND status IN ('sent', 'held')
          AND created_at_ms >= ? AND created_at_ms <= ?
        LIMIT 1
        """,
        (str(trade.get("symbol") or "").upper(), int(entry_ms), hi),
    ).fetchone()
    return row is not None


def _not_in_streak_pause(conn: Any, entry_ms: int | None) -> bool:
    if entry_ms is None:
        return True
    settings = ensure_settings(conn)
    edge = get_edge(conn)
    until = settings.get("paused_until_ms")
    anchor = edge.get("streak_anchor_ms")
    if until is None or anchor is None:
        return True
    return not (int(anchor) <= int(entry_ms) < int(until))


def _exit_line(conn: Any, trade: dict[str, Any]) -> str:
    kind = str(trade.get("exit_kind") or "")
    label = EXIT_LABELS.get(kind, kind or "ไม่ทราบชนิด")
    advice = _last_advice(conn, trade)
    if advice is None:
        return f"ปิด: {label}"
    reason = advice.get("reason")
    extra = f"{advice.get('kind')}"
    if reason:
        extra = f"{extra} ({reason})"
    return f"ปิด: {label} · คำแนะนำล่าสุด: {extra}"


def _last_advice(conn: Any, trade: dict[str, Any]) -> dict[str, Any] | None:
    entry_ms = _int_or_none(trade.get("entry_time_ms")) or 0
    exit_ms = _int_or_none(trade.get("exit_time_ms"))
    hi = int(exit_ms) if exit_ms is not None else int(now_ms())
    row = conn.execute(
        """
        SELECT kind, reason FROM assist_watch_advice
        WHERE symbol=? AND created_at_ms >= ? AND created_at_ms <= ?
          AND status IN ('sent', 'held')
        ORDER BY created_at_ms DESC
        LIMIT 1
        """,
        (str(trade.get("symbol") or "").upper(), int(entry_ms), hi),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _timeline_block(trade: dict[str, Any]) -> str:
    points = _timeline_points(trade)
    if not points:
        return "เลื่อน stop: ไม่มี"
    lines = ["เลื่อน stop:"]
    planned = trade.get("planned_stop")
    for index, (stop, locked) in enumerate(points):
        locked_txt = "ไม่ทราบ" if locked is None else f"{_signed(locked)}R"
        label = "stop เริ่ม " if index == 0 and planned is not None else ""
        lines.append(f"· {label}{_fmt_px(stop)} ล็อก {locked_txt}")
    return "\n".join(lines)


def _timeline_points(trade: dict[str, Any]) -> list[tuple[float, float | None]]:
    """Planned stop, then each done move. Caller passes moves via the trade cache."""
    moves = trade.get("_moves")
    if not isinstance(moves, list):
        moves = []
    side = str(trade.get("side") or "").lower()
    entry = trade.get("entry_fill_avg")
    qty = trade.get("entry_qty")
    loss = trade.get("loss_at_stop")
    loss_f = float(loss) if loss is not None else None
    points: list[tuple[float, float | None]] = []
    planned = trade.get("planned_stop")
    if planned is not None and entry is not None and qty is not None:
        points.append(
            (
                float(planned),
                locked_r(
                    side=side,
                    entry=float(entry),
                    stop=float(planned),
                    qty=float(qty),
                    loss_at_stop=loss_f,
                ),
            )
        )
    if entry is None or qty is None:
        return points
    for move in moves:
        stop = float(move["stop"])
        points.append(
            (
                stop,
                locked_r(
                    side=side,
                    entry=float(entry),
                    stop=stop,
                    qty=float(qty),
                    loss_at_stop=loss_f,
                ),
            )
        )
    return points


def _thesis_line(conn: Any, trade: dict[str, Any]) -> str:
    thesis, confidence = _analysis(conn, trade)
    if thesis is None and confidence is None:
        return "เหตุเข้า: ไม่ทราบ"
    text = "ไม่ทราบ" if thesis is None else thesis.replace("\n", " ").strip()
    if len(text) > 180:
        text = text[:180].rstrip() + "…"
    if confidence is None:
        return f"เหตุเข้า: {text}"
    return f"เหตุเข้า: {text} · ความมั่นใจ {confidence:.2f}"


def _intent_preview(conn: Any, intent_id: Any) -> dict[str, Any] | None:
    if not intent_id:
        return None
    row = conn.execute(
        "SELECT preview_json FROM assist_exec_intent WHERE id=?",
        (str(intent_id),),
    ).fetchone()
    if row is None:
        return None
    raw = row["preview_json"] if hasattr(row, "keys") else row[0]
    try:
        preview = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return None
    if not isinstance(preview, dict):
        return None
    return preview


def _analysis(conn: Any, trade: dict[str, Any]) -> tuple[str | None, float | None]:
    preview = _intent_preview(conn, trade.get("intent_id"))
    if not preview:
        return None, None
    analysis_id = preview.get("analysis_id")
    if not analysis_id:
        return None, None
    found = conn.execute(
        "SELECT thesis, confidence_score FROM analysis_output WHERE analysis_id=?",
        (str(analysis_id),),
    ).fetchone()
    if found is None:
        return None, None
    item = dict(found)
    thesis = str(item["thesis"]) if item.get("thesis") else None
    score = item.get("confidence_score")
    return thesis, None if score is None else float(score)


def _edit_card(
    conn: Any,
    edit: EditFn | None,
    entry_order_id: str,
    *,
    grade: str | None,
    selected: set[str],
    message: dict[str, Any],
) -> None:
    if edit is None:
        return
    mid = message.get("message_id")
    if mid is None:
        row = _journal_row(conn, entry_order_id)
        mid = None if row is None else row.get("card_message_id")
    if mid is None:
        return
    base = str(message.get("text") or "")
    if not base:
        trade = _trade_row(conn, entry_order_id)
        journal = _journal_row(conn, entry_order_id)
        checks: dict[str, bool] = {}
        if journal is not None:
            try:
                loaded = json.loads(str(journal.get("auto_checks_json") or "{}"))
                if isinstance(loaded, dict):
                    checks = loaded
            except json.JSONDecodeError:
                checks = {}
        if trade is not None:
            trade = _with_moves(conn, trade)
            base = format_journal_card(conn, trade, checks)
    text = _grade_suffix(base, grade, selected)
    if grade == "fail":
        markup: dict[str, Any] | None = tag_keyboard(entry_order_id, selected)
    else:
        markup = {"inline_keyboard": []}
    try:
        edit(int(mid), text, reply_markup=markup)
    except Exception:  # noqa: BLE001 — the grade is already stored
        logger.exception("assist journal edit failed for %s", entry_order_id)


def _grade_suffix(text: str, grade: str | None, selected: set[str]) -> str:
    base = text.split("\nเกรด:")[0].rstrip()
    if grade == "pass":
        return base + "\nเกรด: ตามแผน"
    if grade == "fail":
        labels = [TAG_LABELS[tag] for tag in MISTAKE_TAGS if tag in selected]
        extra = f" ({', '.join(labels)})" if labels else ""
        return base + f"\nเกรด: ผิดแผน{extra}"
    return base


def _with_moves(conn: Any, trade: dict[str, Any]) -> dict[str, Any]:
    copied = dict(trade)
    copied["_moves"] = _done_move_stops(conn, trade)
    return copied


def _trade_row(conn: Any, entry_order_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT entry_order_id, intent_id, signal_id, symbol, side, entry_time_ms,
               entry_qty, entry_fill_avg, planned_stop, loss_at_stop, exit_time_ms,
               exit_fill_avg, exit_kind, realized_pnl_usdt, commission_usdt,
               funding_usdt, net_r_real, status
        FROM assist_real_trade WHERE entry_order_id=?
        """,
        (str(entry_order_id),),
    ).fetchone()
    if row is None:
        return None
    return _with_moves(conn, dict(row))


def _journal_row(conn: Any, entry_order_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT entry_order_id, auto_checks_json, grade, mistake_tags, note,
               card_message_id, created_at_ms, graded_at_ms
        FROM assist_journal WHERE entry_order_id=?
        """,
        (str(entry_order_id),),
    ).fetchone()
    return None if row is None else dict(row)


def _set_message_id(conn: Any, entry_order_id: str, message_id: int) -> None:
    conn.execute(
        """
        UPDATE assist_journal SET card_message_id=?
        WHERE entry_order_id=? AND card_message_id IS NULL
        """,
        (int(message_id), str(entry_order_id)),
    )
    conn.commit()


def _write_grade(
    conn: Any,
    entry_order_id: str,
    *,
    grade: str,
    tags: str | None,
    graded_at_ms: int,
) -> None:
    conn.execute(
        """
        UPDATE assist_journal
        SET grade=?, mistake_tags=?, graded_at_ms=?
        WHERE entry_order_id=?
        """,
        (grade, tags, int(graded_at_ms), str(entry_order_id)),
    )
    conn.commit()


def _parse_callback(data: str) -> tuple[str | None, str | None, str | None]:
    if data.startswith("jp:"):
        return "pass", data[3:], None
    if data.startswith("jf:"):
        return "fail", data[3:], None
    if data.startswith("jd:"):
        return "done", data[3:], None
    if data.startswith("jt:"):
        oid, sep, tag = data[3:].rpartition(":")
        if not sep or not oid or not tag:
            return None, None, None
        return "tag", oid, tag
    return None, None, None


def _parse_tags(raw: Any) -> set[str]:
    if not raw:
        return set()
    return {part for part in str(raw).split(",") if part in MISTAKE_TAGS}


def _join_tags(selected: set[str]) -> str | None:
    ordered = [tag for tag in MISTAKE_TAGS if tag in selected]
    return ",".join(ordered) if ordered else None


def _top_tag(counts: dict[str, int]) -> str | None:
    best: str | None = None
    best_n = 0
    for tag in MISTAKE_TAGS:
        n = counts.get(tag, 0)
        if n > best_n:
            best = tag
            best_n = n
    return best


def _net_usdt(trade: dict[str, Any]) -> float | None:
    parts = (
        trade.get("realized_pnl_usdt"),
        trade.get("commission_usdt"),
        trade.get("funding_usdt"),
    )
    if all(part is None for part in parts):
        return None
    return sum(float(part or 0) for part in parts)


def _fmt_px(value: Any) -> str:
    if value is None:
        return "ไม่ทราบ"
    return fmt_price(Decimal(str(value)))


def _signed(value: float) -> str:
    if value > 0:
        return f"+{value:.2f}"
    return f"{value:.2f}"


def _r_or_dash(value: Any) -> str:
    if value is None:
        return "—"
    return f"{_signed(float(value))}R"


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
