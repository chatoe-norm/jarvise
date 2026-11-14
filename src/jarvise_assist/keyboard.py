"""Telegram reply / inline keyboard builders for the trade assistant."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

BTN_POSITIONS = "📋 ไม้ที่ถืออยู่"
BTN_ANYTHING = "🔎 ตอนนี้มีเหรียญไหนน่าเข้า"
BTN_WEEKLY = "📊 ผลสัปดาห์นี้"
BTN_RISK_PREFIX = "⚙️ ตั้งความเสี่ยงต่อไม้"
BTN_TOGGLE = "⏸ หยุด / ▶️ เปิด ผู้ช่วย"
BTN_EXPLORE = "🧪 เหรียญทดลอง"
BTN_QUICK = "⚡ Quick"
EXPLORE_RM_PREFIX = "explore:rm:"

RISK_CHOICE_PCTS: tuple[float, ...] = (0.5, 1.0, 2.0, 3.0)


def risk_button_label(risk_pct: float) -> str:
    pct = f"{risk_pct:g}" if float(risk_pct) == int(risk_pct) else f"{risk_pct:.1f}"
    return f"{BTN_RISK_PREFIX} (ตอนนี้ {pct}%)"


def reply_keyboard(risk_pct: float = 1.0) -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": BTN_POSITIONS}, {"text": BTN_ANYTHING}],
            [{"text": BTN_WEEKLY}, {"text": risk_button_label(risk_pct)}],
            [{"text": BTN_TOGGLE}],
            [{"text": BTN_EXPLORE}, {"text": BTN_QUICK}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def explore_remove_keyboard(symbols: list[str]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": f"ลบ {symbol}", "callback_data": f"{EXPLORE_RM_PREFIX}{symbol}"}] for symbol in symbols
        ]
    }


def skip_inline_keyboard(signal_id: str, *, entry: bool = False) -> dict[str, Any]:
    # callback_data is the assist_signal.id (Telegram limit 64 bytes; our ids fit).
    row: list[dict[str, str]] = [{"text": "⏭ ข้าม", "callback_data": signal_id}]
    if entry:
        row.insert(0, {"text": "เข้าไม้", "callback_data": f"ec:{signal_id}"})
    return {"inline_keyboard": [row]}


def coin_entry_keyboard(symbol: str, side: str) -> dict[str, Any]:
    side_code = "l" if side.lower() == "long" else "s"
    label = f"{symbol.upper()} {'LONG' if side_code == 'l' else 'SHORT'}"
    return {"inline_keyboard": [[{"text": label, "callback_data": f"ec:{symbol.upper()}:{side_code}"}]]}


def anything_now_keyboard(ideas: list[tuple[str, str]]) -> dict[str, Any]:
    """One button per admitted (symbol, side)."""
    rows = []
    for symbol, side in ideas:
        side_code = "l" if side.lower() == "long" else "s"
        label = f"{symbol.upper()} {'LONG' if side_code == 'l' else 'SHORT'}"
        rows.append([{"text": label, "callback_data": f"ec:{symbol.upper()}:{side_code}"}])
    return {"inline_keyboard": rows}


def entry_confirm_keyboard(intent_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ ยืนยันเข้าไม้", "callback_data": f"ey:{intent_id}"},
                {"text": "✖️ ยกเลิก", "callback_data": f"en:{intent_id}"},
            ]
        ]
    }


def move_stop_keyboard(intent_id: str, *, new_stop: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": f"✅ เลื่อน → {new_stop}", "callback_data": f"ms:{intent_id}"},
                {"text": "❌ ไม่เลื่อน", "callback_data": f"mn:{intent_id}"},
            ]
        ]
    }


def exit_confirm_keyboard(intent_id: str) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": "✅ ปิดไม้", "callback_data": f"xx:{intent_id}"}]]}


def _risk_choice_label(pct: float, balance: Decimal | None) -> str:
    pct_s = f"{pct:g}"
    if balance is None or balance <= 0:
        return f"{pct_s}%"
    usdt = (balance * Decimal(str(pct)) / Decimal("100")).quantize(Decimal("0.01"))
    return f"{pct_s}% · {usdt} USDT"


def risk_choice_keyboard(*, balance: Decimal | None = None) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": _risk_choice_label(pct, balance),
                    "callback_data": f"risk:{pct:g}",
                }
                for pct in RISK_CHOICE_PCTS
            ],
            [{"text": "อื่นๆ", "callback_data": "risk:other"}],
        ]
    }


def risk_confirm_keyboard(pct: float) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Yes", "callback_data": f"risk_yes:{pct:g}"},
                {"text": "No", "callback_data": "risk_no"},
            ]
        ]
    }
