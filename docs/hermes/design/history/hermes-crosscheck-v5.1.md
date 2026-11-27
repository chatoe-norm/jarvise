# Cross-check v5.1: Codex r6 × Claude Code (parallel, read-only), merged by J.A.R.V.I.S.E

> Inputs: `hermes-codex-review-v5.1.md` (Codex r6) and `hermes-claude-review-v5.1.md` (Claude Code co-review + safety review). Both reviewed `hermes-architecture-v5.1.md` and its briefs against `origin/main` 438e687 and PR #397 a5967aa. They ran in parallel (launched 10:49, done by about 11:00, Asia/Bangkok) and neither saw the other's output.
> Status: design only. Nothing here authorises code, push, PR, VPS or `.env` action.

## 1. Verdicts

| | Codex r6 | Claude Code |
|---|---|---|
| Design verdict | **NOT-READY** | **READY-WITH-CHANGES** |
| P0 start | **NO-GO** pending Y1 only | **GO-WITH-CHANGES** once C2 lands |
| Safety review | n/a | **APPROVE** (no hard-rule break; spend risks gated before any agents-key use) |
| Open Chana decisions | none | one optional (C10: digest URL text) |

The two verdicts disagree in label but agree in substance: **exactly one design-text fix blocks P0, and they found the same one** (freshness vs bar timing and derived rules: Codex Y1 = Claude C2). Everything else is phase-gated to the PR #397 follow-up, P1, P2 or later, or can be deferred.

## 2. X1–X9 closure, side by side

