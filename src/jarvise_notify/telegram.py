"""Telegram Bot API alerts (soft-fail)."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
DIGEST_CAP = 20


def notify_configured() -> bool:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return bool(token and chat)


def send_telegram_message(
    text: str,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """POST sendMessage. Returns True on success. Soft-fail otherwise."""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        return False
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    own = client is None
    http = client or httpx.Client(timeout=15.0)
    try:
        resp = http.post(
            url,
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        )
        if resp.status_code != 200:
            logger.warning("telegram send failed: status=%s", resp.status_code)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram send soft-fail: %s", type(exc).__name__)
        return False
    finally:
        if own:
            http.close()


def format_enqueue_message(row: dict[str, Any]) -> str:
    size = row.get("size_pct_equity")
    size_s = f"{size}%" if size is not None else "—"
    return (
        "Jarvise pending approval\n"
        f"{row.get('symbol')} {row.get('timeframe')} · {row.get('action')} · size {size_s}\n"
        f"id={row.get('id')} expires_at_ms={row.get('expires_at_ms')}\n"
        "Approve/Reject on /analytics or: jarvise paper approve <id>"
    )


def format_digest_message(rows: list[dict[str, Any]], *, total: int) -> str:
    lines = [f"Jarvise pending digest ({total})"]
    for row in rows[:DIGEST_CAP]:
        size = row.get("size_pct_equity")
        size_s = f"{size}%" if size is not None else "—"
        lines.append(
            f"- {row.get('symbol')} {row.get('timeframe')} {row.get('action')} "
            f"size={size_s} id={row.get('id')}"
        )
    if total > DIGEST_CAP:
        lines.append(f"… +{total - DIGEST_CAP} more")
    lines.append("Approve on /analytics")
    return "\n".join(lines)


def notify_pending_enqueue(
    row: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Soft-fail notify for one newly enqueued approval."""
    if not notify_configured():
        return False
    try:
        return send_telegram_message(format_enqueue_message(row), client=client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify enqueue soft-fail: %s", type(exc).__name__)
        return False


def notify_pending_digest(
    rows: list[dict[str, Any]],
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Send digest if rows non-empty. Soft-fail. Returns status dict."""
    if not rows:
        return {"ok": True, "sent": False, "reason": "empty", "count": 0}
    if not notify_configured():
        return {"ok": True, "sent": False, "reason": "not_configured", "count": len(rows)}
    text = format_digest_message(rows, total=len(rows))
    sent = send_telegram_message(text, client=client)
    return {
        "ok": True,
        "sent": sent,
        "reason": None if sent else "send_failed",
        "count": len(rows),
    }
