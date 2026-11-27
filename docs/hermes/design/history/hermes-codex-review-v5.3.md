1. **VERDICT: NOT-READY** — Z1 and S-1 are repaired, but the new quick-flag projection contradicts the source API and the literal-test contract contradicts required compose defaults. **P0-START: NO-GO** pending those two corrections; budget findings remain follow-up-gated.

References: **A** = `hermes-architecture-v5.3.md`; **I** = `hermes-brief-implementer-v5.3.md`. Source checks use `origin/main` **438e687** and PR #397 **a5967aa**. CLOSED means design closure; implementation/runtime proofs remain **unverified**.

2. **CLOSURE:**

| Item | Status | Evidence | Remaining gap |
|---|---|---|---|
| Z1 | CLOSED | A:649–664,1522 | Every occurrence gets its own freshness check; mixed-use fixture specified. |
| Z2, P0 part | PARTIAL | A:951–991,1134–1158,1523–1524 | Named settings/checks supplied; compose-default prohibition remains contradictory: AA4. |
| Z2, rest | CLOSED | A:1086–1158,1188–1206 | Round-7 omissions now mapped. Unset job cadences explicitly yield `unknown`; actual schedules remain unverified. |
| Z3 | PARTIAL | A:908–915 | Ratio-zero release removed, but unrelated-charge credit is unsafe and durable allocation schema incomplete: AA1/AA2. |
| Z4 | CLOSED | A:759–762,1548 | Actual lease remainder checked; unbounded post-fence suspension acknowledged. |
| Z5 | CLOSED | A:721,754,776–779 | Effective limit handles one attempt; raised setting cannot reopen an exhausted reservation. Change applies at subsequent reservation/recovery eligibility, without cancelling an active send. |
| C3 remainder | PARTIAL | A:454–459 | Explicit projection cites nonexistent member and misdescribes silent defaults: AA3. |
| C9 remainder | CLOSED | A:721,741,768,771–772 | Persisted counter, reset rule and terminal cap specified. |
| Y5/C11 residual | CLOSED | I:90,102 | Generic snapshot `target` distinguished from proposal `target_ref`; ordered roles use bar-open times. |

3. **SETTINGS AUDIT:**

**Untraceable operational literals:** none additionally identified within this delta/round-7 audit after the stated exceptions. All previously listed omissions now have keys, including fixture composition, reconciliation, acceptance targets and maintenance schedules.

**Default coherence:** every concretely specified default satisfies the listed cross-checks. Examples: timeout plus margin is **20,000 < 30,000 ms**; fixture counts total **200**; age thresholds are ordered. Percent conversion and ATR-period restriction are explicit. PR #397 confirms **8 items / 14 days** at `digest.py:24,28`.

Remaining contract inconsistency: A:951 permits defaults only in registry/tests, while A:958,991,1106 requires numeric compose fallbacks and equality tests. See AA4.

**S-1:** correct at design level. A:1039,1165–1177 removes adjustable night copies and requires the existing function. Verified `signals.py:110–114` exposes `is_night_hold(now_ms=...)` with the stated boundaries. No trading-constant migration is authorized; implementation remains unverified.

4. **NEW FINDINGS:**

