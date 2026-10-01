# Jarvise §5.8b — pending-approval Telegram alerts — design

**Date:** 2026-10-01  
**Status:** APPROVED  
**Parent:** Ops hygiene (§5.8)

## Goal

Telegram alert on new pending approvals + hourly digest while any remain. Never block paper enqueue/jobs.

## Locked decisions

1. Channel: Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)
2. Immediate on successful enqueue + hourly pending digest
3. Soft-fail always (missing env / API error → log, continue)

## Non-goals

Slack/email · Telegram approve/reject · live-order alerts · hard-fail

## Architecture

- `jarvise_notify` — Telegram sendMessage helper
- `enqueue_approval` → notify after successful upsert
- `POST /jobs/paper-pending-digest` + n8n 1h cron

## Success

With tokens set: enqueue → Telegram; hourly job → digest or no-op if empty. Without tokens: paper path unchanged.
