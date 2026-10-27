"""Telegram reply / inline keyboard builders for the trade assistant."""

from __future__ import annotations

from typing import Any

BTN_POSITIONS = "📋 ไม้ที่ถืออยู่"
BTN_ANYTHING = "🔎 ตอนนี้มีเหรียญไหนน่าเข้า"
BTN_WEEKLY = "📊 ผลสัปดาห์นี้"
BTN_RISK_PREFIX = "⚙️ ตั้งความเสี่ยงต่อไม้"
BTN_TOGGLE = "⏸ หยุด / ▶️ เปิด ผู้ช่วย"


def risk_button_label(risk_pct: float) -> str:
    pct = f"{risk_pct:g}" if float(risk_pct) == int(risk_pct) else f"{risk_pct:.1f}"
    return f"{BTN_RISK_PREFIX} (ตอนนี้ {pct}%)"


def reply_keyboard(risk_pct: float = 1.0) -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": BTN_POSITIONS}, {"text": BTN_ANYTHING}],
            [{"text": BTN_WEEKLY}, {"text": risk_button_label(risk_pct)}],
            [{"text": BTN_TOGGLE}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def skip_inline_keyboard(signal_id: str) -> dict[str, Any]:
    # callback_data is the assist_signal.id (Telegram limit 64 bytes; our ids fit).
    return {"inline_keyboard": [[{"text": "⏭ ข้าม", "callback_data": signal_id}]]}


def risk_choice_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "1%", "callback_data": "risk:1"},
                {"text": "2%", "callback_data": "risk:2"},
                {"text": "3%", "callback_data": "risk:3"},
                {"text": "5%", "callback_data": "risk:5"},
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
