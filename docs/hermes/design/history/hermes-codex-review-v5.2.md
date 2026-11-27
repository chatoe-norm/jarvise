1. **VERDICT: NOT-READY** — The bar-timing repair is sound, but freshness reuse and the P0 settings contract still need small, explicit corrections. **P0-START: NO-GO** pending Z1 and the P0 portion of Z2.

References: **A** = `hermes-architecture-v5.2.md`; **I** = `hermes-brief-implementer-v5.2.md`; **C** = `hermes-brief-codex-v5.2.md`. CLOSED means design closure; implementation and runtime proofs remain **unverified**.

2. **CLOSURE:**

| Item | Status | Evidence | Remaining gap |
|---|---|---|---|
| Y1/C2 | PARTIAL | A:443–454, 556–568, 625–635, 1393 | Per-timeframe formula, timestamp semantics and historical roles repaired. Multiple uses of one claim need conjunctive freshness checks: Z1. |
| C3 | PARTIAL | A:435–437, 1394 | Effective values correct; provenance flags need explicit projection, including effective clamps and silently defaulted keys. |
| C7/Y4 | CLOSED | A:395, 542–543, 1394 | Observation-level limitation and mandatory `same_observation` explicit. |
| Y5/C11 | PARTIAL | A:456, 462, 810–821; I:169; C:4,138 | I:79 still excludes generic `target`; I:91 retains `bar_close_ms`. |
| C1 | CLOSED | A:846–850; I:66; C:72 | Empty-key and compose acceptance specified; execution unverified. |
| C10 | CLOSED | A:898–905 | Domain-only display, stripped URL, replay rule and consumer fixtures specified. |
| Y2/C4 receipt | CLOSED | A:398, 872 | Bound HMAC receipt, durable consumption and void deadline specified. |
| Y2/C12 coverage | PARTIAL | A:873–874, 1006 | Full worst-case liability retained, but zero coverage ratio defeats coverage proof: Z3. |
| C5 | CLOSED | A:1336, 1353 | Digest cap proofs now precede first spend; proofs unverified. |
| Y3 | PARTIAL | A:729–730, 139? → **1419** | Final check added; lease arithmetic and bounded-pause claim remain wrong: Z4. |
| C9 | PARTIAL | A:692, 736, 739–740 | Mapping/reset/blocked row repaired; `notsent_count` absent from table contract. Configurable retry recovery also incomplete: Z5. |
| C6 | CLOSED | A:821, 1442 | Explicit no-output endpoint and independent sweeper specified. |
| C8 | CLOSED | A:399, 440, 1442 | Stored `assist_signal.id` is the join key. |

Repository checks confirm `INTERVAL_MS` contains the four specified durations; Binance candles store `row[0]` as `timestamp` (`binance_klines.py:49–58`), and `expected_analysis_bar_ms` returns the published **4h opening time** (`sizing.py:392–395`). A:629 correctly generalizes the formula without changing that helper.

3. **SETTINGS AUDIT:**

**No: §7.11 is not exhaustive.** Operational literals lacking a named registry key include:

| Evidence | Value | Suggested key |
|---|---|---|
| A:253 | research lookback 24h | `research_default_lookback_h` |
| A:259 | ask sweeper every 1 minute | `ask_sweeper_interval_s` |
| A:799 | engulfing window 2 bars | `chart_engulfing_bars`, or explicit identity exception |
| A:875,882 | daily reconciliation | `reconciliation_interval_h` |
| A:1109–1112; I:161 | 200 documents; composition 25/25/30/25/30/20/15/15/15 | `research_fixture_counts` |
| A:1310; I:197 | coverage 70% | `coverage_min_pct` |
| A:703; I:136,149; C:138 | pure-module coverage 100% | `pure_branch_coverage_pct` |
| A:1312; I:203; C:135 | 2 mutation guards | `mutation_guard_count` |
| A:1355 | 2 normal digest runs | `digest_parity_run_count` |
| A:1467 | Monday 08:30 | `accountant_report_schedule_bkk` |
| A:1480 | 5 report spot-checks | `manager_report_sample_count` |
| A:1488 | refresh after 2 minutes | `snapshot_refresh_after_min`, or remove superseded instruction |
| A:1496–1497 | monthly upgrades; weekly skill review | `upgrade_interval_months`, `skill_review_interval_days` |

A:1054 puts acceptance targets in TOML but supplies no named keys/ranges. A:1023–1024 references defaults indirectly; verified PR defaults are **8 items / 14 days**. Expand these explicitly.

Coherence gaps:

- Require `age_fresh_max_min ≤ age_old_max_min`; allowed ranges currently permit reversal.
- Define percent conversion for `change_flat_pct=0.1` in A:567’s ratio formula.
- Constrain `atr_period` to available indicator periods.
- Document adapter override distribution/reload and concrete per-job cadence defaults.
- Reconcile A:1395’s narrower literal-test scope with A:911.
- Apply configurable attempt limits throughout recovery: Z5.

