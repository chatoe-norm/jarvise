"""Second-layer paper reviewer: Jarvise filters, Claude (OpenRouter) confirms / rejects / defers.

Paper only. Decisions are applied exclusively through approve_approval / reject_approval,
so kill-switch, market safety, expiry and risk caps are re-checked by the existing code.
The auto path never submits exchange orders and refuses to run when live trading is on.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jarvise.rag import doctrine_snippets
from jarvise_ingest.db import (
    ensure_paper_account,
    get_analysis_output,
    get_approval,
    get_paper_position,
    insert_llm_review,
    list_approvals,
    list_paper_positions,
    load_latest_candle,
    set_approval_resolve_reason,
)
from jarvise_paper.approval import approve_approval, reject_approval
from jarvise_paper.llm_openrouter import OpenRouterError, chat_json
from jarvise_paper.recommendation import doctrine_query
from jarvise_risk import RiskCaps, estimated_notional, evaluate_from_db, load_risk_caps
from jarvise_trade import live_trading_enabled

PROMPT_VERSION = "2026-10-02.1"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
DECISIONS = frozenset({"approve", "reject", "defer"})
REASON_MAX = 280
UNPARSEABLE = "auto:claude:unparseable"
_TRUE = {"1", "true", "yes", "on"}

SYSTEM_PROMPT = f"""You are the second-layer reviewer for Jarvise, a PAPER trading ledger (no real orders).
Jarvise already filtered this candidate with deterministic rules. Your only job is to confirm, reject, or defer it.
Rules:
1. Reply with ONE JSON object and nothing else: {{"decision": "approve" | "reject" | "defer", "reason": "<= 280 chars"}}.
2. You MUST answer "defer" when ANY of these hold: doctrine is empty AND candidate.confidence_score < 0.70; ledger.same_symbol_open is true; market_safety.reasons is non-empty.
3. Never change size, direction, or price. Never suggest live orders.
4. "approve" only when indicators, doctrine and policy agree with the candidate's thesis; "reject" on a clear contradiction; otherwise "defer".
5. Write the reason in Thai, short, for an owner who does not read charts.
Prompt version: {PROMPT_VERSION}"""

ChatFn = Callable[..., dict[str, Any]]
DoctrineFn = Callable[[str], list[dict[str, Any]]]


@dataclass(frozen=True)
class AutoDecideConfig:
    enabled: bool
    model: str
    min_conf: float
    max_per_run: int
    timeout_s: float
    api_key: str | None


def _fenv(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def load_auto_decide_config() -> AutoDecideConfig:
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip() or None
    return AutoDecideConfig(
        enabled=(os.environ.get("JARVISE_PAPER_AUTO_DECIDE") or "").strip().lower() in _TRUE,
        model=(os.environ.get("JARVISE_AUTO_DECIDE_MODEL") or DEFAULT_MODEL).strip(),
        min_conf=_fenv("JARVISE_AUTO_DECIDE_MIN_CONF", 0.55),
        max_per_run=max(0, int(_fenv("JARVISE_AUTO_DECIDE_MAX_PER_RUN", 4))),
        timeout_s=_fenv("JARVISE_AUTO_DECIDE_TIMEOUT_S", 30.0),
        api_key=key,
    )


def filter_candidates(
    conn: Any,
    rows: list[dict[str, Any]],
    *,
    min_conf: float,
    now_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Hard filters before any LLM call. Returns (eligible oldest-first, filtered_out)."""
    eligible: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: int(r.get("created_at_ms") or 0)):
        reason: str | None = None
        action = str(row.get("action") or "flat").lower()
        size = float(row.get("size_pct_equity") or 0.0)
        conf = float(row.get("confidence_score") or 0.0)
        if action == "flat":
            reason = "action_flat"
        elif size <= 0.0:
            reason = "size_zero"
        elif conf < min_conf:
            reason = "low_conf"
        elif int(row.get("expires_at_ms") or 0) <= now_ms:
            reason = "expired"
        elif evaluate_from_db(conn, str(row["symbol"]), now_ms=now_ms).force_flat:
            reason = "force_flat"
        if reason:
            filtered.append({"id": row["id"], "symbol": row.get("symbol"), "reason": reason})
        else:
            eligible.append(row)
    return eligible, filtered


