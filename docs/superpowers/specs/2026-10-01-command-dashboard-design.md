# Jarvise Command Dashboard — Design

**Date:** 2026-10-01  
**Status:** Approved for implementation  
**Approach:** Hybrid FastAPI JSON API + Vite/React/shadcn SPA

## Intent

Replace dump-style HTML (`pre`/JSON cards) on `:8080` with a **Command Dashboard** so the owner immediately sees: pending approvals, paper health, thesis decisions, and ops only when needed.

## Constraints

- Tailscale `:8080` perimeter; optional `WEB_BASIC_AUTH_*`
- Paper-first; `JARVISE_LIVE_TRADING` off by default; kill-switch semantics unchanged
- No public HTTPS, TradingView embed, or live autonomy controls in v1

## Architecture

- **Backend:** [`src/jarvise_web/app.py`](../../../src/jarvise_web/app.py) — auth, `/api/*`, POST approve/reject/kill, serves SPA static
- **Frontend:** `web/` — Vite + React + TypeScript + Tailwind + shadcn patterns
- **Docker:** multi-stage `Dockerfile.web` builds SPA → copies `web/dist` into image

## Information architecture

| Route | Purpose |
|-------|---------|
| `/` (Home) | Approval queue primary; status strip; KPI strip |
| `/paper` | Equity chart, positions, expectancy |
| `/decisions` | Analysis table + filters + regime badges |
| `/exchange` | Spot balances + ~USD bars (soft-fail) |
| `/ops` | Kill switch, pipeline jobs, Qdrant |

Legacy `/analytics` redirects into the SPA (Home or Decisions).

## Visual system

- Dark control-room; single blue accent; green/red for state only
- Typography: Geist / system distinctive stack (not Inter default)
- Charts via Recharts (equity, win/loss, ~USD bars)
- No raw JSON on primary surfaces

## API

Existing: `GET /api/status`, `/api/paper`, `/api/paper/metrics`, `/api/analysis`  
New: `GET /api/dashboard` (Home bundle), `GET /api/approvals?status=pending`  
POSTs return JSON when `Accept: application/json` (else 303 for legacy forms).

## Success criteria

- Home shows Approve/Reject without scrolling past dumps
- Kill engage disables approve path
- Charts render empty and non-empty states
- Tests cover JSON POST + SPA index + auth
