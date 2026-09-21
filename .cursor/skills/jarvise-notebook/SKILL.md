---
name: jarvise-notebook
description: Queries the Jarvise Gemini Notebook (crypto trading knowledge base) before market analysis or trade decisions. Use when analyzing charts, fetching market intel, paper/live trading, or when the user mentions Jarvise, NotebookLM, Gemini Notebook, or notebook 14e11c63-e2ee-4b49-898f-b0cc4c61cb4e.
---

# Jarvise Notebook

Trading knowledge lives in Gemini Notebook **Jarvise : Crypto Trader**. Read [`config/notebook.json`](../../../config/notebook.json) for id, alias, and profile. Query it before analyzing markets or recommending trades.

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

1. `notebook_get` / `source_list_drive` with `skip_freshness=true` if you need source inventory.
2. `notebook_query` with the notebook id. Ask for principles, risk rules, venue constraints, and anything matching the current symbol/setup.
3. Combine notebook context with live chart/news data. Notebook is doctrine, not a price feed.
4. If query fails with auth errors, re-auth (above) and retry once.

## Query style

Ask concrete questions, for example:

- What risk guardrails and deployment stages must the agent follow?
- What does this notebook say about paper trading vs live autonomy?
- Any Binance TH or capital-preservation rules that veto this setup?

Cite notebook guidance in the decision (`BUY` / `SELL` / `FLAT`) so the owner can see which rule applied.

## Do not

- Treat notebook answers as live prices or order-book state.
- Skip risk/paper-first rules from the sources to chase a trade.
- Delete sources, run studio generation, or switch the default `nlm` profile away from `chatoe` without the owner asking.
