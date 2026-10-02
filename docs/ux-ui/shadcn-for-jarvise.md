# shadcn/ui for Jarvise (UX / UI)

**Paper-first control plane.** This page maps the official [shadcn/ui Installation](https://ui.shadcn.com/docs/installation) docs into what Jarvise may adopt for a future analytics UI. It does **not** mean the shipped stack is React/shadcn today.

Local digest: [`data/analytics/sources/shadcn-ui-installation-jarvise.txt`](../../data/analytics/sources/shadcn-ui-installation-jarvise.txt)  
Machine index: [llms.txt](https://ui.shadcn.com/llms.txt)

## Source of truth

| Layer | Where |
|-------|--------|
| Official install | [Installation](https://ui.shadcn.com/docs/installation) |
| CLI / config | [CLI](https://ui.shadcn.com/docs/cli), [components.json](https://ui.shadcn.com/docs/components-json) |
| AI skills | [Skills](https://ui.shadcn.com/docs/skills) |
| Jarvise filter | This doc + local digest |
| Shipped UI | FastAPI `src/jarvise_web/app.py` — `/` + `/analytics` |

## What Jarvise ships today (Technical)

| Surface | Implementation |
|---------|----------------|
| Control web | FastAPI HTML, kill-switch / status |
| Analytics | Server-rendered `/analytics` + JSON APIs (`/api/analysis`, `/api/paper`, …) |
| Styling | Inline CSS in `jarvise_web` — **not** Tailwind / shadcn |
| Deploy | Hostinger VPS + Tailscale (`:8080`); see [`docs/deploy/hostinger-vps.md`](../deploy/hostinger-vps.md) |

P1 plan: [`../superpowers/plans/2026-09-23-p1-analytics-ui.md`](../superpowers/plans/2026-09-23-p1-analytics-ui.md).

## What shadcn Installation offers

From the official Installation page (scraped 2026-09-29):

1. **shadcn/create** — visual preset → framework-specific setup command (Next.js, Vite, Laravel, React Router, Astro, TanStack Start).
2. **CLI scaffold** — templates `next`, `vite`, `start`, `react-router`, `astro` (Laravel: create app first, then `init`).
3. **Existing project** — framework guide “Existing Project” path → `init` + `components.json`.

Related: CSS-variable theming, Typeset, Dark Mode, Registry, and optional [AI Skills](https://ui.shadcn.com/docs/skills) that activate on `components.json` and call `shadcn info --json`.

## Jarvise adopt / defer matrix (UX/UI)

| Capability | Jarvise |
|------------|---------|
| Read official docs + digest for UI planning | **Allowed** (knowledge) |
| Keep FastAPI `/analytics` as SoT until a rewrite plan | **Required today** |
| Scaffold Vite/Next + shadcn in-repo | **Deferred** — owner-approved roadmap slice only |
| Replace Approve/Reject / paper banners with generic marketing UI | **Forbidden** |
| Soft-fail exchange panel; never leak keys to the client | **Required** |
| Official shadcn Skills / MCP inside a future frontend package | **Optional after** that package has `components.json` |
| Treat Vercel CTAs as Jarvise deploy path | **Denied** — VPS + Tailscale remains deploy SoT |

### Preferred components (if a rewrite is approved)

Analytics and approval UX maps cleanly to: **Card**, **Table / Data Table**, **Chart**, **Button**, **Alert Dialog**, **Badge**, **Tabs**, **Alert**, **Skeleton**, **Empty**. Forms for filters: **Field** / **Select** / **Input**. Do not invent live-order chrome beyond the paper approval contract (and later P4-C only when gated).

## Hard rules

| Rule | Behavior |
|------|----------|
| Current phase | Paper analytics + approval UI; no live order placement UI until P4-C is approved and gated |
| Stack default | Python FastAPI HTML until an explicit UI rewrite lands |
| Secrets | Never in client bundles or HTML responses |
| Kill-switch | Always visible / respected on control and analytics surfaces |
| Deploy | Hostinger + Tailscale; not public HTTPS marketing hosting by default |

## Skills (agents)

- Cursor: [`.cursor/skills/jarvise-ux-ui/SKILL.md`](../../.cursor/skills/jarvise-ux-ui/SKILL.md) — load when changing `/analytics` or planning a frontend rewrite.
- Trading doctrine skills (`jarvise-notebook`, OpenClaw pack) stay market/risk focused; they only point here so agents do not silently React-scaffold the monorepo.

## RAG allowlist

[`config/rag-sources.json`](../../config/rag-sources.json) may fetch:

- `https://ui.shadcn.com/llms.txt`
- `https://ui.shadcn.com/docs/installation` (via Firecrawl; SPA)
- Related CLI / skills pages as listed in the config

Prefer Firecrawl or the digest over empty client-rendered shells when HTML fetch returns no body.
