---
name: jarvise-notebook
description: Queries the Jarvise Gemini Notebook (crypto trading knowledge base) before market analysis or trade decisions. Use when analyzing charts, fetching market intel, paper/live trading, or when the user mentions Jarvise, NotebookLM, Gemini Notebook, or notebook 14e11c63-e2ee-4b49-898f-b0cc4c61cb4e.
---

# Jarvise Notebook

Trading knowledge lives in Gemini Notebook **Jarvise : Crypto Trader**. Read [`config/notebook.json`](../../../config/notebook.json) for id, alias, and profile. Query it before analyzing markets or recommending trades. Local extracts: `data/analytics/sources/jarvise-doctrine.txt`, `jarvise-analyzer-stack.txt`, `binance-api-intro-jarvise.txt`, `binance-skills-hub-jarvise.txt`, `api-map.json` (synced 2026-09-22; Binance digests 2026-09-23). Agent map: [`docs/exchange/binance-for-jarvise.md`](../../../docs/exchange/binance-for-jarvise.md).

## Notebook

| Field | Value |
| --- | --- |
| ID | `14e11c63-e2ee-4b49-898f-b0cc4c61cb4e` |
| Alias | `jarvise` |
| Account | `chatoe@gmail.com` (`nlm` profile `chatoe`) |
| URL | https://notebook.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e |

MCP server: `user-gemini-notebook-mcp` (Cursor name `gemini-notebook-mcp`).

## Auth

Sessions expire in about 20 minutes.

1. `nlm login switch chatoe`
2. If auth fails: `nlm login --profile chatoe`
3. MCP `refresh_auth`, then `server_info` until `auth_status` is `configured`

Do not use the `default` profile (`normstudiox@gmail.com`) for this notebook — it returns `PERMISSION_DENIED`.

## Before a trade or market analysis

1. `notebook_get` / `source_list_drive` with `skip_freshness=true` if you need source inventory (Drive count is often 0; web/generated sources dominate).
2. `notebook_query` with the notebook id. Ask for principles, risk rules, venue constraints, and anything matching the current symbol/setup.
3. Combine notebook context with local SQLite price-graph (OHLCV after ingest/backfill) **and** optional research overlays (e.g. TradingView MCP screener/MTF). Notebook is doctrine, not a price feed. Do **not** treat TV MCP output as the sole driver of paper fills or live orders.
4. If query fails with auth errors, re-auth (above) and retry once.

## Hard guardrails (from doctrine)

- Capital preservation > activity; prefer **FLAT** over low-confidence guesses.
- Deployment ladder: **paper → manual approval → autonomy**. Current Jarvise stack is **paper analytics / research only — no order placement**.
- Positive EV after fees/slippage before any live unlock; fractional Kelly only; stops ≥ ~1.5× ATR.
- Kill-switch / daily drawdown halt: lock until a **written human review** (never auto-resume).
- Structure first on charts; oscillators are context, never standalone triggers.
- **Venue-agnostic:** trade anywhere Jarvise can run efficiently, stably, and securely. Binance (incl. TH spot costs when relevant) is an example venue/cost model — not a forever lock.
- Binance APIs for Jarvise = public market data (`GET /api/v3/klines`) plus planned read-only spot account (`GET /api/v3/account`, P3). Never order/withdraw endpoints; never keys with withdrawal/transfer permission. See `binance-api-intro-jarvise.txt` and `docs/exchange/binance-for-jarvise.md`.
- Binance Skills Hub is used read-only through skill `jarvise-binance-intel` (public token info, token audit, rank/hype context, tokenized US stocks, Academy). Never install the hub, `binance-cli`, `baw`, the Agentic Wallet, or the Binance MCP Server. Token audit `HIGH` → FLAT; rank/hype is context, never a trigger. See `binance-skills-hub-jarvise.txt`.

## Query style

Ask concrete questions, for example:

- What risk guardrails and deployment stages must the agent follow?
- What does this notebook say about paper trading vs live autonomy?
- Any Binance TH or capital-preservation rules that veto this setup?
- Price-graph rules for ATR stops and EMA structure on this timeframe?

Cite notebook guidance in the decision (`BUY` / `SELL` / `FLAT`) so the owner can see which rule applied. Default to **FLAT** when confidence is below threshold.

## Do not

- Treat notebook answers as live prices or order-book state.
- Skip risk/paper-first rules from the sources to chase a trade.
- Place live or paper exchange orders from this skill.
- Call Binance (or any venue) order, withdraw, or transfer APIs; treat Binance Agent MCP/REST as execution tooling.
- Delete sources, run studio generation, or switch the default `nlm` profile away from `chatoe` without the owner asking.
