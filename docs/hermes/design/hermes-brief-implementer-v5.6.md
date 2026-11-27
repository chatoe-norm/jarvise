# Implementer brief v5.6: Jarvise × Hermes Agent Team (Cursor/Grok, phase by phase)

> **v5.6 (Chana 12:11):** budget only: AC1 late discovery charged to the active period with provenance, rollover sets `restored_debt_usd` = carried debt, admission adds the active value only; duplicate = skip the whole operation; `unknown` keeps all liabilities and never overrides a drop. **P0 implementation still waits for Claude's design review after 2026-11-30 02:00 BKK.** Replaces `hermes-brief-implementer-v5.5.md`.
>
> **v5.5 (Chana 12:04):** budget only: AB1 re-opened debt, AB2 high-water admission floor + refuse on decrease, AB3 recorded credit + maturity fixture. **P0 implementation does not start until Claude's design review after 2026-11-30 02:00 BKK.** (history)
>
> **v5.4 (Chana 11:54):** AA3 quick flags (`clamped_keys`/`defaulted_keys` + read-only missing-key check), AA4 (compose `${VAR:-N}` only where N equals the registry default), AA1 aggregate coverage release, AA2 `budget_probe_read`/`budget_period`/`budget_allocation` in one transaction. S-1 and the settings rule unchanged (Hermes numbers only). (history)
>
> **v5.3 (Chana 11:25):**
> - Freshness is checked **per use**: a dual-use claim passes every check (Z1).
> - The §7.11 registry is complete, with named acceptance keys and cross-checks (Z2).
> - Existing trading constants, including the main night hold `signals.py:110-114`, are **read-only references, never copied** (S-1).
> - Coverage is proven by FIFO allocation of real usage (Z3).
> - The fence checks the actual remaining lease (Z4).
> - The effective attempt limit uses `notsent_count`/`max_attempts_at_reserve` (Z5).
> - The quick flag projection is explicit (C3).
>
> (history)
>
> **v5.2 (Chana 11:05):** freshness per timeframe and per derived role (Y1/C2), effective quick values (C3), `entry_time_ms` incarnation (C7), compose `:-` with fail-closed services (C1), digest domain links (C10), admission receipts/coverage/cap proofs (Y2/C4/C12/C5), pre-HTTP send fence and sender details (Y3/C9), event outcomes (C6), signal join key (C8), and **the standing rule: no hard-coded numbers; read every number from the §7.11 settings registry**. (history)
> **v5.1 (Chana 10:41):** fixes Codex r5 X1–X9, restores v4's freshness definitions, and sets the agents key name to `JARVISE_HERMES_OPENROUTER_KEY` (`JARVISE_HERMES_API_KEY` stays the adapter↔API-server password, unchanged). Replaces `hermes-brief-implementer-v5.md`.
> v5 note: replaced `hermes-brief-implementer-v4.md` (kept for history). Same playbook (Chana D-6). v5 applies Chana 09:23: fix W1–W9 with Codex's defaults (evidence-derived enums only, Stage C deferred until LearningYard RAM is measured, doctrine deferred). **The Trader strategy spec is v6 and not part of this brief.** All 08:08 and 08:28 decisions stay.

```
OWNER APPROVAL (Chana fills in before this prompt is run; default is NO):
APPROVED PHASE: <PR397-FOLLOWUP (budget probe + key switch + source-policy v2 + allowlist + vps-deploy + runbook step-8 edit) | OPENCLAW-INVENTORY | OPENCLAW-REMOVAL | P0 | P1 | P2 | P3a | P3b | P4 | P5 — exactly one>
APPROVED BY CHANA ON: <date/time Asia/Bangkok>
```
- If the lines above are empty or name a different phase, **only read and plan**.
- One phase per run. Never start the next phase yourself.
- **Local commits only.** Never `git push`, set an upstream, open a PR, merge, run a VPS command, run `docker compose` on a server, or edit `.env`. Stop and ask Chana. Agents' approval is not approval.

## 0. Context
- **Repo and paths:**
  - Repo `chatoe-norm/jarvise`; MacBook checkout `/Users/gizmo-macbook/Public/Projects/jarvise`.
  - Never reset or check out the local `main` (it is intentionally stale). Base new branches on `origin/main`.
  - Worktree: `/Users/gizmo-macbook/Public/Projects/jarvise/worktrees/hermes-<phase>`, branch `hermes/<phase>-<slug>` from current `origin/main`.
