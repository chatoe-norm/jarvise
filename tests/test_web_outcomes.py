from __future__ import annotations

from jarvise_web.outcomes import derive_outcome, summarize_outcomes


def test_derive_outcome_not_queued_without_approval() -> None:
    out = derive_outcome("long", None, 0, [])
    assert out["kind"] == "not_queued"
    assert out["approval_id"] is None
    assert out["fills"] == 0


def test_derive_outcome_filled_when_approved_with_fills() -> None:
    out = derive_outcome(
        "long",
        {"id": "ap1", "status": "approved", "resolve_reason": "auto:claude:approve"},
        fills=1,
        fill_reasons=["open_long"],
    )
    assert out["kind"] == "filled"
    assert out["approval_id"] == "ap1"
    assert out["fill_reasons"] == ["open_long"]


def test_derive_outcome_hold_same_side_or_zero_fills() -> None:
    hold = derive_outcome(
        "long",
        {"id": "ap2", "status": "approved", "resolve_reason": "auto:rule:same_side_hold"},
        fills=0,
        fill_reasons=[],
    )
    assert hold["kind"] == "hold"
    timeout = derive_outcome(
        "long",
        {"id": "ap3", "status": "approved", "resolve_reason": "timeout_hold"},
        fills=0,
    )
    assert timeout["kind"] == "hold"
    zero = derive_outcome(
        "long",
        {"id": "ap4", "status": "approved", "resolve_reason": "auto:claude:approve"},
        fills=0,
    )
    assert zero["kind"] == "hold"


def test_derive_outcome_status_kinds() -> None:
    assert (
        derive_outcome("long", {"id": "a", "status": "rejected", "resolve_reason": "ui"})["kind"]
        == "rejected"
    )
    assert (
        derive_outcome("flat", {"id": "b", "status": "timed_out", "resolve_reason": "timeout_flat"})["kind"]
        == "timed_out"
    )
    assert (
        derive_outcome("long", {"id": "c", "status": "failed", "resolve_reason": "boom"})["kind"] == "failed"
    )
    assert (
        derive_outcome(
            "long",
            {"id": "d", "status": "approved", "resolve_reason": "auto:apply_failed:x"},
            fills=0,
        )["kind"]
        == "failed"
    )
    assert derive_outcome("long", {"id": "e", "status": "pending"})["kind"] == "pending"


def test_summarize_outcomes_counts_kinds_and_actions() -> None:
    facts = [
        {
            "action": "long",
            "approval": {"id": "1", "status": "approved", "resolve_reason": "auto:claude:approve"},
            "fills": 1,
            "fill_reasons": ["open_long"],
        },
        {
            "action": "long",
            "approval": {"id": "2", "status": "approved", "resolve_reason": "auto:rule:same_side_hold"},
            "fills": 0,
            "fill_reasons": [],
        },
        {"action": "flat", "approval": None, "fills": 0, "fill_reasons": []},
        {
            "action": "long",
            "approval": {"id": "3", "status": "rejected", "resolve_reason": "ui"},
            "fills": 0,
            "fill_reasons": [],
        },
    ]
    summary = summarize_outcomes(facts)
    assert summary["total"] == 4
    assert summary["by_kind"]["filled"] == 1
    assert summary["by_kind"]["hold"] == 1
    assert summary["by_kind"]["not_queued"] == 1
    assert summary["by_kind"]["rejected"] == 1
    assert summary["by_action"]["long"] == 3
    assert summary["by_action"]["flat"] == 1
