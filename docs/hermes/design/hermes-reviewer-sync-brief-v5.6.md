# Delta-only design review brief (v5.6, round 11; Codex only; BUDGET SECTION ONLY): Jarvise × Hermes Agent Team

You are **Codex**, the primary design reviewer. Claude Code reviews separately after 2026-11-30 02:00 BKK. Its design and safety review is required before P0 implementation starts and before any phase merges. It is not part of this run.

**Context.**
- In round 10 you reviewed the v5.5 budget section (`hermes-codex-review-v5.5.md`): READY-WITH-CHANGES. AB2 and AB3 were closed; AB1 was partial because of AC1 (a late discovery after a period reset had no path into admission). The merge is `hermes-crosscheck-v5.5.md`.
- Chana approved v5.6 at 12:11:
  - **AC1:** a late discovery is charged to the current period's unmatched usage, and the debt event records the old period; debt is recomputed in the same transaction.
  - At reset, `restored_debt_usd` equals the carried debt, and admission adds only `restored_debt_usd` (no double count).
  - Fixtures for reset → late discovery, duplicate source, and crash.
  - **Wording:** a duplicate reading skips the whole operation; an `unknown` probe keeps all liabilities and never overrides an unresolved drop.
  - Everything else is unchanged.

**Scope: the budget section only.** Review only the v5.5 → v5.6 changes in §7.8 (admission, unmatched usage / late discovery, debt events, period reset, one transaction, unknown state, tests), the matching §12 test line, and the brief bullets. Don't review anything else.

## Mode: read-only (mandatory)
- Don't edit, create, stage or commit files. Don't push, open PRs or comment.
- Don't run `git checkout`/`reset`/`switch`.
- Use only `git show`, `git diff`, `git log`, `git grep`, `rg`, `cat`, `ls`, `diff`, `sed -n`, `head`, `wc`.
- No network, VPS, ssh or docker; no tests; don't launch sub-agents or other reviewers.
- Never read `.env`, keys, tokens or `partner_kyc*`; never print secrets. Don't run `ps`/`pgrep` at all.
- Your only output is your final answer.

## Inputs (notes dir: `/Users/gizmo-macbook/Public/Projects/jarvise/worktrees/.jarvise-notes/hermes-design/`)
- `diff -u hermes-architecture-v5.5.md hermes-architecture-v5.6.md` (and the same for `hermes-brief-implementer-*` and `hermes-brief-codex-*`).
- Changelog: `hermes-architecture-v5.6.md` §18k.
- Round-10 findings: `hermes-codex-review-v5.5.md` (AC1), `hermes-crosscheck-v5.5.md`.

## Hard rules (unchanged; flag any v5.6 change that breaks one)
- No agents-key spend before the hard limit and the probe.
- Fail-closed tokens; no secrets.
- No fallback to the jobs key.
- Agents never touch trading, risk settings or partner money.

## Tasks
- **A. Closure.** Mark AC1, AB1 (overall) and the two wording items (duplicate, unknown) **CLOSED / PARTIAL / OPEN / REGRESSED**, with v5.6 file:line evidence and the remaining gap.
- **B. Soundness of the budget model as a whole after v5.6:**
  - safety: can computed exposure ever fall below real exposure except by the stated residual?
  - liveness: does normal usage still allow admission?
  - idempotency and atomicity: probe reads, reconciliation, debt events, period reset with carried debt;
  - admission under `decrease` and `unknown`.
- **C. New issues** introduced by v5.6 only.
- **D. Convergence:** classify every remaining budget item as **PHASE-GATED(PR #397 follow-up) / CAN-DEFER**, or say "none".

## Output format (exactly)
1. `VERDICT (budget section):` `DESIGN-READY` | `READY-WITH-CHANGES` | `NOT-READY`, plus one sentence.
2. `CLOSURE:` table: item | status | evidence | remaining gap.
3. `SOUNDNESS:` the answers to task B.
4. `NEW FINDINGS:` High / Medium / Low; IDs AD1…; each with evidence, why, fix and its classification.
5. `CONVERGENCE:` table.
6. `GATES:` PR #397 follow-up.
7. `COMMANDS RUN:`.

Be concise: ≤ 700 words. Say "unverified" where evidence is missing.
