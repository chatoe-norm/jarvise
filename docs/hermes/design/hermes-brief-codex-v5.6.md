# Co-review brief v5.6: Jarvise × Hermes phases (Codex CLI and Claude Code; includes the Claude safety review)

> **v5.6 (Chana 12:11):** budget checks updated for AC1 (late discovery → active period with provenance; rollover debt init; no double count) and the duplicate/unknown wording. Replaces `hermes-brief-codex-v5.5.md`.
>
> **v5.5 (Chana 12:04):** budget checks updated for AB1 (re-opened debt), AB2 (high-water admission floor, refuse on decrease), AB3 (recorded credit, maturity fixture). Replaces `hermes-brief-codex-v5.4.md`.
>
> **v5.4 (Chana 11:54):** checks updated for AA1 (aggregate coverage), AA2 (probe/allocation tables, one transaction, decrease = no credit), AA3 (quick flags) and AA4 (compose fallbacks). Replaces `hermes-brief-codex-v5.3.md`.
>
> **v5.3 (Chana 11:25):**
> - Checks updated for Z1 (per-use freshness), Z2 (complete registry and cross-checks), Z3 (FIFO coverage allocation), Z4 (actual-lease fence), Z5 (effective attempt limit, `notsent_count`) and C3 (flag projection).
> - S-1: existing trading constants are read-only references, never copied.
> - Claude reviews v5.3 after 2026-11-30 02:00 BKK. Its safety review is required before any phase merges.
>
> Replaces `hermes-brief-codex-v5.2.md`.
>
> **v5.2 (Chana 11:05):** contracts updated for the v5.1 cross-check (Y1/C2 freshness, C3, C7, C1, C10, Y2/C4/C12/C5, Y3/C9, C6, C8) and the standing rule (no hard-coded numbers, §7.11). Replaces `hermes-brief-codex-v5.1.md`.
> **v5.1 (Chana 10:41):** contracts updated for Codex r5 X1–X9, the restored freshness definitions and the key name `JARVISE_HERMES_OPENROUTER_KEY`; replaced `hermes-brief-codex-v5.md`.
> v5 note: replaced `hermes-brief-codex-v4.md` (kept for history). Under the playbook (Chana D-6, 2026-11-27 07:39):
> - Cursor/Grok implement.
> - **Codex and Claude Code co-review every phase diff, read-only.** Claude's review also counts as the mandatory **safety review**.
> - Codex no longer implements modules (M-A…M-D are implementer work that Codex reviews).
> - This whole file is the prompt. Pipe it on stdin so the reviewer sees every rule (fixes M9: the v1 heredoc referenced rules it didn't include).

## How J.A.R.V.I.S.E runs it (the reviewer does not run these)
```bash
NOTES=/Users/gizmo-macbook/Public/Projects/jarvise/worktrees/.jarvise-notes
WT=/Users/gizmo-macbook/Public/Projects/jarvise/worktrees/hermes-<phase>
export PATH="/Users/gizmo-macbook/Public/Projects/jarvise/.venv/bin:/Users/gizmo-macbook/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
for v in $(env | grep -o '^BINANCE_[A-Z_]*'); do unset "$v"; done
cd "$WT"
codex exec --sandbox read-only -C "$WT" --json -o "$NOTES/hermes-<phase>-codex-review.md" - < "$NOTES/hermes-design/hermes-brief-codex-v5.2.md"
claude -p --output-format text --permission-mode plan - < "$NOTES/hermes-design/hermes-brief-codex-v5.2.md" > "$NOTES/hermes-<phase>-claude-review.md"
```
Fill in before running: `PHASE=<phase>`, `BASE=<base sha>`. The worktree's HEAD is the implementer's local commit.

---

## PROMPT
You are a co-reviewer for Jarvise × Hermes phase **<phase>**. If you are Claude, this review is also the **safety review**. The work is read-only:
- Do not edit, stage, commit, push or comment anywhere.
- No network, VPS or docker. Never read `.env`, keys, tokens or `partner_kyc*`.
- Don't run `ps`/`pgrep` with arguments.

- **Diff:** `git diff <BASE>...HEAD`.
- **Design contract:** `/Users/gizmo-macbook/Public/Projects/jarvise/worktrees/.jarvise-notes/hermes-design/hermes-architecture-v5.6.md` (§7 contracts, §9 hard rules 1–19, §10 security, §11 RAM, §12 phase gates, §14/§17 decisions incl. 08:08 a/b/c, 08:28 and 09:23).
- **Implementer brief:** `hermes-brief-implementer-v5.6.md` in the same folder.

### Hard rules to enforce (architecture v5.6 §9, verbatim)

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


### Checks (cite file:line for every finding)
0. **Key names (Chana 10:41):** `hermes`/`hermes-core` map `OPENROUTER_API_KEY=${JARVISE_HERMES_OPENROUTER_KEY:-}` (no `:?` in the shared compose; services fail closed on an empty key, C1) and `hermes-ingest` uses `JARVISE_HERMES_OPENROUTER_KEY` for the probe only; no host-side `${OPENROUTER_API_KEY}` on any Hermes-side service; `JARVISE_HERMES_API_KEY` is still only `API_SERVER_KEY` + the adapter (not renamed, never a provider key); the two never feed the same container variable.
1. **Hard rules 1–19 above.** Run:
   - `git diff --quiet origin/main -- src/jarvise_assist/execute.py`;
   - grep new imports (AST ban: `jarvise_assist.execute`, `jarvise_trade`, `redis`, `jarvise_assist.futures.build_futures_client`);
   - grep compose/workflows for `BINANCE_|TELEGRAM_BOT_TOKEN|JARVISE_JOBS_TOKEN|\$\{OPENROUTER_API_KEY` on `hermes`, `hermes-core`, `hermes-ingest`, `core-ingress`, egress proxies, `jarvise-mcp`, `research-*` and the n8n agent workflows.
2. **Auth:**
   - Every new endpoint is fail-closed with a scoped token (not Basic, not the jobs token, never `_authorized`/`require_auth`).
   - MCP tokens are per profile, and the profile is derived from the token.
   - Agent read routes are GET only (preview included). `jarvise.db` is `mode=ro` with the `partner_kyc%` authorizer.
3. **Data leaks:** snapshot/partner payloads vs the §7.1 allowlist; no order ids, keys, KYC, real names or bank fields; static `partner_kyc` gate present.
4. **Read-only sources (§7.1, rule 19; exact column map, W6/X6):** the §7.1 tables/columns/types/filters exactly; quick values only via `effective_quick_values(get_quick_config(conn))` with flags, no raw config rows/A2 keys/leverage (C3); `ts_kind` per field; excluded columns absent; no `caps`/`levels`/`r_now`/mark/position `target`/`leverage`/`paper_*`; enum normalization + DISTINCT test; null/unmapped → `unavailable`; `initial_stop` labelled initial; `stop_warning` from latch + `latest_no_stop_reason` (never "no stop exists").
   **Rule 19 details:**
   - No agent route or jarvise-mcp code makes an HTTP call to jobs or reads Redis. Health comes from `agent_job_heartbeat`; doctrine is not built.
   - No call to `scan_signal_candidates`, `run_assist_*`, futures clients or `build_trend_rows` on a live scan.
   - `night_facts` come only from open `assist_watch_position` rows; `settings_version` comes from the shared function.
5. **Sessions and evidence (§7.2):**
   - Session `kind` ∈ `manual|ask|notice`, and only kinds whose tables exist on the base (else 501). Source ids equal the session's exactly (X3).
   - Manual outputs (X8) go to `agent-manual-output` → `agent_manual_result`; zero outbox rows.
   - One open session per profile (partial unique index); 10-minute TTL.
   - 429 `retry_after_ms`, the busy deadline, queue bound 3 and quarantine as specified.
   - Missing header → 400. Unknown, closed, expired or foreign-profile session → 409 **with no evidence row**.
   - Web is the only evidence writer; `body_sha256` is of the served bytes; `fact_times` are populated.
6. **Structured output (§7.3):**
   - One discriminated `agent_output.v2`; `additionalProperties: false`; no free strings.
   - `catalog_version` is on every path and the catalog registry handles rollover and revocation.
   - `run_id` equals `session.run_id`. Claims have typed values, unique ids and are all referenced. Citations are bound to claims; research_doc citations are rejected.
   - Phrases have **no opinion class**, and their `joins` are enforced (e.g. BTC symbol + ETH stop → `join_mismatch`; incarnation = `entry_time_ms` only, NULL → `unavailable`; `same_observation` mandatory on position phrases; `proposal_target`). Derived roles are typed and ordered (`change_sign` earlier/later). `reason_code` predicates are enforced, alias-bound.
   - Freshness (v5.2): per-tf `bar` via `INTERVAL_MS` + `bar_ingest_lag_ms`; `analysis_bar` reuses `expected_analysis_bar_ms`; per-role modes (`ordered`, `read`); checked on every used claim at validation and at send.
   - **Standing rule (§7.11):** no bare numeric literal in new agent code outside the settings registry (exceptions §7.11.5); every number has a registry entry with default/range/owner; cross-checks enforced; §7.11.4 trading constants untouched.
   - Every §7.3.3 code has a test.
   - The renderer is pure and runs at send time. No path to Telegram bypasses validator → outbox → renderer.
   - **No Trader strategy content** (that is v6).
7. **Delivery (§7.4, v5.1 X1):**
   - Validation + result + outbox (+ the event outcome) commit in one transaction.
   - Pure `sender_policy.decide()` runs **before** reservation and consumes no attempt: catalog acceptance, freshness of every used claim, settings version, night/class, pre-flight (credentials, render).
   - Held rows that pass move `held → queued` in the policy transaction; stale/revoked replies become a deliverable `blocked` template; stale notices `dropped`; pre-flight failure retries and ends `failed_preflight` after 30 min. No outbox status `blocked`.
   - Reservation and completion are fenced by `attempt_token`; step 4a re-checks token, lease margin and policy right before HTTP (Y3). Non-429 4xx → `Rejected`; `notsent_max`; validation-time `blocked` row (C9). `send_message_ex` returns `Sent`/`Rejected`/`Ambiguous`/`NotSent` (`NotSent` only before any HTTP call, and it returns the attempt).
   - Lease expiry → `send_unknown`; ≤ 2 transmissions; then `failed_unknown` + ops alert. The crash matrix exists; no test claims zero-send exhaustion is impossible.
   - Run attempt 2 only after a confirmed terminal attempt 1. `callback_data` ≤ 64 bytes.
7b. **Events (§7.6, X7):** `seq` cursor walked over existing rows (gaps safe); one consumer `n8n_notice` via `agent_event_subscription` and `consumer=` on both routes; outcome in the same transaction as the notice or drop; retention writes `expired` first; `POST /jobs/agent-event/{seq}/outcome` + sweeper for no-output endings (C6); stored `assist_signal.id` as the join key (C8).
8. **Night hold:**
   - Checked at delivery time. Trader/accountant LLM text is never delivered at night (template + verified facts only).
   - Notice kind/class comes from the server-side event-type map; an unknown event type is refused at session open.
   - Tester boundaries are tested (23:29:59 / 23:30:00 / 07:29:59 / 07:30:00, midnight wrap, `TZ=UTC` host, naive input, month/year rollover).
9. **Runtime and network (§10–§11):**
   - `hermes-core` is not on `jarvise_net`, has no host ports, its own data dir, no `web` toolset and no Hermes cron; egress is `openrouter.ai` only; ingress only via `core-ingress`.
   - `jarvise-mcp` is only on `agent_api` + `agent_web`. `research-db` is reachable only from `research-mcp`.
   - Reachability tests exist; `NO_PROXY` covers the internal names; Telegram is disabled in both runtimes.
   - Gateways keep the s6 bootstrap (no `no-new-privileges`/`read_only` on them); other services are hardened.
   - `vps-deploy.sh` chown excludes `hermes-core` and includes the overlay; no collision with PR #397.
10. **Budget (§7.8):**
    - Only `JARVISE_HERMES_OPENROUTER_KEY` as the provider key; the probe and enforcement exist before any use of that key.
    - Thresholds $32/$40/$50 everywhere.
    - `budget_reservation` (X2): atomic `BEGIN IMMEDIATE` admission; `reservation_id` UNIQUE `(caller, run_ref, attempt)`; claim returns an HMAC `admission_receipt` verified and durably consumed by `hermes-ingest`/`core-ingress` (Y2/C4); new reservation per digest retry; liability held at worst case until confirmed terminal **and** covered (v5.4 AA1/AA2: the cumulative aggregate rule `hwm − baseline ≥ covered_worst + new worst + unmatched`, oldest lag-eligible first, with `budget_probe_read`/`budget_period`/`budget_allocation` in one transaction; a decrease earns no credit; no time release; `actual_usd` never lowers it, C12); cap proofs for the digest profile (C5); worst case includes reasoning tokens and `(1 + R)` SDK retries; unprovable callers disabled; ceilings $40 core/researcher, $50 digest.
    - The probe is enforced **client-side** (fixed GET/URL, static tests); no proxy path claim.
    - The unknown state pauses core and researcher; reset follows the provider; no fallback key.
11. **P4 (§7.4.1, 08:08-b):** proposals attach only to existing pending `pa:dy`/`pa:ry`/`pa:oy` items. No new confirm flow; ledger writes only in those handlers (call-graph test).
12a. **Digest (§7.9, X5):** `digest_output.v2` carries `{url, day}` only; markdown lines are Jarvise-composed and round-trip through `parse_digest_text`; atomic `items.json` sidecar (producer, category, `md_sha256`; replay rule); URLs https with query/fragment stripped and domain-only link text on owner displays (C10); sidecar-first reader; total `THEME_LABELS`; no title fallback for Hermes items; Home/report/RAG fixtures.
12. **OpenClaw (08:08-a, V4; Stage C deferred until the LearningYard measurement, 09:23):** Stage C quiesces only OpenClaw's digest (Activation step 8 overridden; runbook edited). Removal PRs include the inventory with evidence and ≥ 7-day parity for each needed function.
12b. **LearningYard checklist** (§12) is present for compose/network/volume changes.
13. **Migrations:** additive, next free N, no edits to old migrations.
14. **Tests:**
    - Phase acceptance tests (§12) exist and are hermetic (no live LLM, network or Redis).
    - The mutation spot-check of `mutation_guard_count` guards actually goes red.
    - The rollback drill is recorded; code is py311-compatible.
15. **Rollback flag:** off = previous behaviour, proven by a test.
16a. **v5.3 checks:**
    - **Z1:** freshness is checked per use (a direct slot uses the field class; each derived role uses its mode; every use must pass); the h10 fixture is present.
    - **Z2 / S-1:**
      - no numeric literal outside `settings_registry.py`, fixtures and `acceptance_settings.toml` within the §7.11 scope;
      - every §7.11 cross-check enforced with a red test;
      - no agent copy of a §7.11.4 trading constant (night hold via `signals.py:110-114`).
    - **Z4:** 4a compares the actual remaining lease with timeout + margin.
    - **Z5:** the effective limit is `min(current, at_reserve)`; `notsent_count` is persisted.
    - **C3/AA3:** the quick-flag projection matches §7.1 v5.4 (`clamped_keys`, `defaulted_keys`, read-only missing-key check, field mapping, h12 fixtures); `quick_config.py` untouched.
    - **AA4:** compose numbers only as registry-equal `${VAR:-N}`; equality test present.
    - **AA1:** the unrelated-charge test keeps liability.
    - **v5.5 AB1–AB3:** unmatched usage found after release → `restored_debt_usd` in the same transaction and in admission liability; admission floor = `hwm_usd` and `409 budget_anomaly` on a `decrease` read; the maturity fixture passes and unchanged reads create no new credit.
    - **v5.6 AC1:**
      - a late discovery raises active-period debt, with `source_period_id`;
      - rollover sets `restored_debt_usd = carried_debt_usd`, and admission adds it once;
      - a duplicate read or source skips the whole operation;
      - `unknown` keeps all liabilities and never lifts a `decrease` refusal;
      - the v5.6 fixtures are present.
16. **Pure modules (if in the diff):** M-A output validator, M-A1 renderer, M-A2 field registry + derived rules + phrase catalog, M-A3 `sender_policy.py` (the I/O `outbox_sender.py` is integration-tested, not a pure module), M-B snapshot allowlist, M-C research quality, M-D n8n lint and M-E chart features, as in implementer brief v5.6 §3. Each needs 100% branch coverage, `mypy --strict` and no I/O.

### Output
Write these sections in order:
1. Verdict: `PASS` | `PASS WITH CHANGES` | `FAIL`.
2. `MUST` / `SHOULD` / `NICE` lists (each item with file:line and a fix).
3. "Hard rules" table: rule # (1–19) → OK/VIOLATION + evidence.
4. Commands you ran and their results.

If you are Claude, add a final line: `SAFETY REVIEW: APPROVE | BLOCK` with one-sentence reason.
