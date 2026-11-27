# Cross-check v5.2 (delta-only): Codex r7 × Claude Code — Claude was rate-limited

> Inputs: `hermes-codex-review-v5.2.md` (Codex r7, delta-only, read-only) and `hermes-claude-review-v5.2.md`. Both were launched in parallel on the Mac under `caffeinate -i` at 11:13 Asia/Bangkok.
> **Claude did not run a review.** It exited with code 1, and its whole output is: "You've hit your weekly limit · resets Nov 30 at 2am (Asia/Bangkok)". As instructed, I did not substitute another reviewer or retry. So this cross-check has **one** review, not two. There is no Claude safety review for v5.2 until the limit resets.
> Status: design only. Nothing here authorises code, push, PR, VPS or `.env` action.

## 1. Verdicts
| | Codex r7 | Claude Code |
|---|---|---|
| Design | **NOT-READY** ("the bar-timing repair is sound, but freshness reuse and the P0 settings contract still need small, explicit corrections") | not run (weekly limit; resets 2026-11-30 02:00 BKK) |
| P0 start | **NO-GO** pending Z1 and the P0 part of Z2 | — |
| Safety review | n/a | **missing**; required before any phase ships (D-6) |

## 2. Closure of the v5.1 cross-check items (Codex)
| Item | Status | Note |
|---|---|---|
| Y1/C2 freshness (the v5.1 P0 blocker) | PARTIAL | The per-tf formula, timestamp semantics and historical roles are repaired. Codex confirms `INTERVAL_MS` and that candles store the open time (`binance_klines.py:49-58`). The remaining gap is Z1 (a claim used twice) |
| C3 effective quick values | PARTIAL | The values are right; the flag projection (effective clamps, silent defaults) needs spelling out. P1-gated |
| C7/Y4 incarnation | CLOSED | |
| Y5/C11 drift | PARTIAL | Implementer brief: line 79 still says generic `target`, line 91 still says `bar_close_ms`. Can defer |
| C1 compose `:-` + fail closed | CLOSED | |
| C10 digest links | CLOSED | |
| Y2/C4 signed receipt | CLOSED | |
| Y2/C12 coverage | PARTIAL | Z3: `coverage_min_ratio = 0` lets liability go once the lag passes |
| C5 cap proofs | CLOSED | |
| Y3 pre-HTTP fence | PARTIAL | Z4: the lease arithmetic and the claimed pause bound are wrong |
| C9 sender details | PARTIAL | `notsent_count` column missing; Z5 |
| C6 no-output event outcomes | CLOSED | |
| C8 signal join key | CLOSED | |

## 3. New findings (Codex)
- **Z1, Medium, P0-BLOCKING: freshness must be checked per use.** A claim used both directly (`live`) and through a derived role (`read`) would get only the looser check. Fix: check every occurrence separately, and require all checks to pass. Add a mixed direct-plus-derived fixture.
- **Z2, Medium, P0-BLOCKING for the P0 registry part: settings registry not complete.** Codex found design numbers with no §7.11 key:
  - research lookback 24 h (flow);
  - ask sweeper 1 min;
  - engulfing window 2 bars;
  - daily reconciliation interval;
  - research fixture counts (200; 25/25/30/25/30/20/15/15/15);
  - repo coverage 70%;
  - pure-module branch coverage 100%;
  - 2 mutation guards;
  - 2 parity digest runs;
  - accountant report Monday 08:30;
  - 5 manager report spot-checks;
  - snapshot refresh after 2 min (superseded text);
  - monthly upgrades / weekly skill review.

  Also:
  - acceptance targets lack named keys and ranges;
  - the digest defaults should be written as the verified PR values (**8 items / 14 days**).

  Coherence fixes needed:
  - require `age_fresh_max_min ≤ age_old_max_min`;
  - define the `%` conversion for `change_flat_pct`;
  - constrain `atr_period` to the stored indicator periods;
  - give concrete per-job cadence defaults;
  - document how adapters receive overrides and reload them;
  - make the static-literal test scope one definition.
- **Z3, High, follow-up-gated: coverage accepts unchanged usage.** With ratio 0, an unchanged reading releases liability after the lag alone. Fix: keep liability until coverage is authoritative, or allocate each same-period usage increase once against full worst-case amounts. Tests: unchanged usage, unrelated charges, period reset.
- **Z4, Medium, P1-gated: the fence timing isn't guaranteed.** A lease with just over 5 s left passes, even though the HTTP timeout is 15 s. Fix: require the remaining lease to be greater than the timeout plus the margin. Drop the unsupported pause-bound claim, and test suspension on both sides of the fence.
- **Z5, Medium, P1-gated: lowering `max_send_attempts` strands recovery.** With a limit of 1, an ambiguous send can't be recovered, but terminal handling requires `attempts = 2`. Fix: make a row terminal when attempts reach the effective limit, define what happens when the limit changes mid-flight, and add `notsent_count` to the table.

## 4. Settings audit result
- §7.11 covers the main contracts (sessions, freshness, delivery, events, budget, digest, RAM, gates) but **isn't yet exhaustive**. The Z2 list above is the gap.
- **New fact from Codex on S-1:** Jarvise's main night hold is hard-coded in `signals.py:110-114`, and the `quick_night_*` settings do **not** drive it. So v5.2 §7.11.2 should fall back to its second option: an agent `night_start`/`night_end` setting with a CI equality test against the hard-coded hold. The alternative is to explain the existing value from its real source, without an independently adjustable copy. Recommendation: **the explain-from-source option**, so that the agent and trading code never disagree.

## 5. Merged must-fix list (from the single review)
**Before P0 (a small v5.3 text pass):**
1. Z1: per-use freshness checks plus the fixture.
2. Z2 (P0 part): complete the P0 registry:
   - keys for every number P0 uses, including named acceptance targets and the coverage percentages;
   - the coherence cross-checks above;
   - the digest defaults as verified values;
   - one scope for the literal test;
   - the night-hold source per S-1 (§4).

**Before the PR #397 follow-up:** Z3 (authoritative or allocated coverage), the remaining Z2 digest/budget keys (reconciliation interval), and the digest parity run count.

**Before P1:** Z4, Z5 (plus `notsent_count`), the C3 flag projection, and the remaining C9 items.

**Later phases:** the Z2 research, report and maintenance keys go to P3a/P4/P5.

**Can defer:** Y5/C11 leftovers in the implementer brief.

## 6. Decisions for Chana
- **S-1 (open):** does the standing rule also cover existing trading-code constants (the 5x clamp, 2R, analyzer floors, the hard-coded main night hold, the `sizing.py` cadence and lag)?
  - Codex and I both recommend **keeping the boundary**: leave them as they are, and treat any migration as a separately approved trading change, because hard rules 2–6 freeze that code.
- **Claude review:** wait for the reset (2026-11-30 02:00 BKK), or proceed with Codex alone for the v5.3 text pass and run the Claude safety review before any phase ships.
  - **Recommended:** do v5.3 now with a Codex delta review; run Claude on v5.3 after the reset. The Claude safety review stays mandatory before P0 implementation is merged (D-6).

## 7. Process notes
- Codex: exit 0, empty stderr, finished at 11:16 BKK.
- Claude: exit 1, rate-limited; not retried and not substituted.
- Process checks used only `pgrep -f <marker>`. No push, PR, VPS or `.env` action; no secrets read.
