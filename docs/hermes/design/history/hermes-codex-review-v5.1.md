1. **VERDICT: NOT-READY** — v5.1 substantially closes r5, but one P0 freshness contract needs correction; remaining delivery and budget gaps belong to later phase gates.  
**P0-START: NO-GO** pending Y1 only.

References: **A** = `hermes-architecture-v5.1.md`; **I** = `hermes-brief-implementer-v5.1.md`; **C** = `hermes-brief-codex-v5.1.md`, all in the supplied notes directory. Verified refs: main `438e687`, PR #397 `a5967aa`. “CLOSED” means design closure; implementation remains unverified.

2. **X CLOSURE:**

| ID | status | evidence | remaining gap |
|---|---|---|---|
| X1 | PARTIAL | A:663–706 | Held wake, blocked delivery, preflight expiry, fenced `NotSent`, and conservative double-crash handling are repaired. A late worker can still transmit after its lease/policy facts expire: Y3. |
| X2 | PARTIAL | A:815–829 | Atomic admission, retry reservations, full liability for unconfirmed runs, and disabled-until-proven caps are sound. Probe age does not prove coverage; ingress lacks authoritative claim verification: Y2. |
| X3 | PARTIAL | A:385,515–538,570 | Ordered roles, observation joins, proposal joins and exact source equality are explicit. Incarnation hash is not guaranteed to identify a distinct position lifetime: Y4. |
| X4 | PARTIAL | A:589–594,665–668 | Every used claim is checked twice, but the specified bar helper returns a **4h opening timestamp**, not a timeframe-specific closing timestamp: Y1. |
| X5 | CLOSED | A:851–859 | Compatible markdown, sidecar provenance, total labels and Home/report/RAG fixtures specified. Only permitted URL/day strings remain agent-supplied. Runtime fixtures unverified. |
| X6 | CLOSED | A:422–435 | Columns match main schema; seven config keys match the registry. Missing/partial warning semantics match `watch.py:647–696` and `store.py:996–1011`. |
| X7 | CLOSED | A:766–778 | Named consumer, row-walk cursor, transactional outcomes and expiry-before-delete resolve gaps/retention ordering. Example URL drift is X9. |
| X8 | CLOSED | A:569,639–651; A:1152,1169 | Manual endpoint, result table, idempotency and render-on-read preview exist; explicitly zero outbox rows and no Telegram delivery. |
| X9 | PARTIAL | A:409,661,1012; I:140–143; C:136 | ETL identity and pure/I/O split repaired. Consumer parameters and signal-target wording still drift: Y5. |
| N4 | CLOSED | v4:512; A:589–594 | Original four freshness definitions restored, expanded to every used claim. Concrete bar interpretation remains X4/Y1. |

`NotSent` cannot return an attempt after an HTTP call under the stated contract; stale-token completion cannot decrement it. Two reservation crashes may exhaust delivery with zero transmissions, now explicitly accepted and surfaced.

3. **KEY NAMES:**

Correct. PR #397 `docker-compose.yml:456,511`, `app.py:55`, and runbook step 3 (`:104`) establish `JARVISE_HERMES_API_KEY` as adapter↔API-server authentication. A:800–803 correctly preserves it and changes only the provider source at compose `:457` to `JARVISE_HERMES_OPENROUTER_KEY`. The existing PR still uses the shared provider key; the follow-up must implement the switch and update the runbook.

4. **PARTIAL CLOSURE:**

| ID | status | evidence | remaining gap |
|---|---|---|---|
| W1, V1, N1 | PARTIAL | A:385,515–538 | Lifetime-identity overclaim: Y4; observation binding substantially repairs relational grounding. |
| W2, V2, N2, R2 | PARTIAL | A:570,589–594,665–694 | Freshness interpretation and final transmission recheck: Y1/Y3. |
| W3 | CLOSED | A:851–859 | Downstream contract and exported-field fixtures specified. |
| W5, V5 | CLOSED | A:639–651,766–778 | Manual interface and consumer continuity specified. |
| W6, V6 | CLOSED | A:422–446 | Exact sources, safe warning meaning, SQLite-only path specified. |
| W9, V8, N9 | PARTIAL | A:771–772; I:78 | Residual synchronization: Y5. |
| V3, N5 | PARTIAL | A:678–706 | Completion fencing repaired; late-worker transmission remains Y3. |
| R5 | PARTIAL | A §§7.3–7.8 | Remaining Y1–Y4; later-phase issues do not independently block P0. |