- **Design contract (source of truth):** `../.jarvise-notes/hermes-design/hermes-architecture-v5.6.md` (§7 contracts, §9 hard rules 1–19, §10 security, §11 RAM, §12 phase gates, §14 decisions, §17 Chana decisions incl. 08:08, 08:28 and 09:23). If code on your base contradicts it, **stop and ask**. Don't invent contracts.
- **Verify every file:line** on your base before use. The design was read at `origin/main` = 438e687 (schema 39). PR #397 (`origin/feat/hermes-stage-a` @ a5967aa) is OPEN; if your phase depends on it, branch from what Chana tells you.
- **Goal:** LLM agents on Hermes Agent (Nous Research), pinned image `nousresearch/hermes-agent@sha256:3bc37fd8546e90fc41fe0d29511b4052840c281617cbcec6093bfe03d86a4ad9` (v0.23.4). Two runtimes:
  - `hermes` (PR #397 digest + researcher; `web` via the allowlisted proxy);
  - `hermes-core` (trader, watcher, accountant, manager, orchestrator; no web; egress `openrouter.ai` only; reachable only through the one-way `core-ingress` adapter).
  - n8n is glue.
- **Principle:** money/risk/orders/signals/ledger are deterministic Jarvise Python. Agents only read, explain, research and propose. Only Jarvise sends Telegram.

## 1. Hard rules (verbatim from architecture v5.6 §9; every commit)

1. `JARVISE_LIVE_TRADING` stays off. No PR touches its default or the code that reads it.
2. **Main signal rules unchanged:** `signals.py` scan logic, analyzer floors 0.55/0.70, the analyzer, `same_side_hold`, the 1.5× ATR stop, `flat_exit` and the universe.
3. `src/jarvise_assist/execute.py` has **zero diff** vs the PR base (`git diff --quiet origin/main -- src/jarvise_assist/execute.py`).
4. No orders, trade client or Redis in new agent code. AST import ban on `jarvise_assist.execute`, `jarvise_trade`, `redis` and `jarvise_assist.futures.build_futures_client` (aliases, `from` imports, submodules). No `quick*.py` behaviour change. **Quickfeed never uses the main Redis.**
5. `JARVISE_QUICK_EXECUTE`, `JARVISE_ASSIST_REDUCE` and `JARVISE_ASSIST_CANCEL_OWNER` stay **OFF**; a test asserts they are unset/OFF in every agent container's env. All new flags default off: `JARVISE_AGENT_API`, `JARVISE_AGENT_ASK`, `JARVISE_AGENT_EVENTS`, `JARVISE_AGENT_ACCT`, `JARVISE_HERMES_SMOKE`.
6. Live risk settings unchanged: Main 3%, Quick 0.5%, max 5x, 2R, night hold 23:30–07:30 Asia/Bangkok, compose caps. Agents may *explain* them and never *propose changing* them. `POST /api/risk/caps` is never exposed to agents.
7. **Partner:**
   - Agents never move money or write the ledger. Proposals are recomputed by Jarvise and confirmed by Chana through the existing per-kind flow (`pa:dy` deposits, `pa:ry` requests, `pa:oy` owner previews; 08:08-b) with `guard_check`.
   - **Never read `partner_kyc*`** (static gate + runtime authorizer); no national-ID or photo fields; never DELETE/UPDATE `partner_kyc` rows.
   - `partner_*` changes only within approved scope. Referral/commission/dividends are out (D7).
8. Never run unmerged code against the production ledger. Use `sqlite3 data/analytics/jarvise.db ".backup /tmp/jv-copy.db"` and point `JARVISE_DB` at the copy. Mutation testing runs only on a `git archive` copy.
9. **Secrets:**
   - No Binance keys of any kind, no Jarvise bot token and no `JARVISE_JOBS_TOKEN` in Hermes, n8n agent workflows, jarvise-mcp or research services (CI compose test).
   - No secrets in code, PRs, logs, notes or handoffs. Never read or print `.env`; agents report env vars as `set`/`EMPTY` only.
   - New tokens are fail-closed and distinct from Basic auth and the jobs token. New env names go into `.env.example` commented out, names only.
10. **Tests:** `unset` every `BINANCE_*` for every run. A network guard means no real Binance, Telegram, OpenRouter or VPS calls. No live LLM in CI.
11. **Telegram:**
    - **One sender (Jarvise).** No Hermes Telegram platform and no backup bot (D-7).
    - `callback_data` ≤ 64 bytes. No `ลองใหม่` on confirm paths (including P4 proposal cards). Thai copy is short, through `fit_telegram`, with the footer generated by Jarvise.
    - The agent's Telegram Web use is read-only.
12. **Git and deploy:**
    - No push, PR, merge, deploy, VPS action or `.env` change without Chana's explicit OK.
    - Implementers (Cursor/Grok, `cursor-agent`) make **local commits only**. Push/PR/squash-merge only as `chatoe-norm` via `~/bin/gh`, with `/Users/gizmo-macbook/Public/Projects/jarvise/.venv/bin` on `PATH` for the pre-push hook. `normstudiox` is pull-only.
    - Worktrees only under `/Users/gizmo-macbook/Public/Projects/jarvise/worktrees/<name>`. Never reset the local `main` checkout.
13. **Never merge or deploy during :20–:27 Asia/Bangkok** (the `ASSIST_HOURS` minute-25 cadence, `sizing.py:26-27`). Wait for CI/Deploy idle (`wdpy`).
14. **After every deploy:** run the LearningYard isolation/leak check (§12 "LearningYard check") and the RAM gate (§11).
15. **VPS** access is read-only for agents (`docker exec -i jarvise-jobs-1`, SQLite `mode=ro`). Only Chana runs `docker stats`, `.env` edits and `compose up`.

16. (v4) Agents emit no user-facing prose. All Thai is rendered by Jarvise from the versioned phrase catalog, using session-bound evidence rechecked at send time. At night only the night template + `night_facts` are sent for trader/accountant.
17. Agents never create partner items or ledger writes. Proposals only attach to existing pending items, through the existing `pa:dy`/`pa:ry`/`pa:oy` handlers.
18. No spend on `JARVISE_HERMES_OPENROUTER_KEY` (the agents key) before the provider hard limit and the budget probe are live.
19. (v4) The Hermes path (jarvise-mcp → web agent routes) makes no HTTP call to jobs and no Redis read; agent routes read SQLite only.


**Key names (Chana 10:41, architecture v5.6 §7.8):**
- The agents OpenRouter key is **`JARVISE_HERMES_OPENROUTER_KEY`** (VPS `.env`, set by Chana). `hermes` (`docker-compose.yml:457` on PR #397) and `hermes-core` map it as `OPENROUTER_API_KEY=${JARVISE_HERMES_OPENROUTER_KEY:-}` (the in-container name the image reads; **never `:?`** in the shared compose, C1); an empty key makes `hermes-ingest` answer `503 provider_key_missing`, `core-ingress` refuse unless the budget state is `ok`, and jobs deny admission until the probe has read `ok`; `hermes-ingest` gets it under its own name for the probe only. No Hermes-side service interpolates `${OPENROUTER_API_KEY}`.
- **`JARVISE_HERMES_API_KEY` is unchanged:** it stays the adapter↔Hermes API-server password (`:456` `API_SERVER_KEY`, `:511`, `src/jarvise_intel/app.py:55`). Don't rename it and never use it as a provider key.

## 2. Contracts you implement exactly (details in architecture v5.6 §7)
- **IDs (§7.0):**
  - `ask_id`, `session_id`, `evidence_id`, `notice_id` and outbox `message_id` as in v4.
  - `event_seq` = `agent_event.seq` (the cursor).
  - `catalog_version = "pc<N>"`, with `catalog_sha256` stored separately in `agent_catalog`.
  - v5.1/v5.2: `position_incarnation` = `sha256(symbol|position_side|entry_time_ms)[:16]` (NULL → `unavailable`; observation-level only), `admission_receipt` (HMAC, §7.0), signal join key = stored `assist_signal.id`, `reservation_id` (UNIQUE `(caller, run_ref, attempt)`), event `consumer` (`n8n_notice` only).
  - `settings_version` = the shared pure function over `assist_settings{risk_pct, enabled}`, `assist_quick_settings{day_max_loss_r, streak_lock_n, locked_until_ms}`, the effective quick values + flags (C3) and the constants-registry version (SQLite only).
- **Read API (§7.1):**
  - GET only, behind a flag, with fail-closed tokens; `mode=ro` + the `partner_kyc%` authorizer.
  - **No jobs HTTP and no Redis** (rule 19): health comes from `agent_job_heartbeat`; doctrine is not built.
  - **Exact SQLite sources only** (§7.1 table). **Removed:** `caps` (Redis), `levels`/`level_version`, `r_now`, mark, the generic snapshot `target` field (the proposal's `target_ref` is separate and kept), `leverage`, `pause`, and any `paper_*`.
  - **v5.1 exact column map (X6):** implement the §7.1 table literally (tables, columns, types, filters, excluded columns; v5.2: `quick_effective` via `effective_quick_values(get_quick_config(conn))` with `defaulted`/`clamped` flags, no raw config rows, no A2 keys, no leverage; `ts_kind` per field). Enum normalization map + DISTINCT-value test; unmapped → `unavailable`. Stop if a column differs on your base.
  - Null → `unavailable`. `initial_stop` is labelled as the initial stop. **`stop_warning`** (`missing|partial|unknown|none`) from `no_stop_latched` + `latest_no_stop_reason` (`store.py:996`); never render "no stop exists".
  - Every agent route requires `X-Agent-Session`.
  - Events use `JARVISE_AGENT_EVENTS_TOKEN` (`?consumer=n8n_notice&after_seq=`); ETL chart pulls use `JARVISE_AGENT_ETL_TOKEN` (never the read token). Neither needs a session.
- **Sessions (§7.2):**
  - `kind` ∈ `manual` (P0) | `ask` (P1) | `notice` (P2; bound to an `event_seq` with no consumer outcome). An unsupported kind → 501.
  - One open session per profile; 429 `{retry_after_ms}`; busy at 120 s; ≤ 3 pending asks; quarantine after an unconfirmed close.
  - Foreign, closed or expired session → 409 with no evidence row.
- **Structured output (§7.3):**
  - **Envelope:** one discriminated `agent_output.v2` (`source.type` ∈ `manual|ask|event`). It requires `catalog_version` and a `run_id` equal to `session.run_id`, and allows an optional `proposal{kind, partner_alias, target_ref, amount:{claim}, reason_code}`.
  - **Claims:** typed values (`number` as a canonical decimal, `enum`, `time`, `bool`); unique ids; every claim referenced. Citations must be among the claims' evidence ids; research_doc citations are rejected.
  - **v5.1 (X3):** exact source equality (`ask_id`/`event_seq`/`correlation_id` = the session's); joins add `position_incarnation`, `same_observation` (**mandatory on every position phrase**, catalog CI), `proposal_target` (preview row + alias + target_ref + amount; alias-bound `nav_reconciled_ok`); `change_sign` uses ordered `earlier/later` roles (`later.bar_open_ms − earlier.bar_open_ms` a positive multiple of `INTERVAL_MS[tf]`).
  - **v5.2 freshness (Y1/C2):** classes `live` (≤ `live_max_age_min`), `live_until_expiry`, `bar` (`bar_open_ms ≥ expected_closed_bar_open_ms(tf, now)` with `INTERVAL_MS[tf]` and `bar_ingest_lag_ms[tf]`, and the bar closed), `analysis_bar` (existing `expected_analysis_bar_ms`, reused for 4h analysis rows only), `ordered`, `read`, `setting`, `history` (needs a `time` slot). A claim filling a derived role uses the role's mode (`change_sign.earlier` = `ordered`; `age_bucket`/`job_overdue` = `read`, age is the output). **v5.3 (Z1): checked per use.** A direct slot uses the field class; each derived role uses its mode; every use must pass. Checked at validation and at send. P0 records the measured ingest lag per tf.
  - **Catalog:** phrases have `class` ∈ `fact|derived|rule|caveat` (**no opinion**), `joins`, typed derived roles and `rule_const`s. The reason-code registry carries evidence predicates. The derived-rule registry holds the 4 v5 mechanism rules.
  - **Validation codes:** `schema`, `catalog_version`, `session_binding`, `run_binding`, `foreign_session`, `unknown_phrase`, `phrase_not_allowed`, `unknown_slot`, `missing_slot`, `slot_type`, `evidence_mismatch`, `value_mismatch`, `unavailable`, `join_mismatch`, `derived_mismatch`, `role_type`, `reason_predicate_failed`, `orphan_claim`, `citation_unbound`, `stale`, `night`.
  - **Renderer:** pure, at send time, with catalog + settings snapshot + delivery facts.
- **Manual (§7.4, X8, P0):** `POST /jobs/agent-manual-output` → atomic validation → `agent_manual_result`; `GET /jobs/agent-manual-result/{id}` returns a render-on-read preview. **No outbox row, never sent.**
- **Delivery (§7.4, v5.1 X1):**
  - Validation + result + outbox (+ the event consumer outcome) commit in one transaction.
  - **Sender loop:**
    1. select due `queued|held|send_unknown` rows;
    2. pure `decide()` first, consuming no attempt: full catalog acceptance, freshness of every used claim, settings version, night/class, **pre-flight** (credentials, render). Held rows that pass → `held → queued` in that transaction; stale/revoked reply → deliverable `blocked` template (stays `queued`); stale notice → `dropped` (ops → ops template); pre-flight failure → retry in 5 min, `failed_preflight` after 30 min;
    3. fenced reservation (`attempt_token`, `attempts < 2`, lease) on `queued|send_unknown`;
    4. render;
    4a. **pre-HTTP fence (Y3):** token still owned, lease margin left, `decide()` still `continue`; else `NotSent(fence)`;
    5. `send_message_ex` → `Sent | Rejected | Ambiguous | NotSent` (`NotSent` only before any HTTP call; any non-429 4xx → `Rejected`);
    6. fenced completion; `NotSent` returns the attempt, at most `notsent_max` times (C9). Validation-time rejects insert a `purpose=blocked` row.
  - Lease expiry → `send_unknown` (possible send). At most 2 transmissions; then `failed_unknown` + ops alert. A double crash after reservation may end with zero transmissions; this is surfaced, not prevented.
  - Outbox statuses: `queued|held|sending|sent|send_unknown|failed_send|failed_unknown|failed_preflight|dropped` (no `blocked`).
  - Run attempt 2 only after a confirmed terminal attempt 1.
- **Events (§7.6):**
  - `agent_event.seq` + `agent_event_consumer` (outcome in the same transaction); `GET /jobs/agent-event-outcome/{seq}`.
  - **v5.1 (X7):** one consumer `n8n_notice` in `agent_event_subscription`; `consumer=` on the events and outcome routes. The cursor walks existing rows in `seq` order and stops at the first without an outcome (gaps safe). Retention writes `expired` before deleting.
  - Server-side type → kind → class map; unknown types → `refused`.
  - v5.2 (C6): `POST /jobs/agent-event/{seq}/outcome` + the jobs sweeper for events ending without agent output. (C8) `signal.created` carries the stored `assist_signal.id` in `payload.ref`.
- **P4 (§7.4.1, 08:08-b):** `deposit` → `pa:dy`, request kinds → `pa:ry`, owner previews → `pa:oy`; existing pending items only; `reason_code` predicates.
- **jarvise-mcp (§7.5):** per-profile tokens; P0 = **6** tools, P2 = 7, P3a = 8; acct = 4.
- **Budget (§7.8, v5.1 X2):**
  - The $50 provider limit on `JARVISE_HERMES_OPENROUTER_KEY` is set first.
  - Client-enforced probe: fixed `GET` key-info URL, static tests, key never logged; the proxy enforces the host only.
  - `budget_reservation(reservation_id, caller, run_ref, attempt, …, status open|claimed|terminal|covered|void)`: worst case = `(1 + R) × max_turns × (max_input_tokens × in_price + max_output_tokens × out_price)` with reasoning tokens inside `max_output_tokens` and `R` the pinned SDK retry count. Unprovable caps → caller disabled.
  - Admission in one `BEGIN IMMEDIATE`: `probe_usage + Σ terminal-not-covered + Σ open/claimed + worst_case ≤ ceiling` ($40 core/researcher, $50 digest).
  - `claim` once before forwarding; it returns an HMAC `admission_receipt` (`JARVISE_BUDGET_RECEIPT_KEY`, jobs + adapters only). `hermes-ingest` and `core-ingress` verify it (destination, caller, profile, run, attempt, caps hash, expiry) and consume it durably (fsync) before calling Hermes; 409 on forged/expired/reused/mismatched. Digest attempt 2 needs a new reservation. Unclaimed → `void` after `reservation_void_after_min` (> the longest run deadline).
  - `settle` with `run_terminal=confirmed` → `terminal` (still at full worst case; `actual_usd` is for reconciliation only); `unconfirmed` keeps the full worst case. `terminal → covered` only after a probe read ≥ `terminal_at_ms + provider_lag_min` **and** a rise in the limit-period usage (§7.8). No time-only release.
  - Thresholds: $32 alert / $40 / $50 or 402.
- **Digest (§7.9, PR #397 follow-up, v5.1 X5):** `digest_output.v2` = `{url, day}` items only. URLs are canonicalized (https, query and fragment stripped, safe path ≤ `digest_url_path_max_len`); `render_markdown` writes `N. **<category_th> · <domain> · <day>**` + the stripped URL (parseable by `parse_digest_text`; round-trip asserted); owner displays show the **domain as link text** only (C10). `publish_digest` writes the `<file>.items.json` sidecar (`digest_items.v2`, producer + category + `md_sha256`) atomically before the `.md`. `parse_digest_items_from_dir` prefers a matching sidecar. Policy categories = `THEME_LABELS` keys (add `macro`, `regulation`, `exchange`, `crypto_news`, `onchain`); `context_lines` iterates those keys; no title fallback for `producer=hermes`. Fixtures cover Home, report `lines`, `read_digests`, the web consumer (`marketContextRepository.ts`) and RAG consumers; sidecar replay rule per §7.9.

## 2b. Settings (Chana's standing rule, 11:05; architecture v5.6 §7.11)
- **Never hard-code a number.** Every threshold, cap, budget, window, time or count comes from `src/jarvise_agents/settings_registry.py` (key, type, unit, default, min, max, owner), with Chana's overrides in `agent_setting` via the jobs CLI. Container values use compose `${VAR:-default}`; n8n schedules use workflow env; acceptance targets live in `tests/agents/acceptance_settings.toml`.
- Implement the §7.11.1 cross-checks and the static literal test (exceptions in §7.11.5 only).
- **S-1 (Chana 11:25):** the existing trading constants of §7.11.4 (5x clamp, 2R, analyzer floors, the main night hold `signals.py:110-114`, `sizing.py` cadence/lag) are **not** covered. Read them from their real source (call or import the existing code, or read the DB). Never copy them into settings or literals. The agent night check calls the existing night-hold code.
- **v5.3 (Z2):**
  - every key in §7.11.2, including the flow, testing, fixture and named acceptance tables;
  - digest defaults 8 items and 14 days;
  - the v5.3 cross-checks, each with a red test;
  - adapters get settings via compose env (restart to apply), with a test that compose defaults equal registry defaults;
  - one literal-test scope (§7.11).
- **Sender (Z4/Z5):**
  - 4a requires `lease_until_ms − now > timeout_ms + send_fence_margin_ms`;
  - add the `notsent_count` and `max_attempts_at_reserve` columns;
  - the terminal limit is `min(max_send_attempts, max_attempts_at_reserve)`, so with a limit of 1 an ambiguous send → `failed_unknown`.
- **Budget (v5.4 AA1/AA2; replaces Z3's FIFO):**
  - Tables: `budget_probe_read` (UNIQUE `(period_id, read_at_ms)`), `budget_period` (baseline, `hwm_usd`, `unmatched_usd`, `covered_worst_usd`) and `budget_allocation` (UNIQUE `reservation_id`).
  - Release the oldest lag-eligible terminal rows only while `hwm − baseline ≥ covered_worst + Σ new worst + unmatched`.
  - Unmatched usage = idle increases + excess over possible spend + unreserved runs.
  - Everything runs in one `BEGIN IMMEDIATE`.
  - A same-period decrease → alert, no credit (high-water mark only).
  - Tests: the unrelated-charge test keeps liability; decrease/rebound; replay; reset; crash atomicity.
  - **v5.5:**
    - AB1: `restored_debt_usd`/`carried_debt_usd` on `budget_period`; a `budget_debt_event` log; recompute debt in the same transaction that raises unmatched usage; debt is added to admission liability; residual = "possibly undetectable, bounded by the provider hard limit".
    - AB2: admission `usage_floor = hwm_usd`, terminal rows at full worst case, `409 budget_anomaly` while the latest read is `decrease`.
    - AB3: `credit` as defined in §7.8; unchanged reads create no new credit but may release rows that mature later; maturity fixture.
  - **v5.6:**
    - AC1: late discoveries go to the active period's `unmatched_usd`, with `budget_debt_event.source_period_id`, in one transaction with a debt recompute; closed periods are frozen.
    - Rollover runs in the first `ok` read's transaction, with `restored_debt_usd = carried_debt_usd` and a `rollover` event. Admission adds only the active `restored_debt_usd`.
    - A duplicate read or source skips the whole operation.
    - `unknown` keeps all liabilities and is ignored for the drop test.
    - Fixtures: reset → late discovery, carried debt counted once, duplicate source, crash at each step, unknown after a drop.
- **C3/AA3:** implement the v5.4 quick-flag projection (§7.1, below the snapshot table):
  - use `clamped_keys` and `defaulted_keys`;
  - detect `missing` config keys with a read-only `SELECT key FROM assist_quick_config` on the `mode=ro` connection;
  - map each flag to served fields; drop unserved flags;
  - fixture h12.

  Don't modify `quick_config.py`.
- **AA4:** compose numbers appear only as `${VAR:-N}` with N equal to the registry default; the h11 equality test enforces this and the scanner skips only those positions.

## 3. Pure modules (your work too; Codex reviews them)
Each module:
- lives in `src/jarvise_agents/`, with tests in `tests/agents/`;
- uses the stdlib plus `requirements/worker.lock` only, and is **py311-compatible**;
- passes `mypy --strict` for the module, with 100% branch coverage and no I/O.

- **M-A `output_validator.py`:**
  - API: `validate_output(payload, *, session, evidence_rows, catalog, field_registry, derived_rules, reason_predicates, current_settings_version, event=None, now_ms) -> ValidationResult(ok, code, validated)`.
  - One test per reject code. Hypothesis: any extra field or free string → `schema`; random claim/phrase joins → `join_mismatch` whenever the entity, position or tf-bar differ.
- **M-A1 `renderer.py`:**
  - API: `render_th(validated, *, catalog, settings_snapshot, delivery_facts, footer_ctx) -> Rendered(text, render_sha256)`.
  - Deterministic; `initial_stop` labelled as the initial stop; night template + `night_facts`; `(ส่งซ้ำ)` flag; `fit_telegram`.
- **M-A2 `grounding_fields.py` + `derived_rules.py` + `catalog/phrases_v<N>.json`:**
  - Field registry with `value_type`, enum facts and `source_table`.
  - The 4 mechanism rules (`price_vs_level`, `change_sign`, `age_bucket`, `job_overdue`), each fully tested.
  - Catalog CI: no `opinion` class; joins reference real roles; banned-word lint (`ลองใหม่`, `ปิดเลย`, `ถือต่อ`, order/size/leverage/risk-change verbs, guarantee words).
- **M-A3 `sender_policy.py` (pure) — v5.1 (X9) split:**
  - `decide(row, facts) -> Action`: catalog acceptance, freshness, settings version, night/class, pre-flight result → `continue | hold | to_queued | blocked_template | drop | ops_template | preflight_retry | preflight_fail`. Pure-module rules apply (100% branches, no I/O).
  - Property tests: held rows never consume attempts; every `held` row that passes policy reaches `queued`; stale replies always yield a deliverable template.
- **`outbox_sender.py` + `send_message_ex` (I/O, not a pure module):** the §7.4 transactions, fenced reservation/completion, `NotSent` attempt return, crash matrix tests (integration tests on a `/tmp` DB; not held to the no-I/O rule).
- **M-E `chart_features.py` (P3a):** pure detector over closed candles → features JSON + deterministic SVG params. `svg_sha256` equals the sha of `charts.render_candle_svg` output (`charts.py:41`); snapshot tests. (No trader interpretation; that is v6.)
- **M-B `snapshot_allowlist.py`:**
  - `build_snapshot(raw, *, as_of_ms) -> dict` per the §7.1 allowlist incl. `source_as_of`.
  - Adds `snapshot_id`, drops unknown keys, raises on the secret regex.
  - Tests: fuzz at any depth; stable hash.
- **M-C `research_quality.py` (P3a):**
  - `canonical_url`, `content_sha256` (NFC, whitespace-collapsed), `simhash64`, `hamming`, `is_near_exact(max_hamming=3)`.
  - `trust_score(tier, independent_origins, age_hours, kind, cap)` with the cap a parameter; `citable`.
  - Stage-2 thresholds are config, not constants.
  - 200-doc fixture per architecture v5.6 §8.
- **M-D `n8n_lint.py` + `tests/test_n8n_agent_workflows.py`:**
  - Workflows `infra/n8n/workflows/jarvise-agent-*.json` may target only `jobs:8090/jobs/agent-*`, `web:8080/api/agent/*`, `core-ingress` (never `hermes-core` directly), `hermes-ingest` (digest) and `research-mcp` ETL routes.
  - Never `/jobs/assist-*`, `/api/risk/*`, `/api/paper/*` POST, Binance, `api.telegram.org`, or the `hermes` gateway directly.
  - Secrets are placeholders only. A schema-validation Code node comes before every `POST /jobs/agent-*`.

## 4. Phase work
Do exactly the approved phase as specified in architecture v5.6 §12 (build, acceptance, rollback). Reminders:
- **PR397-FOLLOWUP (v5–v5.2 additions):** client-enforced probe (`budget_probe.py`, fixed GET/URL, static tests; the proxy enforces the host only); `budget_reservation` + reserve/claim (signed receipt)/settle (C11 fix), with the digest workflow reserving before each slot; structured digest `digest_output.v2` + source-policy `category` + the `news_reaction` label-only path (W3); the runbook edit (step 8 override + the RAM precondition).
- **PR397-FOLLOWUP comes first.** It must land before Stage C or any spend on `JARVISE_HERMES_OPENROUTER_KEY`:
  - Chana confirms the provider hard limit.
  - Usage probe in `hermes-ingest` + `data/hermes/budget/agent_budget.db` + digest-side enforcement + the unknown-state rule + mocked-response tests.
  - **Then**, in the same PR, switch `hermes` to the agents key: `docker-compose.yml:457` → `OPENROUTER_API_KEY=${JARVISE_HERMES_OPENROUTER_KEY:-}` (C1; plus the fail-closed checks and a `compose config` CI run with the key unset); run the C5 cap proofs for the digest profile and record the measured `provider_lag_min`; update `.env.example` (name only, commented) and `docs/ops/hermes-stage-a.md:20`. Leave `:456` (`JARVISE_HERMES_API_KEY`) as is.
  - Source-policy v2 ships atomically: the file + the `policy.py:179` loader with v1 compat + the egress allowlist reader + tests.
  - `vps-deploy.sh`: budget dir ownership; the overlay and the `hermes-core` chown exclusion (here or in P0).
  - Gateways keep the s6 root→10000 bootstrap: no `no-new-privileges`/`read_only` on `hermes`/`hermes-core`.
- **Stage C is deferred until LearningYard RAM is measured (09:23).** When Chana instructs it, it **overrides PR #397 Activation step 8 (V4):** only OpenClaw's digest publication is quiesced; the OpenClaw container keeps running until the inventory + parity gate passes. The follow-up PR edits the runbook text to match.
- **OPENCLAW-INVENTORY / REMOVAL (08:08-a):**
  - Inventory every non-digest function with evidence (needed / not needed).
  - OpenClaw stays until every needed function has a replacement proven in production (≥ 7 days parity).
  - Then: rehearsed rollback **and reinstall**, a 30-day backup, `hermes import-agent openclaw --dry-run` only. Never port `jarvise-order-router`.
- **P0** ships the `manual` session kind only (`ask`/`notice` → 501) **with the manual-output endpoint + `agent_manual_result` (X8)**, plus the heartbeat table, `settings_version`, the §7.1 exact-source allowlist incl. `night_facts`, `agent_output.v2`, the validator/renderer, `derived_rules.py` (4 mechanism rules), the `agent_catalog` registry with `pc0`, and 6 MCP tools.
- **No Trader strategy content** (market-structure rules, trader phrases, golden set); that is v6.
- **P1 only after OpenClaw removal** (D-5) and the **pin gate**. It adds `agent_ask` + the `ask` kind, catalog `pc1` (v6 supplies the trader phrases), the outbox sender (M-A3) and the readability gate (**≥ 18/20** golden coverage + Chana approval).
- **`hermes-core` placement:**
  - Lives in `docker-compose.agents.yml` (profile `agents-core`), data dir `./data/hermes-core`.
  - Networks per v5.6 §10: `core_net`, `core_egress`; ingress only via `core-ingress`; MCP via `agent_api`; research via `research_core` from P3a.
  - Never on `jarvise_net`, no host ports. `jarvise-mcp` is only on `agent_api` + `agent_web`.
- **P4 (08:08-b):**
  - Proposals attach only to an existing pending item of the matching kind (`pa:dy` deposits, `pa:ry` requests, `pa:oy` owner previews).
  - No new generic confirm flow; agents never create items; ledger writes stay in those handlers.
- **RAM (08:08-c):** the on-demand worker is prohibited at S4 until measured. Don't add it to any agent compose profile.
- **Migrations:** next free N on your base; additive only; never edit old migrations.
- **New env vars** go into `.env.example` **commented out, names only**.

## 5. Checks before "done" (every phase)
- `ruff check src tests scripts`, `ruff format --check src tests scripts`, `mypy`, `pytest --cov=src --cov-fail-under=70`, run with the venv on PATH and every `BINANCE_*` unset. Run `web/` checks only if `web/` changed.
- `git diff --quiet origin/main -- src/jarvise_assist/execute.py`.
- `grep -rnE "BINANCE_|TELEGRAM_BOT_TOKEN|JARVISE_JOBS_TOKEN|\$\{OPENROUTER_API_KEY" infra/hermes* docker-compose.agents.yml` finds nothing except explicit negative tests; for the PR #397 follow-up also check the `hermes`/`hermes-ingest` blocks of `docker-compose.yml` (only `${JARVISE_HERMES_OPENROUTER_KEY…}` for the provider key; `API_SERVER_KEY=${JARVISE_HERMES_API_KEY…}` unchanged).
- Reachability tests for the phase's networks (v5.6 §10) pass.
- **LearningYard checklist** (v5.6 §12) prepared for Chana if the phase touches compose, networks or volumes.
- Static test: no `httpx`/jobs URL and no `redis`/`_redis`/`redis_get*` in agent routes or jarvise-mcp (rule 19).
- Phase acceptance tests exist, are hermetic (no live LLM/network/Redis), and fail when the guard is removed (record 2 mutation red runs on a `git archive` copy).
- Rollback drill on a `/tmp` DB copy, recorded.
- Write `docs/handoff/hermes-<phase>.md`: what changed, flags, env names (names only), rollback, open questions.
- **STOP.** Report the diff summary. J.A.R.V.I.S.E launches the Claude and Codex co-reviews (`claude -p --permission-mode plan`, `codex exec --sandbox read-only`) using `hermes-brief-codex-v5.6.md`. Resolve every MUST, then wait for Chana.

## 6. Stop and ask Chana when
- Any change to `execute.py`, `signals.py` rules, `quick*.py` behaviour, risk constants, live or execution flags, or partner-ledger semantics.
- A migration number collides, or a §7 contract doesn't fit the code on your base.
- A Hermes feature behaves differently on the pin (report the docs page and the observed behaviour).
- Any push, PR, merge, VPS, `.env`, secret or deploy action, or anything during :20–:27 BKK.
