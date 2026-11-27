# Cross-check v5.6 (round 11): Codex only, budget section only

> **Inputs:** `hermes-codex-review-v5.6.md` (Codex r11, `codex exec --sandbox read-only` under `caffeinate -i`, finished about 12:17 ICT 2026-11-27).
> **Claude:** not run. Claude's design and safety review (after 2026-11-30 02:00 BKK) is required before P0 implementation starts and before any phase merges.
> **Design files** (box and Mac sha256 match):
> - `hermes-architecture-v5.6.md` `1f24677b…2272`
> - `hermes-brief-implementer-v5.6.md` `7bffab94…6787`
> - `hermes-brief-codex-v5.6.md` `88381410…301a`
> - `hermes-reviewer-sync-brief-v5.6.md` `58a2ce44…e06b`

## 1. Verdict
| Reviewer | Verdict (budget section) |
|---|---|
| Codex r11 | **DESIGN-READY**: v5.6 closes AC1 and both wording gaps |

This is the first DESIGN-READY verdict on the budget section. No High or Medium findings.

## 2. Closure
| Item | Codex r11 |
|---|---|
| AC1 late discovery across a reset | **CLOSED** (the $1 vs $0.40 → $0.60 debt fixture was checked) |
| AB1 overall | **CLOSED**. The accepted residual stays: possibly undetectable, bounded by the provider hard limit |
| Duplicate skips the whole operation | **CLOSED** |
| `unknown` keeps liabilities and never lifts a drop | **CLOSED** |

## 3. New finding
| ID | Sev | Finding | My check | Class |
|---|---|---|---|---|
| AD1 | Low | The Codex brief says a late discovery "raises active-period debt", but the architecture also allows it to consume credit instead | Agree. Brief wording only; the architecture and fixture are correct | CAN-DEFER. Fix in the post-Claude round ("consumes active-period credit or raises active-period debt, keeping provenance"). Not changed now, per Chana's "budget-only, everything else unchanged" |

## 4. Remaining items
| Item | Class |
|---|---|
| Probe semantics, hard-limit check, cap/receipt enforcement, key isolation | PHASE-GATED (PR #397 follow-up) |
| AB1–AB3/AC1 implementation and acceptance (reset, duplicates, crashes, unknown/decrease) | PHASE-GATED (PR #397 follow-up) |
| AD1 wording; better attribution / smaller residual | CAN-DEFER |
| **Claude design and safety review of v5.6** (after 2026-11-30 02:00 BKK) | **Required before P0 implementation** |

## 5. Status of the whole design after round 11
- **P0 design:** GO from Codex (round 9), on hold for Claude.
- **Budget section:** DESIGN-READY (round 11).
- **Open Chana decisions:** none.
- **Next step:** Claude reviews v5.6 after its reset. It should be a full design review, not only a delta, because Claude last reviewed v5.1.
