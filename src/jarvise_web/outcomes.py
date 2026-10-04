"""Map approval_queue + paper fills onto owner-facing analysis outcomes."""

from __future__ import annotations

from typing import Any

OUTCOME_KINDS = (
    "filled",
    "hold",
    "rejected",
    "timed_out",
    "failed",
    "pending",
    "not_queued",
)

HOLD_REASONS = frozenset({"auto:rule:same_side_hold", "timeout_hold"})


def empty_outcome_summary() -> dict[str, Any]:
    return {
        "by_kind": {kind: 0 for kind in OUTCOME_KINDS},
        "by_action": {},
        "total": 0,
    }


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def derive_outcome(
    action: object,
    approval: dict[str, Any] | None,
    fills: object = 0,
    fill_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Classify one analysis row. Missing approval → not_queued."""
    _ = action
    fills_n = _as_int(fills)
    reasons = [str(item) for item in (fill_reasons or []) if item]
    if approval is None:
        return _pack(
            kind="not_queued",
            approval_id=None,
            approval_status=None,
            resolve_reason=None,
            resolved_at_ms=None,
            fills=fills_n,
            fill_reasons=reasons,
        )

    status = str(approval.get("status") or approval.get("approval_status") or "").lower() or None
    resolve_raw = approval.get("resolve_reason")
    resolve_reason = str(resolve_raw) if resolve_raw not in (None, "") else None
    approval_id = approval.get("id") or approval.get("approval_id")
    approval_id_s = str(approval_id) if approval_id not in (None, "") else None
    resolved_at_ms = approval.get("resolved_at_ms")

    kind = _kind_from_status(status, resolve_reason, fills_n)
    return _pack(
        kind=kind,
        approval_id=approval_id_s,
        approval_status=status,
        resolve_reason=resolve_reason,
        resolved_at_ms=resolved_at_ms,
        fills=fills_n,
        fill_reasons=reasons,
    )


def summarize_outcomes(facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Count kinds/actions. Facts may be derived outcomes or raw approval/fill dicts."""
    summary = empty_outcome_summary()
    by_kind: dict[str, int] = summary["by_kind"]
    by_action: dict[str, int] = summary["by_action"]
    for fact in facts:
        derived = fact if "kind" in fact else _derive_from_fact(fact)
        kind = str(derived.get("kind") or "not_queued")
        if kind not in by_kind:
            by_kind[kind] = 0
        by_kind[kind] += 1
        action = str(fact.get("action") or derived.get("action") or "").lower() or "unknown"
        by_action[action] = by_action.get(action, 0) + 1
    summary["total"] = len(facts)
    return summary


def _derive_from_fact(fact: dict[str, Any]) -> dict[str, Any]:
    return derive_outcome(
        fact.get("action"),
        fact.get("approval"),
        fact.get("fills") or 0,
        list(fact.get("fill_reasons") or []),
    )


def _kind_from_status(status: str | None, resolve_reason: str | None, fills_n: int) -> str:
    if status == "pending":
        return "pending"
    if status == "rejected":
        return "rejected"
    if status == "timed_out":
        return "timed_out"
    if status == "failed" or (resolve_reason or "").startswith("auto:apply_failed"):
        return "failed"
    if status == "approved":
        if resolve_reason in HOLD_REASONS or fills_n <= 0:
            return "hold"
        return "filled"
    if status is None:
        return "not_queued"
    return "failed"


def _pack(
    *,
    kind: str,
    approval_id: str | None,
    approval_status: str | None,
    resolve_reason: str | None,
    resolved_at_ms: object,
    fills: int,
    fill_reasons: list[str],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "approval_id": approval_id,
        "approval_status": approval_status,
        "resolve_reason": resolve_reason,
        "resolved_at_ms": resolved_at_ms,
        "fills": fills,
        "fill_reasons": fill_reasons,
    }
