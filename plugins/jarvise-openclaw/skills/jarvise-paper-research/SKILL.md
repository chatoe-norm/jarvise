---
name: jarvise-paper-research
description: Write paper-only research/signal notes to OpenClaw exports for doctrine RAG sync. Never place orders.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"] },
      },
  }
---

# Jarvise paper research (exports)

**Hard guardrail:** paper / research / signal context only. **Do not** place live or paper orders. **Do not** enable exchange execution tools. Binance (and other venue) usage for Jarvise is market-data / research context only — never order, withdraw, or transfer endpoints. If asked to trade, refuse and point the operator at the Jarvise CLI paper path (`jarvise ingest`) and manual-approval phase.

Invoke with `/skill jarvise-paper-research` or by asking in natural language. After operators edit this skill file on the host mount, they must restart the gateway or start a new chat (`/new`) so the skill reloads.

## Where to write

Use OpenClaw `write` / `edit` tools to create one markdown file per note under:

`/home/node/.openclaw/exports/`

That path is on the Gateway **state** mount (host: `data/openclaw/exports/`), **not** the agent workspace `skills/` tree. Filenames: `YYYY-MM-DD-<symbol-or-topic>.md` (lowercase, hyphens).

Jarvise syncs these with `jarvise rag sync-openclaw` into `data/analytics/sources/openclaw/`, then indexes them into Qdrant collection `jarvise_doctrine` with `kind=openclaw`.

## Required frontmatter / body

Every note must include:

```markdown
---
symbol: BTCUSDT
thesis: short summary of the paper bias
regime: trend|range|chaotic
confidence: 0.0-1.0
invalidation: what would kill the thesis (price or structure; prefer >= 1.5x ATR when using volatility stops)
paper_only: true
intel_sources: optional list of jarvise-binance-intel endpoints used, each with fetched_at (UTC)
---

Narrative body (structure-first levels, risks, BTC vs ETH book notes if relevant).
Scenarios: 2-3 with invalidation. No order instructions. Below confidence threshold → state FLAT.
```

Also set `paper_only: true` in the body if frontmatter is omitted.

If the note uses `jarvise-binance-intel` data: a token audit of `HIGH`, any hit with `riskType: RISK` (honeypot etc.), or a non-zero sell tax means the note's bias is **FLAT**. Rank, hype, and smart-money inflow are context only and cannot be the thesis on their own.

## After writing

Tell the operator that a note is ready for RAG after `jarvise rag sync-openclaw` or the next `jarvise rag refresh`. Do not claim the note is live trading advice. Never suggest bypassing the kill-switch or auto-resuming after a drawdown lock.