**S-1:** Recommend the stated boundary: preserve existing trading constants and require separate approval to migrate them. Main night hold is hard-coded in `signals.py:110–114`; quick-night settings do not drive it. Explain existing values from their actual sources, without creating independently adjustable agent copies.

4. **NEW FINDINGS:**

**Medium — Z1: Freshness overrides need per-use semantics.**  
**Evidence:** A:564,625,632 permits a derived role to replace a claim’s field freshness; claim reuse is allowed.  
**Why:** An old position-entry claim used both directly and through `age_bucket` could receive only `read` freshness, bypassing the direct fact’s `live` requirement.  
**Fix:** Validate every occurrence independently; all applicable checks must pass. Add a mixed direct/derived reuse fixture. **P0-BLOCKING.**

**Medium — Z2: Settings completeness is asserted before being specified.**  
**Evidence:** A:911–928,1054,1376,1395; omissions above.  
**Why:** P0 implements this registry, yet defaults, constraints and literal coverage are incomplete.  
**Fix:** Complete P0 keys, metadata and cross-checks before coding; assign later omissions to their phases. **P0-BLOCKING for P0; otherwise PHASE-GATED.**

**High — Z3: “Provider-proven” coverage accepts unchanged usage.**  
**Evidence:** A:874,1006 sets `coverage_min_ratio=0`.  
**Why:** An unchanged reading satisfies condition (b), releasing liability after the lag alone. A positive heuristic ratio also does not prove attribution.  
**Fix:** Retain liability without authoritative coverage, or conservatively allocate same-period usage increases once against full worst-case amounts. Test unchanged usage, unrelated charges and reset boundaries. **PHASE-GATED(PR #397 follow-up).**

**Medium — Z4: Fence timing guarantee is unsupported.**  
**Evidence:** A:729–730,924.  
**Why:** A remaining lease just above 5 seconds passes despite a 15-second timeout. Suspension after the check is unbounded by either setting.  
**Fix:** Require actual remaining lease greater than timeout plus margin; remove the unsupported pause bound and test suspension on both sides of the fence. **PHASE-GATED(P1).**

**Medium — Z5: Lowering the send limit strands recovery.**  
**Evidence:** A:724,744–745,982.  
**Why:** With `max_send_attempts=1`, an ambiguous first attempt cannot reserve recovery, but terminal handling requires `attempts=2`.  
**Fix:** Terminalize when attempts reach the effective limit; define behavior when limits change in flight. Add the missing `notsent_count` schema field. **PHASE-GATED(P1).**

5. **CONVERGENCE:**

| Remaining item | Classification | Reason |
|---|---|---|
| Z1 | P0-BLOCKING | P0 validator/manual previews must preserve freshness. |
| Z2: P0 registry/constraints | P0-BLOCKING | Registry is explicitly a P0 deliverable. |
| Z3 | PHASE-GATED(PR #397 follow-up) | Required before agents-key spend; P0 spends nothing. |
| Z4, Z5, C9 residual | PHASE-GATED(P1) | P0 has no outbox or Telegram. |
| C3 flag projection | PHASE-GATED(P1) | Required before settings explanations are delivered. |
| Z2 later settings | PHASE-GATED(respective phase) | Research/report controls need not block P0. |
| Y5/C11 residual | CAN-DEFER | Canonical architecture resolves intent. |
| Receipt/cap/event implementation proofs | PHASE-GATED(follow-up/P1/P2) | Contracts closed; runtime evidence unverified. |

6. **OPEN DECISIONS FOR CHANA:**

**S-1 only:** extend the rule into existing trading code, or preserve that boundary. **Recommended: preserve it**, with any migration handled as a separately approved trading change. C10 is already decided.

7. **GATES:**

- **P0:** resolve Z1 and P0 Z2; keep manual-only, six tools, no spend/sender/events.
- **PR #397 follow-up:** resolve Z3; prove receipts, caps, empty-key behavior and digest consumers before spend.
- **P1:** resolve Z4/Z5/C3/C9; retain pin, removal/parity, readability and independent safety-review gates.
- All shipping still requires Chana’s OK, RAM evidence, blackout compliance and LearningYard checks.

8. **COMMANDS RUN:**

`diff -u` for all three v5.1/v5.2 pairs; `cat`, `sed -n`, `rg -n` on supplied notes; `git log -1 --oneline` for both refs; scoped `git show`/`git grep` for sizing, timeframes, candle producer/DB, quick config, store/watch, night hold and PR compose/adapter/digest.

No writes, network, tests, services, sub-agents or secret files accessed.