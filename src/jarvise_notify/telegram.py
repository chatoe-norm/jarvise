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


def format_ingest_health_message(payload: dict[str, Any]) -> str:
    lines = [f"Jarvise ingest health ({payload.get('timeframe')})"]
    for sym, info in (payload.get("symbols") or {}).items():
        lines.append(
            f"- {sym}: rows={info.get('rows')} ema200_ready={info.get('ema200_ready')} "
            f"age_min={info.get('newest_age_min')} gaps={info.get('gaps')}"
        )
    for alert in (payload.get("alerts") or [])[:DIGEST_CAP]:
        lines.append(f"! {alert}")
    hint = payload.get("backfill_hint")
    if hint:
        lines.append("One-shot backfill (VPS worker):")
        lines.append(str(hint))
    return "\n".join(lines)


def notify_ingest_health(
    payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Alert only when there are alerts. Soft-fail."""
    if not payload.get("alerts") or not notify_configured():
        return False
    return send_telegram_message(format_ingest_health_message(payload), client=client)


def format_auto_decide_message(payload: dict[str, Any]) -> str:
    if payload.get("skipped") and payload.get("reason") == "live_trading_enabled":
        return (
            "Jarvise auto-decide REFUSED: JARVISE_LIVE_TRADING=true.\n"
            "Auto path is paper-only; queue left for the owner."
        )
    if payload.get("error"):
        return f"Jarvise paper auto-decide CRASHED: {payload['error']}\nQueue untouched beyond what the payload lists; check Ops."
    approved = payload.get("approved") or []
    rejected = payload.get("rejected") or []
    deferred = payload.get("deferred") or []
    failed = payload.get("apply_failed") or []
    lines = [
        f"Jarvise paper auto-decide ({payload.get('model')})",
        f"approved={len(approved)} rejected={len(rejected)} deferred={len(deferred)} failed={len(failed)}",
    ]
    for d in deferred[:DIGEST_CAP]:
        lines.append(f"- DEFER {d.get('symbol')} id={d.get('id')}: {d.get('reason')}")
    for f in failed[:DIGEST_CAP]:
        lines.append(f"- FAILED {f.get('symbol')} id={f.get('id')}: {f.get('error')}")
    lines.append("Decide on Home (:8080) or: jarvise paper approve|reject <id>")
    return "\n".join(lines)


def format_kill_switch_message(reason: str, *, engaged: bool, error: str | None = None) -> str:
    if engaged:
        return (
            "Jarvise KILL-SWITCH ENGAGED\n"
            f"reason: {reason}\n"
            "Paper enqueue/approve and jobs are blocked until you clear it on Ops (:8080/ops) "
            "after a written review."
        )
    return (
        "Jarvise KILL-SWITCH ENGAGE FAILED\n"
        f"reason: {reason}\n"
        f"error: {error or 'unknown'}\n"
        "Redis is unset or unreachable. Services fail closed, but check Redis now."
    )


def notify_kill_switch(
    reason: str,
    *,
    engaged: bool,
    error: str | None = None,
    client: httpx.Client | None = None,
) -> bool:
    """Alert on every kill-switch engage attempt (success or failure). Soft-fail."""
    if not notify_configured():
        return False
    try:
        return send_telegram_message(
            format_kill_switch_message(reason, engaged=engaged, error=error), client=client
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify kill-switch soft-fail: %s", type(exc).__name__)
        return False


def notify_auto_decide(
    payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> bool:
    """One message per run when something needs the owner (defer / failure / live refusal)."""
    refused = bool(payload.get("skipped")) and payload.get("reason") == "live_trading_enabled"
    needs_owner = (
        refused
        or bool(payload.get("error"))
        or bool(payload.get("deferred"))
        or bool(payload.get("apply_failed"))
    )
    if not needs_owner or not notify_configured():
        return False
    return send_telegram_message(format_auto_decide_message(payload), client=client)
