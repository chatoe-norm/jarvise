# Cross-check v5.5 (round 10): Codex only, budget section only

> **Inputs:** `hermes-codex-review-v5.5.md` (Codex r10, `codex exec --sandbox read-only` under `caffeinate -i`, finished 12:07 ICT 2026-11-27).
> **Claude:** not run. Claude's design and safety review (after 2026-11-30 02:00 BKK) is required **before P0 implementation starts** (Chana 12:04) and before any phase merges.
> **Design files** (box and Mac sha256 match):
> - `hermes-architecture-v5.5.md` `0074e519…eaf1`
> - `hermes-brief-implementer-v5.5.md` `075e1bb7…3165`
> - `hermes-brief-codex-v5.5.md` `401d1d6f…e149`
> - `hermes-reviewer-sync-brief-v5.5.md` `5de17019…14b6`

## 1. Verdict
| Reviewer | Verdict (budget section) |
|---|---|
| Codex r10 | **READY-WITH-CHANGES**: the same-period fixes are sound, and one cross-period gap remains (AC1) |

The P0 position is unchanged from round 9 (P0 design GO for the no-spend scope), but P0 is **on hold** until Claude's design review, per Chana at 12:04.

## 2. Closure
| Item | Codex r10 | Note |
|---|---|---|
| AB1 re-opened debt | PARTIAL → AC1 | Within a period it is closed, and the residual wording is accepted. Late discovery after a period reset is not covered |
| AB2 high-water admission floor, refuse on decrease | **CLOSED** | |
| AB3 recorded credit, maturity fixture | **CLOSED** | |

## 3. New finding (J.A.R.V.I.S.E check)
| ID | Sev | Finding | My check | Proposed fix | Class |
|---|---|---|---|---|---|
| AC1 | High | Reconciliation can find unmatched usage that belongs to an **already-reset** period. Rollover copied that period's final debt (e.g. 0), so the discovery never reaches current admission liability | Agree. v5.5 defines `carried_debt_usd` only at rollover time | Charge a late discovery to the **active** period's `unmatched_usd`. Keep provenance in `budget_debt_event.source_period_id`, with idempotency per source. Recompute the active period's debt in the same transaction. This is conservative: it can only consume current credit or create debt. At rollover, initialize `restored_debt_usd = carried_debt_usd`, and admission adds `restored_debt_usd` only, never `carried_debt_usd` as well. Fixtures: rollover → late discovery → admission; duplicate source; crash | PHASE-GATED (PR #397 follow-up) |

Codex also notes these, which I agree with:
- debt must be recomputed on **every** later `ok` read (already in A:965), and processing a duplicate read must skip the whole operation;
- `unknown` must keep all liabilities and must never override an unresolved `decrease`.

These are wording items for the same small fix.

## 4. Convergence
| Item | Class |
|---|---|
| AC1 plus the two wording items | PHASE-GATED (PR #397 follow-up) |
| Probe, provider-limit, receipt, cap and key-isolation proofs; AB1–AB3 acceptance | PHASE-GATED (PR #397 follow-up) |
| Better attribution / smaller residual | CAN-DEFER |

## 5. Next
- No P0 design blocker.
- P0 implementation waits for Claude's design review (after 2026-11-30 02:00 BKK).
- AC1 is a small budget-only fix: either a v5.6 now with a short Codex delta, or folded into the post-Claude round. It must land before the PR #397 follow-up spends.