def build_brief(
    conn: Any,
    row: dict[str, Any],
    *,
    doctrine: list[str],
    now_ms: int,
    caps: RiskCaps,
    min_conf: float,
) -> dict[str, Any]:
    symbol = str(row["symbol"]).upper()
    timeframe = str(row["timeframe"])
    candle = load_latest_candle(conn, symbol, timeframe) or {}
    analysis = (
        get_analysis_output(conn, str(row["analysis_id"])) if row.get("analysis_id") else None
    ) or {}
    account = ensure_paper_account(conn)
    positions = list_paper_positions(conn)
    size = float(row.get("size_pct_equity") or 0.0)
    safety = evaluate_from_db(conn, symbol, now_ms=now_ms).as_dict()
    return {
        "candidate": {
            "symbol": symbol,
            "timeframe": timeframe,
            "action": row.get("action"),
            "regime_state": row.get("regime_state"),
            "confidence_score": row.get("confidence_score"),
            "size_pct_equity": size,
            "invalidation_price": analysis.get("invalidation_price"),
            "thesis": analysis.get("thesis"),
            "expires_at_ms": row.get("expires_at_ms"),
        },
        "indicators": {
            key: candle.get(key) for key in ("close", "ema_20", "ema_200", "rsi_14", "atr_14")
        },
        "ledger": {
            "equity": float(account["equity"]),
            "cash": float(account["cash"]),
            "open_positions": len(positions),
            "same_symbol_open": get_paper_position(conn, symbol) is not None,
        },
        "market_safety": {
            "ok": safety["ok"],
            "force_flat": safety["force_flat"],
            "reasons": list(safety["reasons"]),
        },
        "doctrine": list(doctrine),
        "policy": {
            "min_conf": min_conf,
            "max_notional_per_order_usd": float(caps.max_notional_per_order),
            "max_daily_loss_usd": float(caps.max_daily_loss_usd),
            "estimated_notional_usd": round(
                estimated_notional(equity=float(account["equity"]), size_pct_equity=size), 2
            ),
            "mode": "paper only — no real orders; never change size or direction",
        },
        "prompt_version": PROMPT_VERSION,
    }


