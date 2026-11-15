"""Partner-facing Telegram portal (P3). Bound private chats only.

Shows only the partner's own balance. Decimal only. No admin confirm path.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from jarvise_assist import partner_store as ps
from jarvise_assist.futures import build_futures_client
from jarvise_assist.partner_admin import (
    fmt_pct,
    fmt_signed_usdt,
    fmt_usdt,
    send_to_partner,
)
from jarvise_assist.partner_value import (
    AccountValue,
    AccountValueError,
    read_account_value,
)

logger = logging.getLogger(__name__)

BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
_CACHE_TTL_MS = 30_000
_START_RE = re.compile(r"^/start(@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]{20,64})$")

# Portal-only V cache: (AccountValue, read_at_ms) or None
_value_cache: tuple[AccountValue, int] | None = None


def reset_value_cache() -> None:
    """Test hook: clear the portal AccountValue cache."""
    global _value_cache
    _value_cache = None


def _bangkok_stamp(now_ms: int) -> str:
    dt = datetime.fromtimestamp(now_ms / 1000, tz=BANGKOK)
    return dt.strftime("%d/%m %H:%M")


def _state_emoji(state: str) -> str:
    if state == "up":
        return "🟢"
    if state == "down":
        return "🔴"
    return "⚪"


def _gate(message_or_cb_msg: dict[str, Any], from_user: dict[str, Any] | None) -> bool:
    raw_chat = message_or_cb_msg.get("chat")
    chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
    if not isinstance(from_user, dict):
        return False
    return str(chat.get("type") or "") == "private" and str(chat.get("id")) == str(from_user.get("id"))


def _resolve_client(futures: Any) -> Any:
    if futures is not None:
        return futures
    return build_futures_client()


def _read_v_cached(futures: Any, *, now_ms: int) -> AccountValue | AccountValueError:
    global _value_cache
    if _value_cache is not None:
        cached, at = _value_cache
        if int(now_ms) - at < _CACHE_TTL_MS:
            return cached
    av = read_account_value(_resolve_client(futures), now_ms=now_ms)
    if isinstance(av, AccountValue):
        _value_cache = (av, int(now_ms))
    return av


def _card_buttons() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🔄 ดูยอด", "callback_data": "pp:v"}],
        ]
    }


def _build_card(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    av: AccountValue | AccountValueError,
    *,
    now_ms: int,
) -> tuple[str, dict[str, Any] | None]:
    name = str(row["name"])
    stamp = _bangkok_stamp(now_ms)
    cost = ps.usdt_from_micro(int(row["cost_micro"]))
    units = ps.units_from_e8(int(row["units_e8"]))

    if str(row["status"]) == "closed":
        lines = [f"พอร์ตของ {name} · {stamp}", "ปิดพอร์ตแล้ว"]
        due_rows = [r for r in ps.list_due_payouts(conn) if str(r["partner_id"]) == str(row["id"])]
        for d in due_rows:
            x = ps.usdt_from_micro(int(d["payout_micro"] or 0))
            lines.append(f"รอโอน {fmt_usdt(x)} USDT")
        return "\n".join(lines), None

    if isinstance(av, AccountValueError):
        lines = [
            f"พอร์ตของ {name} · {stamp}",
            "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง",
            f"ทุน {fmt_usdt(cost)} USDT",
        ]
        return "\n".join(lines), _card_buttons()

    if units == 0 and cost == 0:
        lines = [
            f"พอร์ตของ {name} · {stamp}",
            "ยังไม่มีเงินในพอร์ต",
        ]
        return "\n".join(lines), _card_buttons()

    view = ps.view_partner(conn, str(row["id"]), av.value)
    if not view.get("ok"):
        lines = [
            f"พอร์ตของ {name} · {stamp}",
            "อ่านยอดไม่ได้ตอนนี้ ลองใหม่อีกครั้ง",
            f"ทุน {fmt_usdt(cost)} USDT",
        ]
        return "\n".join(lines), _card_buttons()

    net = Decimal(view["net"])
    pnl = net - cost
    pct = fmt_pct(pnl, cost)
    if pct != "—" and pnl > 0 and not pct.startswith("+"):
        pct = f"+{pct}"
    lines = [
        f"พอร์ตของ {name} · {stamp}",
        f"ทุน {fmt_usdt(cost)} USDT",
        f"ยอดสุทธิ {fmt_usdt(net)} USDT (หลังหักค่าธรรมเนียม)",
        f"{_state_emoji(str(view['state']))} {fmt_signed_usdt(pnl)} ({pct})",
    ]
    return "\n".join(lines), _card_buttons()


def _send_card(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    send: Any,
    futures: Any,
    now_ms: int,
) -> dict[str, Any]:
    av = _read_v_cached(futures, now_ms=now_ms)
    text, markup = _build_card(conn, row, av, now_ms=now_ms)
    send_to_partner(send, row, text, reply_markup=markup)
    return {"ok": True, "cmd": "card"}


def handle_portal_text(
    conn: sqlite3.Connection,
    message: dict[str, Any],
    *,
    send: Any,
    futures: Any = None,
    now_ms: int,
) -> dict[str, Any]:
    raw_chat = message.get("chat")
    chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
    raw_from = message.get("from")
    from_user: dict[str, Any] = raw_from if isinstance(raw_from, dict) else {}
    if not _gate(message, from_user):
        return {"ignored": "foreign_chat"}

    tg = str(from_user.get("id"))
    row = ps.partner_by_telegram_id(conn, tg)
    if row is not None:
        return _send_card(conn, row, send=send, futures=futures, now_ms=now_ms)

    text = str(message.get("text") or "")
    m = _START_RE.match(text.strip())
    if m is None:
        return {"ignored": "foreign_chat"}

    code = m.group(2)
    admin_chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    bound = ps.bind_invite(conn, code, tg, admin_chat_id=admin_chat, now_ms=now_ms)
    if not bound.get("ok"):
        # Only exception to send_to_partner: invalid invite reply
        try:
            send(
                "ลิงก์เชิญไม่ถูกต้องหรือหมดอายุ — ขอลิงก์ใหม่จากแอดมิน",
                chat_id=str(chat.get("id")),
            )
        except Exception:  # noqa: BLE001
            logger.exception("portal invalid-invite send failed")
        return {"ok": False, "reason": "invite_invalid"}

    bound_row = ps.partner_by_telegram_id(conn, tg)
    assert bound_row is not None
    send_to_partner(send, bound_row, "ผูกบัญชีแล้ว · บอทนี้แสดงเฉพาะยอดของคุณ")
    _send_card(conn, bound_row, send=send, futures=futures, now_ms=now_ms)

    display = str(from_user.get("first_name") or "")
    if not display.strip():
        uname = str(from_user.get("username") or "").strip()
        display = f"@{uname}" if uname else "ผู้ใช้"
    display = display.replace("\n", " ").replace("\r", " ").strip()[:32]
    try:
        send(f"🔗 {bound['name']} ผูก Telegram แล้ว · {display}")
    except Exception:  # noqa: BLE001
        logger.exception("portal admin bind notice failed")
    return {"ok": True, "cmd": "bind", "partner_id": bound["partner_id"]}


def handle_portal_callback(
    conn: sqlite3.Connection,
    data: str,
    *,
    callback: dict[str, Any],
    send: Any,
    answer: Any,
    edit: Any,
    futures: Any = None,
    now_ms: int,
) -> dict[str, Any]:
    raw_msg = callback.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    raw_from = callback.get("from")
    from_user: dict[str, Any] = raw_from if isinstance(raw_from, dict) else {}
    if not _gate(msg, from_user):
        return {"ignored": "foreign_chat"}

    tg = str(from_user.get("id"))
    row = ps.partner_by_telegram_id(conn, tg)
    if row is None:
        return {"ignored": "foreign_chat"}

    cq_id = str(callback.get("id") or "")
    answered = False

    def _answer(text: str | None = None) -> None:
        nonlocal answered
        if not answered:
            answer(cq_id, text=text)
            answered = True

    if data == "pp:v":
        _answer()
        av = _read_v_cached(futures, now_ms=now_ms)
        text, markup = _build_card(conn, row, av, now_ms=now_ms)
        mid = msg.get("message_id")
        partner_id = str(row["telegram_user_id"])
        ok = False
        if mid is not None:
            try:
                ok = bool(edit(int(mid), text, reply_markup=markup, chat_id=partner_id))
            except Exception:  # noqa: BLE001
                logger.exception("portal edit failed")
                ok = False
        if not ok:
            send_to_partner(send, row, text, reply_markup=markup)
        return {"ok": True, "cmd": "refresh"}

    _answer("ปุ่มนี้ใช้ไม่ได้แล้ว")
    return {"ok": False, "reason": "unknown"}
