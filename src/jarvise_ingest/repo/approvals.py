"""Approval queue repository facade."""

from jarvise_ingest.db import (
    claim_approval_for_fill,
    get_approval,
    list_approvals,
    mark_approval_failed,
    resolve_approval,
    set_approval_paper_order_ids,
    set_approval_resolve_reason,
    upsert_pending_approval,
)

__all__ = [
    "claim_approval_for_fill",
    "get_approval",
    "list_approvals",
    "mark_approval_failed",
    "resolve_approval",
    "set_approval_paper_order_ids",
    "set_approval_resolve_reason",
    "upsert_pending_approval",
]
