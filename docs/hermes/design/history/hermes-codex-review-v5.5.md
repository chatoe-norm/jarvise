1. **VERDICT (budget section): READY-WITH-CHANGES** — The same-period fixes are sound, but late discovery across a period reset needs an explicit debt-propagation rule before spend.

References: **A** = `hermes-architecture-v5.5.md`; **I** = `hermes-brief-implementer-v5.5.md`; **C** = `hermes-brief-codex-v5.5.md`. Closure concerns design; implementation remains **unverified**.

2. **CLOSURE:**

| Item | Status | Evidence | Remaining gap |
|---|---|---|---|
| AB1 | PARTIAL | A:935,944–950,960–966,981–986; I:164; C:160 | Same-period restoration and honest residual wording close. Unmatched usage discovered after its period resets has no explicit path into current admission liability; AC1. |
| AB2 | CLOSED | A:934–935,975–977,987; I:165; C:160 | HWM floor, full terminal worst cases and denial until a later `ok` are specified. Enforcement unverified. |
| AB3 | CLOSED | A:955–959,988–989,1564; I:166; C:160 | Existing credit may fund later maturity once; unchanged readings create none. Fixture execution unverified. |

3. **SOUNDNESS:**

- **Safety:** Within one period, the new debt equation restores the aggregate coverage shortfall, and admission includes that debt alongside HWM and uncovered/open worst cases. This closes detectable post-release undercounting within that period. A whole-model guarantee remains **unverified** across rollover because of AC1. The possibly undetectable direct-key-use residual is now honestly stated; provider hard-limit enforcement remains **unverified**.
- **Liveness:** Normal usage permits admission while conservative liability fits the caller ceiling. AB3 prevents an unchanged reading from stranding credit solely because eligibility matured later. Sustained admission is not guaranteed: actual spend below reserved worst cases can retain liabilities indefinitely. That conservative behavior predates v5.5.
- **Probe reads and allocations:** Unique probe/allocation keys and the single transaction provide the intended replay/crash contract (A:943–946,967–974). Duplicate processing must skip the entire accounting operation.
- **Reconciliation and debt events:** Same-transaction unmatched updates, debt recomputation and event uniqueness specify atomic restoration and same-source replay protection (A:945,950,960–962,986). Runtime proofs are **unverified**. Recompute debt on every later `ok` read as A:965 requires, including when unmatched usage is unchanged.
- **Period reset:** Existing debt is explicitly carried; uncovered reservations remain counted. Atomic rollover, replay and late historical discoveries need the AC1 contract and fixture.
- **Decrease / unknown:** A decrease denies every caller until a later `ok`; equality with HWM restores eligibility without creating new credit. Unknown denies core/researcher; the existing bounded digest exception remains (A:1001). It must retain A:935’s liabilities and cannot override an unresolved decrease. An empty key or never-successful probe still denies admission.

4. **NEW FINDINGS:**

**High — AC1: Late historical discovery has no defined propagation into active-period debt.**

**Evidence:** A:944,950,960–966,978; reset acceptance at A:1564.

**Why:** Rollover copies the previous period’s *final* debt. Suppose a $2 reservation was covered with debt zero; the period resets carrying zero; reconciliation subsequently identifies $1 of unmatched usage belonging to that old period. Recomputing the old period restores $1 there, but the contract never says how current admission acquires it. Assigning discoveries to the current period might avoid this, but that rule is also unspecified. Thus detected exposure can remain omitted, beyond the stated undetectable residual.

**Fix:** Define late-discovery attribution and atomically propagate any resulting uncovered debt into active admission accounting, preserving provenance and avoiding double counting. Specify initialization of current `restored_debt_usd` during rollover. Add rollover → late discovery → admission, duplicate-source and crash fixtures.

**Classification:** **PHASE-GATED(PR #397 follow-up).**

No other new delta finding established.

5. **CONVERGENCE:**

| Remaining budget item | Classification |
|---|---|
| AC1: rollover and late-discovery contract/fixtures | PHASE-GATED(PR #397 follow-up) |
| Probe semantics, provider limit, receipt/cap enforcement and key isolation proofs | PHASE-GATED(PR #397 follow-up) |
| AB1–AB3 implementation; replay, reconciliation, debt retirement, reset and crash acceptance | PHASE-GATED(PR #397 follow-up) |
| Better attribution to reclaim conservative surplus or reduce the accepted residual | CAN-DEFER |

6. **GATES:** PR #397 follow-up must resolve AC1 and prove budget acceptance before Stage C or any agents-key spend. Hard limit and probe precede spend; no jobs-key fallback or new trading/risk/partner-money authority is introduced. Claude’s independent design/safety review remains required before P0 implementation starts and before any phase merges; it was not performed here.

7. **COMMANDS RUN:**

`diff -u` on all three v5.4/v5.5 document pairs; `cat` on the round-9 review and cross-check; `sed -n` and `rg -n` on the supplied design/brief files. No writes, network, tests, services, sub-agents or secret-file access.