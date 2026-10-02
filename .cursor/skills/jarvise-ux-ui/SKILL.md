---
name: jarvise-ux-ui
description: Jarvise analytics / control UI conventions and shadcn adoption rules. Use when editing /analytics, jarvise_web, planning a frontend rewrite, or when the user mentions shadcn, Tailwind, React UI, or UX/UI.
---

# Jarvise UX / UI

Shipped UI is the **Command Dashboard SPA** under [`web/`](../../../web/) (Vite/React, shadcn-style) served with FastAPI JSON APIs in [`src/jarvise_web`](../../../src/jarvise_web) (`Home` / `Paper` / `Decisions` / `Exchange` / `Ops` on `:8080`).

Canonical map: [`docs/ux-ui/shadcn-for-jarvise.md`](../../../docs/ux-ui/shadcn-for-jarvise.md)  
Digest: [`data/analytics/sources/shadcn-ui-installation-jarvise.txt`](../../../data/analytics/sources/shadcn-ui-installation-jarvise.txt)  
Upstream: [Installation](https://ui.shadcn.com/docs/installation), [Skills](https://ui.shadcn.com/docs/skills), [llms.txt](https://ui.shadcn.com/llms.txt)

## When editing the shipped UI

1. Prefer small HTML/CSS changes in `jarvise_web` that preserve existing cards, filters, and Approve/Reject.
2. Keep **PAPER ONLY** (or explicit **LIVE APPROVAL ENABLED** when that flag exists) visible.
3. Soft-fail exchange / missing-data panels — never render secrets.
4. Kill-switch and risk-cap messaging stay owner-protective; do not hide them for “cleaner” chrome.

## When the user asks for shadcn / React / Tailwind

1. Read the digest + `docs/ux-ui/shadcn-for-jarvise.md` first.
2. **Do not** run `npx shadcn@latest init|create` or add `components.json` at the repo root without an explicit owner-approved rewrite plan.
3. If a rewrite is approved: prefer a dedicated frontend package; Vite or Next.js per official install guides; keep JSON APIs as the contract with Python; deploy remains Hostinger + Tailscale (not Vercel-by-default).
4. Official shadcn AI Skills activate only after `components.json` exists in that frontend package.

## Prefer (future rewrite)

Card, Table/Data Table, Chart, Button, Alert Dialog, Badge, Tabs, Alert, Skeleton, Empty — mapped to paper ledger, expectancy, approval queue, exchange panel, risk caps.

## Never

- Silent SPA replacement of `/analytics`
- Live order buttons before P4-C is owner-gated
- Client-side API keys / secrets
- Unreviewed community registries as production UI source
