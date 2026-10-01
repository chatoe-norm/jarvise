# Pending-approval Telegram — Implementation Plan

**Goal:** Soft-fail Telegram on enqueue + hourly pending digest via jobs/n8n.

## File map

| Path | Responsibility |
|------|----------------|
| `src/jarvise_notify/` | Telegram client + formatters |
| `src/jarvise_paper/approval.py` | Notify after enqueue |
| `src/jarvise/jobs.py` | `paper-pending-digest` route |
| `infra/n8n/workflows/jarvise-paper-pending-digest.json` | Hourly cron |
| `.env.example` / compose | Telegram env |

## Tasks

1. `jarvise_notify` + unit tests (mock HTTP)
2. Wire enqueue soft-fail notify
3. Digest job + n8n workflow
4. Docs / PROJECT_CONTEXT §5.8b
