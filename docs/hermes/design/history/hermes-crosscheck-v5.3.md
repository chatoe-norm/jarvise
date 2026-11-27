# Cross-check v5.3 (round 8): Codex only

> **Inputs:** `hermes-codex-review-v5.3.md` (Codex r8, `codex exec --sandbox read-only` under `caffeinate -i`, finished 11:33 ICT 2026-11-27).
> **Claude:** not run, per Chana's 11:25 decision. Claude reviews v5.3 after its reset (2026-11-30 02:00 BKK). Its safety review is still required before any phase merges.
> **Design files** (box and Mac sha256 match):
> - `hermes-architecture-v5.3.md` `825442b7…ce32`
> - `hermes-brief-implementer-v5.3.md` `52593c17…f5f5`
> - `hermes-brief-codex-v5.3.md` `31586a4c…ae3469a`
> - `hermes-reviewer-sync-brief-v5.3.md` `05c8f041…dd780`

## 1. Verdict
| Reviewer | Verdict | P0 start |
|---|---|---|
| Codex r8 | **NOT-READY** | **NO-GO** pending AA3 and AA4 (two narrow corrections) |
| Claude | not run (rate limit; after 2026-11-30 02:00 BKK) | — |

## 2. Closure of round-7 items
| Item | Codex r8 | Note |
|---|---|---|
| Z1 dual-use freshness | **CLOSED** | Per-use check plus the h10 fixture |
| Z2 P0 part | PARTIAL | Every key and cross-check is present. Only AA4 (the compose-default exception) is left |
| Z2 rest | **CLOSED** | Unset job cadences render `unknown` until measured |
| Z3 coverage | PARTIAL | Ratio 0 removed. AA1/AA2 are new and gated to the follow-up PR |
| Z4 fence | **CLOSED** | |
| Z5 attempt limit | **CLOSED** | |
| C3 remainder | PARTIAL → AA3 | |
| C9 remainder | **CLOSED** | |
| Y5/C11 residual | **CLOSED** | |
| S-1 | **Correct** | `is_night_hold(now_ms=…)` verified at `signals.py:110-114`; no copies |

Settings audit: no further untraceable literals. All defaults satisfy all cross-checks (e.g. 20,000 < 30,000 ms; fixture sum 200). Digest 8 / 14 confirmed at PR #397 `digest.py:24,28`.

## 3. New findings (J.A.R.V.I.S.E check)
| ID | Sev | Finding | My check | Class |
|---|---|---|---|---|
| AA3 | Med | The C3 projection names `QuickConfigView.clamped`, but the real member is `clamped_keys`. `defaulted_keys` deliberately **excludes** silently missing config keys, so "including silently defaulted ones" is false | **Confirmed** on `origin/main` `quick_config.py:679-684,712-720` | **P0-BLOCKING** |
| AA4 | Med | The §7.11 literal test scans compose, yet compose must carry `${VAR:-default}` numbers equal to the registry | **Confirmed** (a contradiction in my v5.3 text) | **P0-BLOCKING** |
| AA1 | High | FIFO lets an unrelated usage increase "cover" a still-pending reservation, which understates liability | Agree. My v5.3 sentence claiming unrelated charges stay conservative is wrong: any unattributed dollar can release a row early | PHASE-GATED (PR #397 follow-up) |
| AA2 | Med | Partial allocation has no durable per-reservation record; probe identity and same-period decreases are unspecified | Agree | PHASE-GATED (PR #397 follow-up) |

## 4. Proposed v5.4 fixes (small; no new Chana decision needed)
- **AA3:**
  - use `clamped_keys`;
  - derive "silently defaulted" with a read-only presence check of each served key in the `assist_quick_config` rows (missing → defaulted), together with `defaulted_keys`;
  - filter the flags to served fields only;
  - add a missing-key fixture.
- **AA4:** in §7.11.5, add one exception: compose `${VAR:-N}` fallbacks are allowed only where N equals the registry default, enforced by the existing equality test. Any other compose number fails. Mirror this in both briefs.
- **AA1:**
  - Coverage credit is allowed only while no unattributed spend can exist. During the coverage window, every agents-key call must hold a reservation, checked by the daily reconciliation.
  - A row is released only when total usage since the period baseline is at least the sum of all closed worst cases plus every unreconciled amount (an aggregate invariant, not per-dollar FIFO).
  - Otherwise liability is kept. Red test: an unrelated charge with a pending row → liability unchanged.
- **AA2:**
  - `budget_probe_read(id PK, period_id, read_at_ms, usage_usd, UNIQUE(period_id, read_at_ms))`;
  - `budget_allocation(probe_id, reservation_id, usd, PK(probe_id, reservation_id))`;
  - allocation and `covered` updates run in one transaction;
  - a same-period decrease → ops alert, no credit until usage exceeds the previous high-water mark.

## 5. Convergence
| Item | Class |
|---|---|
| AA3, AA4 | P0-BLOCKING (both are text fixes) |
| AA1, AA2, receipt/cap/probe/digest proofs | PHASE-GATED (PR #397 follow-up) |
| Z4/Z5/C9 crash and setting-change proofs | PHASE-GATED (P1) |
| Unset job-cadence values | PHASE-GATED (P2) |

## 6. Open decisions for Chana
- None new from Codex.
- Process choice: apply AA1–AA4 as v5.4 now with a short Codex delta (recommended; P0 would then likely be GO), or wait and fold them into the round after Claude's 2026-11-30 review.