def brief_hash(brief: dict[str, Any]) -> str:
    raw = json.dumps(brief, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def parse_decision(obj: Any) -> tuple[str, str]:
    """Strict contract: exactly {decision, reason}. Anything else → defer/unparseable."""
    if not isinstance(obj, dict) or set(obj) != {"decision", "reason"}:
        return "defer", UNPARSEABLE
    decision = str(obj.get("decision") or "").strip().lower()
    if decision not in DECISIONS:
        return "defer", UNPARSEABLE
    reason = str(obj.get("reason") or "").strip()[:REASON_MAX]
    return decision, reason


def _default_doctrine(query: str) -> list[dict[str, Any]]:
    return doctrine_snippets(query, limit=3, raise_on_error=True)


def _blocked_reason(kill_switch: bool, kill_switch_check: Callable[[], bool] | None) -> str | None:
    """Re-evaluated per candidate: the run must stop applying as soon as either gate trips."""
    if kill_switch or (kill_switch_check is not None and kill_switch_check()):
        return "kill_switch engaged"
    if live_trading_enabled():
        return "live_trading_enabled"
    return None


def run_auto_decide(
    conn: Any,
    *,
    now_ms: int | None = None,
    kill_switch: bool = False,
    kill_switch_check: Callable[[], bool] | None = None,
    config: AutoDecideConfig | None = None,
    chat: ChatFn = chat_json,
    doctrine_lookup: DoctrineFn | None = None,
) -> dict[str, Any]:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    cfg = config or load_auto_decide_config()
    started = time.monotonic()
    base: dict[str, Any] = {
        "ok": True,
        "paper_only": True,
        "model": cfg.model,
        "prompt_version": PROMPT_VERSION,
        "at_ms": ts,
    }
    if not cfg.enabled:
        return {**base, "skipped": True, "reason": "auto_decide_disabled"}
    if kill_switch:
        return {**base, "ok": False, "skipped": True, "reason": "kill_switch engaged"}
    if live_trading_enabled():
        return {**base, "ok": False, "skipped": True, "reason": "live_trading_enabled"}

    lookup = doctrine_lookup or _default_doctrine
    caps = load_risk_caps()
    pending_rows = list_approvals(conn, status="pending", limit=100)
    eligible, filtered_out = filter_candidates(conn, pending_rows, min_conf=cfg.min_conf, now_ms=ts)
    approved: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    apply_failed: list[dict[str, Any]] = []
    doctrine_unavailable = False
    lookup_error: str | None = None
    processed = 0

    for index, row in enumerate(eligible):
        entry = {"id": row["id"], "symbol": row.get("symbol")}
        if index >= cfg.max_per_run:
            deferred.append({**entry, "reason": "deferred_cap"})
            continue
        if not cfg.api_key:
            deferred.append({**entry, "reason": "missing_api_key"})
            continue
        blocked = _blocked_reason(kill_switch, kill_switch_check)
        if blocked:
            deferred.append({**entry, "reason": blocked})
            continue
        try:
            try:
                hits = lookup(doctrine_query(row))
            except Exception as exc:  # noqa: BLE001 — spec §8: proceed without doctrine, flag the run
                hits = []
                doctrine_unavailable = True
                lookup_error = f"{type(exc).__name__}: {exc}"
            doctrine = [str(h.get("text")) for h in hits if isinstance(h, dict) and h.get("text")]
            brief = build_brief(conn, row, doctrine=doctrine, now_ms=ts, caps=caps, min_conf=cfg.min_conf)
            processed += 1
            try:
                raw = chat(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(brief, ensure_ascii=False, default=str)},
                    ],
                    model=cfg.model,
                    timeout_s=cfg.timeout_s,
                    api_key=cfg.api_key,
                )
                decision, reason = parse_decision(raw)
            except OpenRouterError as exc:
                decision, reason = "defer", f"auto:claude:error:{exc}"[:REASON_MAX]
            insert_llm_review(
                conn,
                {
                    "approval_id": row["id"],
                    "model": cfg.model,
                    "decision": decision,
                    "reason": reason,
                    "brief_hash": brief_hash(brief),
                    "created_at_ms": ts,
                },
            )
        except Exception as exc:  # noqa: BLE001 — fail closed per candidate; keep the batch running
            deferred.append({**entry, "reason": f"auto:error:{type(exc).__name__}:{exc}"[:REASON_MAX]})
            continue
        entry["reason"] = reason
        try:
            blocked = _blocked_reason(kill_switch, kill_switch_check)
            if blocked:
                deferred.append({**entry, "reason": blocked})
                continue
            if decision == "approve":
                result = approve_approval(conn, row["id"], kill_switch=kill_switch, now_ms=ts)
                if result.get("ok"):
                    set_approval_resolve_reason(conn, row["id"], "auto:claude:approve")
                    approved.append({**entry, "fills": len(result.get("fills") or [])})
                else:
                    inner = str(result.get("error") or "approve failed")
                    after = get_approval(conn, row["id"])
                    if after is not None and after["status"] != "pending":
                        set_approval_resolve_reason(conn, row["id"], f"auto:apply_failed:{inner}"[:400])
                    apply_failed.append({**entry, "error": inner})
            elif decision == "reject":
                result = reject_approval(conn, row["id"], reason=f"auto:claude:reject:{reason}"[:400], now_ms=ts)
                if result.get("ok"):
                    rejected.append(entry)
                else:
                    apply_failed.append({**entry, "error": str(result.get("error") or "reject failed")})
            else:
                deferred.append(entry)
        except Exception as exc:  # noqa: BLE001
            apply_failed.append({**entry, "error": f"{type(exc).__name__}: {exc}"})
            continue

    return {
        **base,
        "processed": processed,
        "approved": approved,
        "rejected": rejected,
        "deferred": deferred,
        "filtered_out": filtered_out,
        "apply_failed": apply_failed,
        "doctrine_unavailable": doctrine_unavailable,
        "doctrine_error": lookup_error,
        "duration_s": round(time.monotonic() - started, 3),
    }
