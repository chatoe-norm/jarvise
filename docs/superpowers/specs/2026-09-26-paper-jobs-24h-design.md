# Jarvise — paper jobs 24/7 (enqueue + expire) — design

**Date:** 2026-09-26  
**Status:** Shipped + VPS activated 2026-09-29 — [`../plans/2026-09-26-paper-jobs-24h.md`](../plans/2026-09-26-paper-jobs-24h.md)  
**Depends on:** P4 paper approval slice B shipped (`jarvise paper run` enqueue default, `paper expire`)  
**Plan:** [`../plans/2026-09-26-paper-jobs-24h.md`](../plans/2026-09-26-paper-jobs-24h.md)  
**Related:** [`PROJECT_CONTEXT.md`](../../../PROJECT_CONTEXT.md) §5.2; ingest/rag jobs pattern in `src/jarvise/jobs.py`

## Goal

Run **paper enqueue** and **approval expire** 24/7 on the VPS through the existing n8n → `jobs:8090` contract so the approval queue fills without a laptop and timed-out pendings clear on a schedule — still **PAPER ONLY**, no live exchange orders.

## Non-goals

- Scheduled `--auto-fill` (Approve remains the fill gate)
- P4-C live spot orders on Approve
- New notification channels (Slack / Telegram / email)
- Reworking ingest or rag job implementations beyond shared patterns
- Changing paper CLI semantics (jobs call existing commands)

## Locked decisions

1. **Approach 1:** subprocess runners in `jobs.py` mirroring ingest/rag  
2. `POST /jobs/paper-run` → `jarvise paper run --universe $JARVISE_PAPER_UNIVERSE --timeframe $JARVISE_PAPER_TIMEFRAME --json` (**no** `--auto-fill`)  
3. `POST /jobs/paper-expire` → `jarvise paper expire --json`  
4. Env defaults: `JARVISE_PAPER_UNIVERSE=paper_core`, `JARVISE_PAPER_TIMEFRAME=4h`  
5. n8n cadence: paper-run **every 4 hours**, expire **every 1 hour**  
6. Redis JSON keys: `jarvise:paper:last`, `jarvise:paper:expire:last` via `publish_redis_status`  
7. Kill-switch engaged → skip payload, HTTP **409**, exit code 3  
8. `/analytics` Pipeline status + `GET /api/status` include both paper keys alongside ingest/rag  
9. No exchange trade/order APIs anywhere in this slice  

## Architecture

```text
n8n (every 4h) ──POST──► /jobs/paper-run
                           │ kill-switch? → 409 + Redis skipped
                           ▼
                    jarvise paper run --universe $U --timeframe $TF --json
                           │ enqueue only
                           ▼
                    Redis jarvise:paper:last

n8n (every 1h) ──POST──► /jobs/paper-expire
                           │ kill-switch? → 409 + Redis skipped
                           ▼
                    jarvise paper expire --json
                           ▼
                    Redis jarvise:paper:expire:last

/analytics Pipeline status ← paper + paper_expire (+ ingest + rag)
GET /api/status
```

**Auth:** unchanged — if `JARVISE_JOBS_TOKEN` is set, require header `X-Jarvise-Token`.

**HTTP status mapping (same as ingest/rag):**

| CLI exit | HTTP |
|----------|------|
| 0 | 200 |
| 3 (kill-switch) | 409 |
| other | 500 |

## Components

### 1. Jobs runners (`src/jarvise/jobs.py`)

```text
run_paper_run() -> (exit_code, payload)
run_paper_expire() -> (exit_code, payload)
```

- Kill-switch check first → `_skipped()` + publish Redis + return `(3, payload)`  
- `paper-run` cmd uses env with defaults `paper_core` / `4h`; must **not** pass `--auto-fill`  
- `paper-expire` cmd: `python -m jarvise paper expire --json`  
- Parse stdout JSON via existing `_parse_stdout`; set `paper_only: true`, `exit_code`  
- Always `publish_redis_status` for the corresponding key  

**Routes:**

```text
POST /jobs/paper-run
POST /jobs/paper-expire
```

### 2. n8n workflows

Add importable JSON (same shape as `jarvise-ingest-schedule.json`):

| File | Trigger | POST URL |
|------|---------|----------|
| `infra/n8n/workflows/jarvise-paper-run.json` | every 4 hours | `http://jobs:8090/jobs/paper-run` |
| `infra/n8n/workflows/jarvise-paper-expire.json` | every 1 hour | `http://jobs:8090/jobs/paper-expire` |

Workflow meta / notes: `paper_only: true`, enqueue only / expire only, no order placement. Operator imports via n8n UI (same as ingest/rag). Timeouts: paper-run ≥ 120s; expire ≥ 60s.

Optional host helper scripts only if ingest/rag already use them for local ops; not required if workflows alone match the VPS pattern.

### 3. Web

- Constants: `PAPER_KEY = "jarvise:paper:last"`, `PAPER_EXPIRE_KEY = "jarvise:paper:expire:last"`  
- `/analytics` Pipeline status pretty-print: add `paper` and `paper_expire` blocks  
- Dashboard “Last ingest / Last RAG” cards: either extend similarly or leave dashboard as-is if Pipeline on `/analytics` is enough — **prefer extending Pipeline on `/analytics` + `/api/status` only** (YAGNI for dashboard cards unless already trivial)  
- `/api/status`: include both keys  

### 4. Env / compose

- `.env.example`: document `JARVISE_PAPER_UNIVERSE`, `JARVISE_PAPER_TIMEFRAME`  
- Ensure jobs/worker containers receive these vars (same pattern as `JARVISE_INGEST_SYMBOLS` if already passed; add if missing)

## Error handling

| Case | Behavior |
|------|----------|
| Kill-switch | Skip; Redis skipped payload; HTTP 409 |
| CLI non-zero (not 3) | Redis written with payload; HTTP 500 |
| Empty/invalid stdout | `_parse_stdout` fallback with stdout/stderr tails |
| Expire with zero rows | Exit 0, `expired: 0` (success) |

No interactive prompts.

## Testing

- Unit: kill-switch → both runners return `(3, skipped)` and would publish (mock Redis publish if needed)  
- Unit: stub `subprocess.run` — paper-run argv includes universe/timeframe defaults and **excludes** `--auto-fill`  
- Unit: routes `POST /jobs/paper-run` and `/jobs/paper-expire` dispatch  
- Web: stub Redis JSON → Pipeline HTML or `/api/status` contains paper keys  
- Regression: existing `tests/test_jobs.py` ingest/rag cases still pass  

## Success criteria

1. Spec + plan accepted before product code  
2. With workflows imported and stack deployed, 4h job enqueues pendings for `paper_core` @ `4h` without a laptop  
3. Hourly expire marks timed-out pendings  
4. Kill-switch stops both jobs (409 + Redis skipped)  
5. `/analytics` Pipeline shows paper + expire JSON; no live-order code paths added  

## Out of scope / follow-ups

- Pending-approval notifications  
- Aligning run exactly to candle close minute (fixed 4h interval is enough for this slice; refine later if needed)  
- Chaining paper-run after ingest HTTP (explicit non-goal; separate schedules)  

## Related

- [Product roadmap](2026-09-23-product-roadmap-design.md)  
- [P4 paper approval design](2026-09-23-p4-manual-approval-paper-slice-design.md)  
- [Product usage](../../product-usage.md)  
- [Hostinger VPS](../../deploy/hostinger-vps.md)  
- [Ops](../../deploy/ops.md)  
