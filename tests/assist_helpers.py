"""Shared assist test helpers (not fixtures)."""

from __future__ import annotations

from typing import Any

from jarvise_assist.keyboard import BTN_TOGGLE
from jarvise_assist.menu import handle_menu_text, handle_toggle_confirm_callback


def assist_toggle_confirm(
    conn: Any,
    *,
    now_ms: int,
    send: Any,
    button: str = BTN_TOGGLE,
    message_id: int = 1,
) -> dict[str, Any]:
    """Tap ผู้ช่วย (or legacy), then ✅. Returns the confirm-callback result."""
    prompts: list[dict[str, Any]] = []

    def wrap_send(text: str, reply_markup: Any = None, **kwargs: Any) -> Any:
        prompts.append({"text": text, "reply_markup": reply_markup})
        return send(text, reply_markup=reply_markup, **kwargs)

    prompt = handle_menu_text(conn, button, now_ms=now_ms, send=wrap_send)
    assert prompt.get("confirm") is True
    assert prompts, "confirm prompt was not sent"
    markup = prompts[-1]["reply_markup"]
    assert markup and markup.get("inline_keyboard")
    confirm_data = markup["inline_keyboard"][0][0]["callback_data"]
    assert str(confirm_data).startswith("at:")

    def answer(text: str | None = None) -> None:
        return None

    def edit(mid: int, text: str, *, reply_markup: Any = None) -> bool:
        del mid, text, reply_markup
        return True

    return handle_toggle_confirm_callback(
        conn,
        str(confirm_data),
        send=send,
        answer=answer,
        edit=edit,
        message={"message_id": message_id},
        now_ms=now_ms,
    )
