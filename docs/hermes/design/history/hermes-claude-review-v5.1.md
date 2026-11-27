**Claude Code co-review and safety review of Hermes design v5.1 (read-only, round 6)**

1. **VERDICT: READY-WITH-CHANGES.** v5.1 closes X1, X3, X4/N4, X5, X7 and X8 at design level and gets the key names right. One P0 contract contradiction is left (C2); the other gaps only matter for phases after P0.
   **P0-START: GO-WITH-CHANGES.** Land the C2 text fix before P0 coding starts.

A = `hermes-architecture-v5.1.md`, I = implementer brief v5.1, C = co-review brief v5.1. Refs checked at `origin/main` 438e687 and PR #397 a5967aa.

2. **X CLOSURE:**

| ID | status | evidence | remaining gap |
|---|---|---|---|
| X1 | CLOSED | A:663-706. Held rows move to `queued` inside the policy transaction. Blocked replies become template rows. Pre-flight retries every 5 min and fails after 30 min without using an attempt. `NotSent` is returned only before the HTTP call and is token-fenced. An expired lease becomes `send_unknown`. The zero-send guarantee is withdrawn (A:702-705). | I walked held wake, blocked delivery, pre-flight expiry, `NotSent`, a double crash after reservation and a late worker. No path exceeds 2 transmissions. `NotSent` can't double-send, because attempts always ≥ transmissions. Minor items: C9. |
| X2 | PARTIAL | A:823-829. Admission runs in one `BEGIN IMMEDIATE`. Reservations are UNIQUE `(caller, run_ref, attempt)` and claimed once; digest attempt 2 needs a new one. Unconfirmed runs stay at worst case. A row is covered only after a probe read ≥ terminal + 60 min. Callers with unprovable caps stay disabled. | The adapter checks uniqueness, not authenticity (C4). The pin gate doesn't list the cap proofs and runs after the first digest spend (C5). `PROVIDER_LAG` is unmeasured and has no acceptance gate. |
| X3 | CLOSED | A:385, 514-539, 529, 570: ordered roles, `same_observation`, `proposal_target` (alias, target_ref, amount from the preview row), exact source equality. | `entry_time_ms` is a sound discriminator: it is preserved by COALESCE (`store.py:932`) and reset on close or side flip (`watch.py:444-451`). `entry` changes on same-side adds (`watch.py:446-448`), which only causes false splits (C7). |
| X4/N4 | CLOSED | A:589-594 matches v4:512 word for word. It applies to every used claim, at validation and at send (A:665-668). | Applying it to derived roles breaks three rules: `change_sign`, `job_overdue`, `age_bucket` (C2). |
| X5 | CLOSED | Checked `digest.py:214/232/479`, `news_sources.py:345/392` and `news_reaction.py:16/166-170/195`. The composed title line parses. The sidecar is written before the `.md` link. Hermes items are label-only. | No agent string except URL and day reaches Home or the report. The fixture list misses `read_digests` (`news_sources.py:128`) and the web consumer (`marketContextRepository.ts:72-76`). URL path residual: C10. |
| X6 | PARTIAL | Every column matches `db.py:670/692/804/1649/1695/1727`. `stop_warning` is correct against `watch.py:647-696` and `store.py:996`: the latch is set only after a sent warning, and clearing it drops the advice rows. | The snapshot serves stored quick-config values, not the effective values Jarvise uses (C3). |
| X7 | CLOSED | A:766-778. The cursor walks existing rows. SQLite's single writer guarantees commit order. Retention writes `expired` before deleting. One consumer, `n8n_notice`. | No endpoint writes an outcome when an event ends without agent output (C6). Doc drift (C11). |
| X8 | CLOSED | A:639-640, 651, 1152; P0 test h4 at A:1169. No outbox table exists in P0. | The preview route takes the reply token only, so name who runs Chana's staging read. Manual sessions still need a `run_id` (CAN-DEFER). |
| X9 | PARTIAL (Low) | `after_seq`/`consumer`, ETL token (A:1012), pure vs I/O sender split (A:661, I:140-143, C:136). | Residual drift: C11. |

3. **KEY NAMES:** Correct. In PR #397 compose, `:456` is `API_SERVER_KEY=${JARVISE_HERMES_API_KEY:-}`, `:457` is `OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}`, and `:511` passes `JARVISE_HERMES_API_KEY` to the adapter. `app.py:55` reads it as the gateway password. Runbook step 3 (`hermes-stage-a.md:104`) and `:20` match. v5.1 (A:800-803, A:1125, I:65-66) remaps only `:457` and leaves `JARVISE_HERMES_API_KEY` untouched. The v5 rename suggestion (old §18e) is gone; `OPENROUTER_API_KEY_AGENTS` survives only in the historical §18a table. One defect: the `:?` form (C1).

4. **PARTIAL CLOSURE:**

