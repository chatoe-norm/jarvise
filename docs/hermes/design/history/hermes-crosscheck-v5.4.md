# Cross-check v5.4 (round 9): Codex only

> **Inputs:** `hermes-codex-review-v5.4.md` (Codex r9, `codex exec --sandbox read-only` under `caffeinate -i`, finished 12:00 ICT 2026-11-27).
> **Claude:** not run, per Chana's 11:54 instruction. Claude reviews after its reset (2026-11-30 02:00 BKK). Its safety review is still required before any phase merges.
> **Design files** (box and Mac sha256 match):
> - `hermes-architecture-v5.4.md` `4cb3f8f3…6466`
> - `hermes-brief-implementer-v5.4.md` `eb9c7522…4b39`
> - `hermes-brief-codex-v5.4.md` `bf83eec1…d97a`
> - `hermes-reviewer-sync-brief-v5.4.md` `a1966730…5a93`

## 1. Verdict
| Reviewer | Verdict | P0 start |
|---|---|---|
| Codex r9 | **READY-WITH-CHANGES** | **GO** (the stated no-spend P0 scope) |
| Claude | not run (after 2026-11-30 02:00 BKK) | — |

First P0 GO from Codex in this loop. No P0-BLOCKING items remain.

## 2. Closure of round-8 items
| Item | Codex r9 | Note |
|---|---|---|
| AA3 quick flags | **CLOSED** | Verified against `quick_config.py:660-769`; implementation is proven by h12 at P0 merge |
| AA4 compose fallbacks | **CLOSED** | Narrow exception; proven by h11 at P0 merge |
| AA1 aggregate release | PARTIAL → AB1, AB3 | |
| AA2 durable ledger | PARTIAL → AB2 | |

## 3. New findings (J.A.R.V.I.S.E check); all PHASE-GATED (PR #397 follow-up, before any agents-key spend)
| ID | Sev | Finding | My check | Proposed fix |
|---|---|---|---|---|
| AB1 | High | Unmatched usage found **after** a release only blocks future releases; it doesn't restore the released liability. Also, v5.4 says reconciliation catches direct key use "at the next run", but adapter ledgers can't see calls made directly with the key | Agree. The guaranteed-detection wording is mine and too strong | When newly found `U` breaks the release inequality, re-open debt atomically: add `restored_debt_usd` = the shortfall to liability in the same transaction. Change the residual to "possibly undetectable, bounded by the provider hard limit". Test: discovery after coverage, before delayed billing |
| AB2 | High | Admission still uses the raw `probe_usage`, so a same-period decrease grants admission headroom despite "no credit" | **Confirmed** (A:927 uses `probe_usage`) | Admission uses `budget_period.hwm_usd` as the usage floor, and denies new reservations while the latest read is `decrease`. Test a decrease near each ceiling |
| AB3 | Med | The "unchanged usage → nothing released" test contradicts the cumulative rule once a row becomes lag-eligible later | Agree | Reword as "unchanged usage creates no **new** credit". Already-recorded credit may be used once by rows that mature later. Add a maturity fixture |

## 4. Convergence
| Item | Class |
|---|---|
| P0 design blockers | **none** |
| AA3/AA4 implementation (h12/h11) | PHASE-GATED (P0 merge) |
| AB1–AB3; probe, receipt, cap, digest and atomicity proofs | PHASE-GATED (PR #397 follow-up) |
| Sender fence, attempt and crash proofs | PHASE-GATED (P1) |
| Unset cadence values | PHASE-GATED (P2) |
| Better attribution to reclaim surplus | CAN-DEFER |

## 5. Open decisions for Chana
- None required to start P0.
- **Process choice:**
  - **(a)** Write v5.5 (AB1–AB3, budget section only) now with a short Codex delta, or fold it into the post-Claude round. Either way, it must be done before the PR #397 follow-up spends.
  - **(b)** Whether P0 implementation may begin before Claude's 2026-11-30 design review. Claude's safety review is still required before the P0 merge.