5. **DECISION FIDELITY:**

- **10:41 — PARTIAL:** key naming and restoration honored; X1–X3 retain the gaps below.
- **09:23 — OK:** evidence-only enums; Stage C measurement gate; doctrine deferred; Trader spec remains v6.
- **08:28 — PARTIAL:** typed JSON/Jarvise rendering preserved; Y1/Y3 affect freshness enforcement.
- **08:08-a — OK:** inventory, continued operation and ≥7-day replacement parity retained.
- **08:08-b — OK:** existing `pa:dy`/`pa:ry`/`pa:oy`; no agent-created items.
- **08:08-c — OK:** worker prohibited at S4 pending measurement and separate decision.
- **D-1 — OK:** two runtimes, same pin, separate directories/egress.
- **D-2 — PARTIAL:** correct key, hard limit and pre-spend ordering; Y2 remains.
- **D-3 — OK:** shadow trigger, parity, 30-day backup and rehearsed reinstall retained.
- **D-4 — OK:** both RAM gates retained; measurements unverified.
- **D-5 — OK:** P0 after contracts; P1 after removal.
- **D-6 — OK:** implementer/reviewer separation and mandatory Claude safety review retained.
- **D-7 — OK:** no backup Telegram bot.
- **D-8 — OK:** web-only evidence writer, separate DB.
- **D-9 — PARTIAL:** binding/footer preserved; Y1/Y4 remain.
- **D-10 — OK:** multilingual-e5-small and versioned source policy retained.
- **D-11 — OK:** domain allowlist before Stage C; core OpenRouter-only.
- **t252u — OK:** eventual full OpenClaw removal retained.
- **07:39 also-items — PARTIAL:** M7/M9 and Tester night boundaries retained; R5 has residuals.

All supplied hard rules are represented at A:924–957 and in both briefs. Examined PR diffs show zero changes to execution, signals, sizing or schema. No new trading scope or adopted Trader universe/EMA/one-at-a-time strategy found.

6. **NEW FINDINGS:**

**High**

- **Y1 — Bar freshness contract names the wrong timestamp/helper.**  
  **Evidence:** A:591 requires `bar_close_ms` equality “for its tf”; main `sizing.py:392–395` accepts only `now_ms` and returns the opening time of a published **4h** analysis bar.  
  **Why:** literal implementation rejects valid facts or invents timeframe policy. Also, two `change_sign` closes cannot both equal the latest bar if both receive class `bar`.  
  **Fix:** specify per-timeframe expected timestamps, opening/closing conversion and publication lag; define historical-role classification so earlier candles remain usable with their time slots. Preserve existing analyzer behavior.  
  **F:** **P0-BLOCKING**.