| ID | status | evidence | remaining gap |
|---|---|---|---|
| W2, W3, V2, V3, N1, N2, N5 | CLOSED | via X1, X3, X5, X8 | none |
| W1, V1 | CLOSED (mechanism) | A:513-539 | C2 |
| W5, V5 | PARTIAL | A:766-778 | C6 |
| W6, V6 | PARTIAL | A:416-436 | C3 |
| R2 | PARTIAL | A §7.1, §7.3 | C2, C3 |
| R5 | PARTIAL | A §7.6, §7.8 | C4, C5, C6 |
| W9, V8, N9 | PARTIAL (Low) | cross-doc | C11 |

5. **DECISION FIDELITY:**
- **10:41 OK:** key name, `JARVISE_HERMES_API_KEY` not renamed, parallel review, no v6 content.
- **09:23 OK:** evidence-only enums, Stage C deferred, doctrine deferred.
- **08:28 OK:** typed output and Jarvise rendering. The new `stop_warning` Thai phrases still go through catalog approval.
- **08:08-a/b/c OK:** inventory and parity before removal; existing `pa:dy`/`pa:ry`/`pa:oy` flows; S4 worker prohibited.
- **D-1 to D-11 OK.** D-2 enforcement is PARTIAL (C4, C5).
- **t252u OK.**
- **07:39 also-items OK:** M7/M9 and Tester night-hold tests present (P1 test e). No Trader universe, EMA or one-at-a-time rule adopted.
- **Scope:** nothing added without approval. The sidecar, the subscription table and the claim endpoint are mechanism fixes inside existing approvals.

6. **NEW FINDINGS:**

