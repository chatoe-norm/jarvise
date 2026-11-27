1. **VERDICT: READY-WITH-CHANGES** — AA3/AA4 close the P0 blockers; budget coverage still needs corrections before agents-key spend. **P0-START: GO** for the stated no-spend scope.

References: **A** = `hermes-architecture-v5.4.md`; **I** = `hermes-brief-implementer-v5.4.md`. Closure means design closure; implementation and runtime enforcement remain **unverified**.

2. **CLOSURE:**

| Item | Status | Evidence | Remaining gap |
|---|---|---|---|
| AA1 | PARTIAL | A:933–945,956–966 | Detectable unmatched usage discovered after release does not restore liability; reconciliation cannot guarantee detection of direct provider calls. AB1. |
| AA2 | PARTIAL | A:935–954 versus A:918,928 | Durable allocations and atomic updates are specified, but admission still uses falling `probe_usage`. AB2. |
| AA3 | CLOSED | A:468–489,1577; I:161–167 | Correct `clamped_keys`, silent-missing presence check, field projection and fixtures. Verified against `origin/main` `quick_config.py:660–769`. Implementation unverified. |
| AA4 | CLOSED | A:1001,1237–1239,1578; I:168 | Only registry-mapped, unit-matched fallback positions receive the exception. Equality/scanner implementation unverified. |

3. **SOUNDNESS:**

- **Every detectable unrelated charge:** No. Known unmatched usage is excluded before release, but later discovery only increases `U`; previously covered reservations remain released.
- **Residual:** The undercount risk and provider backstop are acknowledged honestly. However, A:956 promises detection at reconciliation that A:966 cannot establish: direct provider calls need not appear in adapter ledgers. Acceptable only as an explicitly bounded, potentially undetectable residual—not guaranteed next-run detection. Provider enforcement remains unverified.
- **Liveness:** Conditional. Example: worst case $2 and final actual usage $1 retains liability indefinitely; sufficiently large later attributed usage can release it. This follows Chana’s retain-liability decision. Normal usage therefore permits some releases, but guarantees neither individual completion nor sustained admission. Already-recorded credit must remain usable after eligibility matures; AB3.
- **Transaction/idempotency:** The single `BEGIN IMMEDIATE`, unique probe records and unique reservation allocations repair durable accounting. Duplicate processing must make the **whole reconciliation operation** a no-op. Crash/replay proofs remain unverified. Decrease handling must also govern admission.
- **AA4:** Narrow enough at design level: known registry variable, canonical default, same unit, exact validated positions only. S-1 remains intact.

4. **NEW FINDINGS:**

**High — AB1: Later unmatched discovery leaves released exposure understated.**  
**Evidence:** A:941,943–950,956,966.  
**Why:** A $2 reservation has $1 reported; an unrelated $1 inside its window permits release. Reconciliation later identifies that charge before the remaining legitimate cost appears. Increasing `U` blocks future releases but never restores the released liability. Furthermore, adapter-count reconciliation cannot observe arbitrary calls made directly with the key.  
**Fix:** Atomically restore conservative debt when newly discovered unmatched usage invalidates coverage, or require sufficient reconciliation before release. Test discovery **after coverage**, before delayed legitimate billing. Remove the guaranteed-detection claim unless an independent provider-side source proves it.  
**Classification:** **PHASE-GATED(PR #397 follow-up).**

**High — AB2: A usage decrease still grants admission headroom.**  
**Evidence:** A:954 versus the admission formula at A:928 and probe payload at A:918.  
**Why:** Coverage uses the high-water mark, but admission uses `probe_usage`. A lower same-period reading can therefore admit another reservation despite “no credit.”  
**Fix:** Use the period high-water mark as the admission usage floor, or deny admission during the anomaly. Apply the policy to every caller and test a decrease near each ceiling.  
**Classification:** **PHASE-GATED(PR #397 follow-up).**

**Medium — AB3: The unchanged-usage test contradicts cumulative coverage.**  
**Evidence:** A:943–945,959.  
**Why:** Usage can reach $2 before a $2 terminal reservation becomes lag-eligible. A later unchanged reading makes it eligible; the formula releases it, while the test requires nothing released.  
**Fix:** State “unchanged usage creates no additional credit”; allow eligible reservations to consume previously recorded credit once. Add that maturity fixture and preserve the separate decrease/rebound restriction.  
**Classification:** **PHASE-GATED(PR #397 follow-up).**

No additional hard-rule violation identified in this delta.

5. **CONVERGENCE:**

| Remaining item | Classification | Reason |
|---|---|---|
| P0 design blockers | None | AA3/AA4 are closed. |
| AB1–AB3 | PHASE-GATED(PR #397 follow-up) | Must resolve before budget enforcement or agents-key spend. |
| Probe, receipts, caps, digest and atomicity proofs | PHASE-GATED(PR #397 follow-up) | Design text does not prove enforcement. |
| AA3/AA4 implementation acceptance | PHASE-GATED(P0) | h12/h11 must pass before P0 merge. |
| Sender fence, attempts and crash proofs | PHASE-GATED(P1) | P0 has no outbox or Telegram. |
| Unset cadence measurements | PHASE-GATED(P2) | Keep `unknown` until verified. |
| Better attribution to reclaim conservative surplus | CAN-DEFER | Retaining liability is the approved safe fallback. |

6. **OPEN DECISIONS FOR CHANA:**

None required to start P0. The findings follow existing safety requirements; no trading-setting decision is needed.

7. **GATES:**

- **P0:** Manual sessions, read API, evidence, validator/renderer, `pc0`, six MCP tools and settings registry only; prove h11/h12. No spend, events, outbox or Telegram.
- **PR #397 follow-up:** Resolve AB1–AB3; prove reservation accounting, receipts, enforceable caps, probe/reset/decrease behavior, empty-key rejection and digest compatibility before spend.
- **P1:** Retain pin, OpenClaw-removal/parity, readability and sender-recovery gates.
- Every phase still requires Claude’s independent safety review before merge, Chana’s shipping approval, blackout compliance and LearningYard deployment checks.

8. **COMMANDS RUN:**

`diff -u` for all three v5.3/v5.4 pairs; `cat` on round-8 review/cross-check; `rg -n` and `sed -n` on supplied notes; `git log -1 --oneline` for both refs; `git show` for quick config and PR #397 compose; scoped `git grep` for budget/probe identifiers on both refs.

No writes, network, tests, services, sub-agents or secret-file access.