| ID | Codex | Claude | Merged view |
|---|---|---|---|
| X1 delivery | PARTIAL (Y3: a late worker can still send after its lease or policy facts expire) | CLOSED (minor details in C9) | **PARTIAL.** Y3 is real: completion is fenced, transmission is not. Fix before P1 |
| X2 budget | PARTIAL (Y2: probe age ≠ coverage; ingress can't verify a reservation) | PARTIAL (C4 authenticity, C5 cap proofs, C12 understated liability) | **PARTIAL, agreed.** Fix before the PR #397 follow-up |
| X3 relations | PARTIAL (Y4: incarnation isn't true lifetime identity) | CLOSED (C7: drop mutable `entry` from the hash) | **CLOSED for P0 if `same_observation` is mandatory.** Narrow the incarnation (C7/Y4); can defer |
| X4 / N4 freshness | X4 PARTIAL (Y1), N4 CLOSED | CLOSED wording, but C2 contradiction | **P0-BLOCKING, agreed** (Y1 + C2) |
| X5 digest | CLOSED | CLOSED (fixture gaps; URL residual C10) | **CLOSED**; add `read_digests` + web consumer fixtures |
| X6 sources | CLOSED | PARTIAL (C3: stored vs effective quick values) | **PARTIAL.** C3 is a real semantic gap; text fix now, enforced by P1 |
| X7 events | CLOSED | CLOSED (C6: no outcome writer for no-output endings) | **CLOSED for the cursor**; C6 before P2 |
| X8 manual | CLOSED | CLOSED | **CLOSED, agreed** |
| X9 drift | PARTIAL (Y5) | PARTIAL (C11) | **PARTIAL (Low), agreed**; same items |
| Key names | Correct | Correct, but `:?` defect (C1) | **Correct**; switch `:?` → `:-` + in-container fail-closed (C1) |

## 3. Agreements (both reviewers)
- **Key names are right:** `JARVISE_HERMES_OPENROUTER_KEY` remaps compose `:457`, and `JARVISE_HERMES_API_KEY` (`:456`, `:511`, `app.py:55`, runbook step 3) is untouched. The v5 rename suggestion is gone.
- **X5, X7 (cursor), X8 closed.** X6 columns match `db.py`; `stop_warning` matches `watch.py:647-696` / `store.py:996`.
- **The one P0 blocker is freshness** (Y1 = C2): it is a design-text fix.
- **Budget authenticity and coverage** (Y2 ≈ C4 + C5 + C12) must be fixed before the first agents-key spend, not before P0.
- **Incarnation** (Y4 ≈ C7) is overclaimed but can be deferred because of `same_observation`.
- **Doc drift** (Y5 ≈ C11): `consumer=` missing in examples; signal-plan vs position `target` wording.
- Decision fidelity OK (10:41, 09:23, 08:28, 08:08 a/b/c, D-1…D-11, t252u). No Trader strategy content, no unapproved scope, zero PR diff to execution/signals/sizing/schema.

## 4. Disagreements and how I resolve them
| Topic | Codex | Claude | Resolution |
|---|---|---|---|
| Overall label | NOT-READY | READY-WITH-CHANGES | Both say one P0 text fix. I treat v5.1 as **NOT READY FOR P0 until v5.2 lands Y1/C2**, then GO for P0 |
| X1 late worker (Y3) | open, P1-gated | not raised | **Accept Y3**: add a final token + lease + policy check immediately before HTTP, and a suspend-across-lease/night test. P1-gated |
| X6 effective values (C3) | not raised | open, P1-gated | **Accept C3**: serve `get_quick_config(conn).values` read-only with `clamped`/`defaulted` flags, use `effective_quick_values` for risk, drop the inactive A2 keys. Fix the text now (cheap) |
| `:?` compose interpolation (C1) | not raised | High | **Accept C1** (my v5.1 text introduced `:?`): use `:-`, refuse an empty key inside the container, and add a CI `compose config` run with the key unset. Before the follow-up |
| No-output event outcome (C6) | not raised | P2 | **Accept**: idempotent `POST /jobs/agent-event/{seq}/outcome` or a jobs sweeper |
| `assist_signal.id` vs §7.0 `signal_id` (C8) | not raised | P2 | **Accept**: define the join key (stored ids are `as_<uuid16>`) |
| Digest URL text (C10) | "only URL/day remain; permitted" | optional Chana decision | **Recommend (b)** strip query and fragment and show the domain as link text, as a mechanism fix under 08:28. Ask Chana only if a confirmation is wanted |

## 5. Recommended must-fix list

### Before P0 (v5.2 design text; blocks P0 coding)
1. **Freshness and bar timing (Codex Y1 + Claude C2):**
   - define the expected closed bar **per timeframe** (`15m|1h|4h|1d`): closing timestamp, the open→close conversion and the publication lag. Don't use `sizing.py:392` directly, because it returns the **opening** time of a **4h** analysis bar. Existing analyzer behaviour stays unchanged;
   - **per-role freshness** in the derived-rule registry: `change_sign.earlier` is checked by order against a fresh `later` (not "latest bar"); for `age_bucket`/`job_overdue`, freshness applies to the read (serve time) and the age/overdue state is the output, not a reason to reject;
   - define in `grounding_fields` whether each field's `ts_ms` is observation time or source time.
2. **Bundle the cheap text fixes in the same v5.2 pass** (none block P0 alone):
   - C3: effective quick values plus flags; drop inactive A2 keys;
   - C7/Y4: incarnation = `entry_time_ms` only (NULL → `unavailable` for joins); `same_observation` mandatory on every position phrase; claim only observation-level identity;
   - Y5/C11: `consumer=` in every example; consistent "position target"; `side` enum case; retention wording; brief self-references; `claim` in implementer §4;
   - C1: replace `:?` with `:-` plus in-container fail-closed (text now; implemented in the follow-up).

### Before the PR #397 follow-up (first agents-key spend)
3. **Verifiable admission (Y2 + C4):** `claim` returns a short-lived signed receipt bound to `(reservation_id, caller, destination adapter, profile, run_ref, attempt, caps, exp)` under a new fail-closed signing key shared by jobs and the adapters only. The adapter verifies it and consumes it durably before forwarding. Tests: forged, unclaimed, wrong-adapter and replayed receipts. Void timer > max run deadline.
4. **Coverage (Y2 + C12):** keep worst case until coverage is proven by provider-side usage for the limit's own period; Hermes-reported `actual_usd` doesn't lower liability before coverage; measure `PROVIDER_LAG` as an acceptance gate.
5. **Cap proofs (C5):** add `max_tokens` incl. reasoning, retry count R, `max_turns`, input cap and delegation to the pin gate, and run it for the `hermes` digest profile in the follow-up (before first spend).
6. **C1** implemented (`:-`, container refuses empty key, `compose config` CI with the key unset).
7. **C10** digest URL display rule (recommended option b).

### Before P1
8. **Y3** final pre-HTTP token/lease/policy check + suspension tests.
9. **C9** sender details (401/404 → `Rejected`; bounded `NotSent`; reset `preflight_since_ms`; explicit validation-time `blocked` row).
10. **C3** enforced in code (text fixed in v5.2).

### Before P2
11. **C6** outcome writer for events that end without agent output.
12. **C8** signal join key.

## 6. Decisions for Chana
- **Required:** none. All items are corrections within approved scope (10:41, 09:23, 08:28, D-2).
- **Optional:** C10 digest URL text. Recommended default (b): link text = domain; query/fragment stripped.
- **Suggested next step (needs Chana's OK):** v5.2 = items 1–2 (P0 text) plus 3–7 written into the follow-up contract; then a short confirmation review of the delta only.

## 7. Process notes
- Both reviewers ran read-only on the Mac under `caffeinate -i`. Neither hit a rate limit (both exit 0, empty stderr).
- Claude was run as `~/.local/bin/claude -p --permission-mode plan --output-format text` with an allowlist of read-only tools (`Read`, `Grep`, `Glob`, `git show/diff/log/grep`, `rg`, `ls`, `cat`, `sed -n`, `wc`, `head`, `diff`).
- No push, PR, VPS or `.env` action. No secrets read. Process checks used only `pgrep -f <marker>`.