**High**
- **C1: `${JARVISE_HERMES_OPENROUTER_KEY:?}` in the shared `docker-compose.yml`** (A:801, I:65).
  - *Evidence:* main has no `:?` anywhere today. `vps-deploy.sh:27`, `backup.sh:12` and `run-ingest-host.sh:13` all run compose with `-f docker-compose.yml`.
  - *Why it matters:* as far as I know, Compose interpolates the whole file before applying profiles (unverified on the VPS Compose version). If so, an empty or unset key aborts main-stack deploys and backups. That would happen right after merge, because Chana sets the key only after the hard limit.
  - *Fix:* use `:-`. Fail closed inside the container instead (the entrypoint or `provider_ready()` refuses an empty key), plus a CI `compose config` test with the key unset.
  - **PHASE-GATED (PR #397 follow-up).**

**Medium**
- **C2: Freshness contradicts the derived rules** (A:529-532 vs A:589).
  - *Evidence:* the `earlier` role of `change_sign` is never the latest expected bar, so it always fails `bar` freshness. A `job_overdue` heartbeat or an `age_bucket` fact old enough to be "overdue" or "stale" is rejected as `stale` first. `fact_times.ts_ms` has no defined meaning per field, and the health phrases ship in pc0.
  - *Fix:* add per-role freshness to the derived-rule registry. `earlier` is checked by order against a fresh `later`. For `age_bucket` and `job_overdue`, freshness applies to the read (serve time), and the age is the output. Define in `grounding_fields` whether each field's `ts_ms` is observation time or source time.
  - **P0-BLOCKING** (text only).
- **C3: Snapshot serves stored, not effective, quick values.**
  - *Evidence:* `get_quick_config` (`quick_config.py:693`) fills defaults for missing keys and clamps out-of-range values. `effective_quick_values` (`:755`) then takes min(risk, cap). The design serves raw values, and missing keys become `unavailable`. `quick_time_stop_*` are A2 settings marked "ยังไม่ใช้" (not active yet).
  - *Why it matters:* the agent could state risk values Jarvise doesn't enforce.
  - *Fix:* serve `get_quick_config(conn).values` (it is read-only) with `clamped`/`defaulted` flags. Drop the A2 keys or label them inactive.
  - **PHASE-GATED (P1)**, since pc0 has no settings phrases. Fixing the text now avoids rework.
- **C4: Ingress single-use check can't verify the reservation is real** (A:826).
  - *Evidence:* the adapter ledger checks only that the id is present and unused. It can't see jobs, so an n8n bug that skips reserve/claim, or sends an invented `br_` id, passes. An unclaimed reservation becomes `void` at 10 min, which equals the digest's 600 s deadline.
  - *Fix:* `claim` returns a short-lived HMAC token over `(reservation_id, caller, run_ref, attempt, exp)`, using a new fail-closed key shared by jobs and the adapters. The adapter verifies it before forwarding. Set the void timer above the maximum run deadline.
  - **PHASE-GATED (PR #397 follow-up).**
- **C5: The pin gate doesn't include the X2 cap proofs** (A:1102-1112 vs A:822).
  - *Evidence:* missing items are `max_tokens` including reasoning, the retry count R, `max_turns`, the input cap and delegation. The gate is scheduled "before hermes-core", which is after the digest's first spend.
  - *Fix:* add these items, run them for the `hermes` digest profile in the follow-up, and add a measured `PROVIDER_LAG` acceptance gate.
  - **PHASE-GATED (PR #397 follow-up).**
- **C6: No writer for outcomes when an event ends without agent output.**
  - *Evidence:* run timeout, budget denial, 30-min admission expiry and the ops template all need an outcome. A:776 says the outcome "is written", but no endpoint or sweeper does it. The cursor stalls until 30-day retention.
  - *Fix:* add an idempotent `POST /jobs/agent-event/{seq}/outcome` (reply token; 409 if an outcome exists), or a jobs sweeper.
  - **PHASE-GATED (P2).**

**Low**
- **C7: `position_incarnation` includes the mutable `entry`.** Use `entry_time_ms` only; NULL → `unavailable` for joins. Also document that a close and reopen within one watch interval is invisible to main code. **CAN-DEFER.**
- **C8: `assist_signal.id` doesn't match the §7.0 `signal_id` format.** Stored ids are `as_<uuid16>` (`store.py:526,596`), and the table has no tf or candle-close columns. So `signal.created` events can't join snapshot signals. Define the join key. **PHASE-GATED (P2).**
- **C9: Sender details.**
  - Treat Telegram 401/404 (any non-429 4xx) as `Rejected`.
  - Bound repeated `NotSent` returns, so a render that differs from the pre-flight render can't loop.
  - Reset `preflight_since_ms` when pre-flight passes.
  - Validation-time `blocked` should insert a `purpose=blocked` outbox row explicitly (A:617).
  - **CAN-DEFER (P1 tests).**
- **C10: Digest residuals.**
  - The URL path and query are agent-chosen visible text; only the domain is policy-checked.
  - `read_digests` assigns themes by keyword, so the new categories get no stats.
  - The sidecar has no replay rule for "sidecar already exists" on retry.
  - **PHASE-GATED (follow-up)** for the URL decision; the rest **CAN-DEFER.**
- **C11: Doc drift.**
  - A:771 and the flow at A:273 omit `consumer=`.
  - A:765 contradicts A:775 on retention.
  - The registry example says `position.side ∈ LONG|SHORT`, but A:430 maps `side` to `long|short`.
  - The co-review brief says at :3 that it replaces itself, and at :136 cites "implementer brief v5".
  - Implementer brief §4 (:161) omits `claim`.
  - **CAN-DEFER.**
- **C12: Liability can be understated before coverage.** It uses Hermes-reported `actual_usd`, which may leave out SDK retries. Keep the worst case until covered unless the cost comes from the provider. The probe must use usage for the limit's own period, not all-time usage. **CAN-DEFER (follow-up).**

7. **CONVERGENCE:**

| item | class | reason |
|---|---|---|
| C2 | P0-BLOCKING | P0 builds the validator and `derived_rules.py`; the contract contradicts itself |
| X2 residual, C1, C4, C5, C10 (URL) | PHASE-GATED (PR #397 follow-up) | they matter only once the agents key is spent |
| C3, C9 | PHASE-GATED (P1) | P1 is the first phase where settings phrases and Telegram sends exist |
| C6, C8 | PHASE-GATED (P2) | events don't exist before P2 |
| C7, C11, C12, X8 run_id/preview access, C10 (other) | CAN-DEFER | the implementer can settle these under the existing contract |

8. **OPEN DECISIONS FOR CHANA:**
- **C10, digest URL text:** (a) accept the URL as an agent-chosen residual, like `day`; or (b) strip query and fragment and show the domain as link text, keeping the full URL only as the link. **Recommended: (b).**

9. **GATES:**
- **P0:** GO once the C2 text fix lands (fixing C3/C11 text at the same time is cheap).
- **PR #397 follow-up:** C1, C4 and C5 fixed; `PROVIDER_LAG` measured; existing acceptance kept; Claude safety review of the shipped SHA.
- **P1:** C3 and C9; pin gate including the caps; OpenClaw removal; v6 catalog; 18/20 plus readability approval; crash matrix.
- **P2:** C6 and C8.
- **Deploy:** Chana's explicit OK; both RAM gates with LearningYard measured; no deploy :20–:27 BKK; LearningYard check after every deploy; `compose config` passes with the agents key unset.

10. **COMMANDS RUN:**
- `ls`, `wc -l`, `grep -n` and `sed -n` on the notes; `diff` v5 vs v5.1.
- `git log -1 --oneline` for both refs.
- `git show origin/feat/hermes-stage-a:` for `docker-compose.yml`, `src/jarvise_intel/app.py`, `src/jarvise_intel/digest.py`, `docs/ops/hermes-stage-a.md` and `.env.example` (names only).
- `git show origin/main:` for `db.py`, `watch.py`, `store.py`, `quick_config.py`, `sizing.py`, `telegram.py`, `news_sources.py`, `news_reaction.py`, `web/src/lib/api.ts` and `web/src/repositories/marketContextRepository.ts`.
- `git grep` on origin/main for compose invocations, the `assist_signal` id format and `digest_items` consumers.
- No files changed, no network, no tests run. I didn't write a plan file, because the brief made this review strictly read-only.

11. **SAFETY REVIEW: APPROVE.** The design breaks no hard rule. Agents still emit no prose, never send and never move money. The remaining risks (C1, C4, C5) are gated before any spend on the agents key, and C2 is a text fix before P0.
