# Hermes Agent Team: design pack v5.6 (design only, no code)

> **DO NOT MERGE until Chana approves.** This folder is a design record kept for review. Nothing here authorizes code, deploys, VPS or `.env` changes, or any trading or risk change.

## Status (2026-11-27)
- **Codex:** budget section **DESIGN-READY** (round 11, v5.6). Whole design **READY-WITH-CHANGES / P0 GO** for the no-spend P0 scope (round 9, v5.4; unchanged since, because v5.5 and v5.6 changed only the budget section).
- **Claude:** last reviewed v5.1 (round 6). It was rate-limited after that, so it has **not** reviewed v5.2–v5.6.
- **P0 implementation is on hold** until Claude's design review (Chana, 2026-11-27 12:04).

## Pending
1. **Claude full design and safety review of v5.6** after **2026-11-30 02:00 BKK**. It should be a full review, not a delta. It is required before P0 implementation starts and before any phase merges.
2. **AD1 (Low, CAN-DEFER):** `hermes-brief-codex-v5.6.md` says a late discovery "raises active-period debt". It should say "consumes active-period credit or raises active-period debt, keeping provenance". Fix in the post-Claude round.
3. **PR #397 follow-up proofs (before any agents-key spend):** probe semantics, the provider hard limit, cap and receipt enforcement, key isolation, and the AB1–AB3/AC1 budget acceptance tests (reset, duplicates, crashes, unknown/decrease).

## Hard rules
- **No merge until Chana approves.** This PR is a draft and stays a draft.
- Design only: no code, deploy, VPS or `.env` action follows from this pack.
- Live trading stays off; main signal rules, `execute.py`, and risk settings are unchanged. Agents never touch orders, risk settings or partner money.
- S-1 (Chana 11:25): existing main trading constants are read-only references, never adjustable copies.
- Every number this design introduces is an adjustable setting (§7.11).

## Files
| File | What |
|---|---|
| `hermes-architecture-v5.6.md` | Source of truth (changelogs §18a–§18k) |
| `hermes-brief-implementer-v5.6.md` | Implementer brief (phase by phase) |
| `hermes-brief-codex-v5.6.md` | Phase co-review brief (Codex and Claude) |
| `hermes-reviewer-sync-brief-v5.6.md` | Round-11 delta-review prompt (budget section) |
| `hermes-codex-review-v5.6.md`, `hermes-crosscheck-v5.6.md` | Round-11 result |
| `history/` | Claude v5.1 review; Codex reviews and cross-checks for v5.1–v5.5 |

## Review history
| Round | Version | Codex | Claude |
|---|---|---|---|
| 6 | v5.1 | NOT-READY / P0 NO-GO | READY-WITH-CHANGES / P0 GO-WITH-CHANGES |
| 7 | v5.2 | NOT-READY / P0 NO-GO | rate-limited |
| 8 | v5.3 | NOT-READY / P0 NO-GO | not run |
| 9 | v5.4 | READY-WITH-CHANGES / **P0 GO** | not run |
| 10 | v5.5 (budget) | READY-WITH-CHANGES | not run |
| 11 | v5.6 (budget) | **DESIGN-READY** | not run |
