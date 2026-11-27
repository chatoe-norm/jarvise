1. **VERDICT (budget section): DESIGN-READY** — v5.6 closes AC1 and both wording gaps; implementation and provider guarantees remain unverified.

References: **A** = `hermes-architecture-v5.6.md`; **I** = `hermes-brief-implementer-v5.6.md`; **C** = `hermes-brief-codex-v5.6.md`. Closure means design closure.

2. **CLOSURE:**

| Item | Status | Evidence | Remaining gap |
|---|---|---|---|
| AC1 | CLOSED | A:959–962,978–983,1006–1012; I:170–174 | Active-period charge, historical provenance, atomic recompute, rollover initialization and fixtures specified. Enforcement unverified. |
| AB1 overall | CLOSED | A:941,972–983,996–1000 | Detected shortfalls reach admission across resets; carried debt counted once. Accepted undetectable residual remains. |
| Duplicate wording | CLOSED | A:953,960,985,1011–1012; I:172 | Entire accounting operation skipped on duplicate source/read. Runtime idempotency unverified. |
| Unknown wording | CLOSED | A:939–940,1013,1026–1028; I:173 | All liabilities retained; unresolved decrease still blocks every caller. Enforcement unverified. |

3. **SOUNDNESS:**

- **Safety:** No additional undercount path established by this delta. Late discoveries consume active credit or increase active debt atomically. Admission includes HWM, uncovered/open worst cases and active restored debt. The reset → discovery fixture correctly converts $1 unmatched usage against $0.40 credit into $0.60 debt (A:1009). A real-exposure guarantee remains conditional on cap/probe enforcement; the explicitly accepted direct-key-use residual remains bounded only by the provider hard limit (A:999). Those external guarantees are **unverified**.
- **Liveness:** Normal usage permits admission while conservative liability plus the new reservation fits its ceiling. Previously recorded credit remains usable when rows mature. Actual spending below reserved worst cases can retain liabilities and restrict admission indefinitely; this conservative behavior predates v5.6.
- **Idempotency/atomicity:** Probe uniqueness, whole-operation duplicate handling and `BEGIN IMMEDIATE` cover reads and allocations. Reconciliation checks source identity before charging, recomputing debt and inserting its event. Rollover initializes restored debt equal to carried debt, records a unique rollover event and freezes the old period. Uncovered reservations remain counted. Every later `ok` recomputes debt; crash/replay fixtures cover all three transactions (A:978–995,1011–1012). Execution is **unverified**.
- **Decrease/unknown:** Decrease blocks all callers until a later `ok`; intervening unknown reads cannot lift it. Without a drop, unknown denies core/researcher and permits digest only under both the full-liability ceiling and unknown headroom. A never-successful probe still denies admission (A:916).

4. **NEW FINDINGS:**

**High:** none. **Medium:** none.

**Low — AD1: Reviewer brief overstates debt growth.**

- **Evidence:** C:164 says a late discovery “raises active-period debt”; A:962 permits consuming credit instead.
- **Why:** With sufficient credit, debt remains zero. The brief could encourage an incorrect assertion.
- **Fix:** Say “consumes active-period credit or raises active-period debt, preserving provenance.”
- **Classification:** **CAN-DEFER**; the authoritative architecture and fixture are clear.

5. **CONVERGENCE:**

| Remaining budget item | Classification |
|---|---|
| Probe semantics, hard-limit verification, cap/receipt enforcement and key-isolation proofs | PHASE-GATED(PR #397 follow-up) |
| AB1–AB3/AC1 implementation and acceptance, including reset, duplicates, crashes and unknown/decrease | PHASE-GATED(PR #397 follow-up) |
| AD1 wording | CAN-DEFER |
| Better attribution or reduction of the accepted residual | CAN-DEFER |

6. **GATES:** PR #397 follow-up must prove budget acceptance before Stage C or agents-key spend (A:1578–1591). Hard limit and probe precede spend. No delta relaxes fail-closed credentials, permits jobs-key fallback, or grants trading/risk/partner-money authority. Claude’s independent design/safety review after **2026-11-30 02:00 BKK** remains required before P0 implementation and every phase merge; unperformed here.

7. **COMMANDS RUN:** `diff -u` on all three v5.5/v5.6 document pairs; `cat` on the round-10 review and cross-check; `rg -n` piped to `sed -n` for numbered design/brief excerpts. No writes, network, tests, services, sub-agents or secret-file access.