**High — AA1: Unrelated usage cannot prove reservation coverage.**  
**Evidence:** A:910–913, especially the assertion that unrelated charges preserve conservatism.  
**Why:** suppose an eligible terminal run still has $1 unreported liability. An unrelated $1 charge raises usage and covers that reservation. Computed exposure becomes $1 although eventual exposure can be $2. Dedication of the key does not establish attribution; daily reconciliation is too late to justify release.  
**Fix:** exclude unreserved/unattributed usage from coverage credit; retain liability when attribution or a conservative aggregate accounting invariant cannot be proven. Require an adversarial unrelated-charge test that preserves the pending liability. **PHASE-GATED(PR #397 follow-up).**

**Medium — AA2: FIFO partial allocations lack a durable representation.**  
**Evidence:** A:909–911 specifies only `(period_id, probe_read_at_ms, usage_usd, allocated_usd)`; neither this table nor A:894’s reservation schema records allocation per reservation.  
**Why:** restart cannot reconstruct which row received a partial allocation. Probe identity, uniqueness, atomic allocation/coverage updates and decreasing-counter behavior are unspecified despite replay/reset guarantees.  
**Fix:** define unique probe records and allocation rows keyed to both probe and reservation; update allocation totals and covered status atomically. Define period baselines and fail closed on unexplained same-period usage decreases rather than crediting a later rebound twice. **PHASE-GATED(PR #397 follow-up).**

**Medium — AA3: C3’s source projection is incorrect.**  
**Evidence:** A:455–456 versus `quick_config.py:680–683,712–721,747–750`.  
**Why:** the actual member is **`clamped_keys`**, not `clamped`. `defaulted_keys` deliberately excludes silently missing config keys. Following the design literally either raises an attribute error or omits required provenance.  
**Fix:** use `clamped_keys`; derive silent-missing provenance through read-only storage presence checks; map/filter flags to served fields, retaining the runtime clamp additions. Add a missing-key fixture. **P0-BLOCKING**, because P0 explicitly builds this projection and tests it at A:1502,1521; this exceeds the former wording-only P1 remainder.

**Medium — AA4: Required compose defaults violate the new literal policy.**  
**Evidence:** A:951 versus A:958,991,1106 and P0 h11 at A:1523.  
**Why:** compose must contain registry-matched numeric fallbacks, yet scanned compose services receive no exception for them. Both acceptance requirements cannot hold as written.  
**Fix:** explicitly permit generated or registry-validated compose fallback defaults; continue rejecting independent numeric values. Use that same exception in the reviewer brief. **P0-BLOCKING.**

No additional hard-rule violation was identified in the reviewed changes.

5. **CONVERGENCE:**

| Remaining item | Classification | Reason |
|---|---|---|
| AA3 / C3 projection | P0-BLOCKING | P0 snapshot implementation cites an invalid API and incomplete provenance. |
| AA4 / Z2 literal policy | P0-BLOCKING | P0 compose and static acceptance contracts conflict. |
| AA1–AA2 / Z3 | PHASE-GATED(PR #397 follow-up) | Required before spend; P0 has no spend. |
| Receipt, cap, probe and digest implementation proofs | PHASE-GATED(PR #397 follow-up) | Contracts alone do not establish enforcement. |
| Z4/Z5/C9 crash and setting-change proofs | PHASE-GATED(P1) | P0 has no outbox or Telegram. |
| Unset job-cadence measurements | PHASE-GATED(P2) | Preserve `unknown` until verified before stale-event activation. |

6. **OPEN DECISIONS FOR CHANA:**

None newly required. S-1 is decided; the remaining corrections follow existing constraints.

7. **GATES:**

- **P0:** correct AA3/AA4, then proceed with manual sessions, read API, evidence, validator/renderer, `pc0`, six tools and registry only.
- **PR #397 follow-up:** resolve AA1/AA2; prove receipts, caps, probe behavior, empty-key rejection and digest compatibility before agents-key spend.
- **P1:** prove effective-limit recovery and both sides of the send fence; retain pin, OpenClaw-removal/parity and readability gates.
- Every phase still requires Claude’s independent safety review before merge, Chana’s shipping approval, blackout compliance, RAM evidence and LearningYard checks.

8. **COMMANDS RUN:**

`diff -u` for all three v5.2/v5.3 pairs; `cat`, `rg -n`, `sed -n` on supplied notes; `git log -1 --oneline` for both refs; scoped `git show`/`git grep` for signals, quick config, indicator storage, jobs and PR #397 digest.

No writes, network, tests, services, sub-agents or secret-file access.