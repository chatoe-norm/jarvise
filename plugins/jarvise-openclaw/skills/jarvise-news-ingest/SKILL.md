---
name: jarvise-news-ingest
description: Write a paper-only news digest for the OpenClaw ingest agent. n8n triggers the run. Never place orders and never write doctrine exports.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"] },
      },
    10|  }
---

# Jarvise news ingest (paper context)

**Hard guardrail:** paper / research context only. **Do not place orders.** If asked to trade, refuse and point the operator at the Jarvise paper path.

n8n (`infra/n8n/workflows/jarvise-news-ingest.json`) wakes agent `ingest` at 07:00 and 19:00 Asia/Bangkok. This skill is the operating note for that turn. It does not enqueue market-data ingest and it does not sync doctrine.

## Where to write

`/home/node/.openclaw/workspace-ingest/digests/YYYY-MM-DD-HH.md`

Follow `WATCHLIST.md` beside this agent's workspace copy of `AGENTS.md`. At most 8 items. Each item has a primary URL and a time.

## Never

- Do not write `/home/node/.openclaw/exports/`. That folder is what `jarvise rag sync-openclaw` copies into `jarvise_doctrine`.
- Do not write SQLite or the paper ledger.
- Do not change analyzer floors, `same_side_hold`, or live-trading flags.
- Do not place an order or approve a paper queue item.

Close the digest with: `Not a trade signal. Research context only.`