- **Y2 — Budget coverage and admission authenticity remain unproven.**  
  **Evidence:** A:826–828; network boundaries A:967–986.  
  **Why:** a successful probe 60 minutes after termination need not include the charge; measurement is not a guaranteed coverage watermark. Separately, an adapter-local ledger can reject reuse but cannot establish that a newly presented ID was actually issued/claimed, or bind its caller/profile/caps. Cross-adapter reuse is unspecified.  
  **Fix:** retain liability until demonstrable provider coverage; otherwise keep conservative liability. Define a verifiable admission receipt bound to destination, caller, profile, run, attempt, caps and expiry, consumed durably before forwarding. Test forged/unclaimed/wrong-adapter IDs and crash replay.  
  **F:** **PHASE-GATED(PR #397 follow-up)**; apply to core/researcher before their activation. Reasoning/retry caps are correctly gated as unverified.

**Medium**

- **Y3 — Completion fencing does not fence transmission.**  
  **Evidence:** A:665–687 checks policy before reservation; A:694 only rejects a late completion.  
  **Why:** a worker paused after reservation can resume after lease expiry, catalog revocation, settings change or 23:30 and still send its old rendering.  
  **Fix:** define send ownership and a final active-token/lease/policy check immediately before HTTP; test suspension across lease expiry and the night boundary. Preserve conservative ambiguity accounting.  
  **F:** **PHASE-GATED(P1)**.

- **Y4 — Incarnation is an observation fingerprint, not guaranteed lifetime identity.**  
  **Evidence:** A:385,515; main `db.py:1659,1663`, `store.py:932`, `watch.py:443–467`.  
  **Why:** nullable/preserved entry time and identical entry can repeat; a close/reopen missed between polls is not distinguishable. Adding also changes entry without creating a new lifetime.  
  **Fix:** narrow the identity claim, enforce `same_observation` on every position phrase, and test null/repeated timestamps and equal-entry reopen. Defer lifetime attribution until a reliable producer generation exists.  
  **F:** **CAN-DEFER**; P0’s mandatory same-observation binding already prevents mixed-read sentences.

**Low**

- **Y5 — Small contract drift remains.**  
  **Evidence:** A:771–772 omits required `consumer=`; I:78 removes generic `target`, while A:427,440 explicitly permits signal-plan target.  
  **Why:** literal implementers receive conflicting interfaces.  
  **Fix:** synchronize examples; say “position target” consistently.  
  **F:** **CAN-DEFER**.

7. **CONVERGENCE:**

| item | classification | one-line reason |
|---|---|---|
| X4/Y1; freshness portions of W2/V2/N2/R2/R5 | P0-BLOCKING | P0 implements this validator and its bar boundary tests. |
| X2/Y2; D-2 residual | PHASE-GATED(PR #397 follow-up) | Must hold before first agents-key spend; P0 spends nothing. |
| X1/Y3; V3/N5 and delivery residuals | PHASE-GATED(P1) | P0 has no sender or outbox. |
| X3/Y4; W1/V1/N1 | CAN-DEFER | Enforce existing same-observation rule; avoid claiming lifetime uniqueness. |
| X9/Y5; W9/V8/N9 | CAN-DEFER | Canonical contracts resolve intent; clean examples during implementation. |
| Provider fields/caps/pin proofs | PHASE-GATED(PR #397 follow-up) | Digest stays disabled unless proven; repeat applicable proofs before P1/P3a. |
| Proposal evidence/guard fixtures | PHASE-GATED(P4) | Contract is specified; P0 exposes no partner tools. |
| Research/ETL fixtures and isolation | PHASE-GATED(P3a) | Those services and identities ship later. |

8. **OPEN DECISIONS FOR CHANA:**

None. These are corrections within approved scope. Runtime/provider evidence remains unverified.

9. **GATES:**

- **P0:** correct Y1, then manual-only implementation may proceed under explicit phase authorization.
- **PR #397 follow-up:** resolve Y2; prove caps/probe behavior; implement key switch, structured digest, source policy, allowlist and runbook amendments before spend.
- **P1:** resolve Y3; removal/parity, pin gate, approved v6 phrases/goldens, ≥18/20 coverage and readability approval.
- **Deploy:** explicit Chana approval, independent reviews including Claude safety review, measured RAM gates, blackout compliance and every-deploy LearningYard checks. Stage C remains deferred.

10. **COMMANDS RUN:**

Read-only commands, repeated invocations grouped:

- `ls`, `wc -l`, `cat`, `rg -n`, `sed -n` on supplied design/history/brief files.
- `diff -u` v5/v5.1 architecture and companion briefs.
- `git log -1 --oneline` for both specified refs.
- `git diff --stat origin/main...origin/feat/hermes-stage-a`.
- Path-scoped PR diff for `execute.py`, `signals.py`, `sizing.py`, `db.py`: empty.
- `git show <specified-ref>:<path>` with `rg`/`sed` for cited schema, watch/store/config/sizing, Telegram, digest, news, compose, adapter, proxy and runbook evidence.

No files changed, network/services/tests run, secrets read, or other reviewer output consulted.