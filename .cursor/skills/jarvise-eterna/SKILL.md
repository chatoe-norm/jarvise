---
name: jarvise-eterna
description: Eterna MCP Gateway (mcp.eterna.exchange) knowledge for Jarvise — research overlay and future venue candidate only. Use when the user mentions Eterna, eterna-mcp, ai.eterna.exchange, Eterna Institutional, an MCP exchange for AI agents, or asks whether Jarvise can trade through an MCP server. Never places orders, moves funds, or writes market data into SQLite or the paper ledger.
---

# Jarvise Eterna (Cursor)

Canonical map: [`docs/exchange/eterna-for-jarvise.md`](../../../docs/exchange/eterna-for-jarvise.md). **Read it in full and follow it** — adopt / exclude / deny tables, auth notes, and the future venue path. This file only adds Cursor-specific notes.

Upstream: [eterna-mcp](https://github.com/EternaHybridExchange/eterna-mcp), [llms.txt](https://ai.eterna.exchange/llms.txt), [MCP guide](https://eterna.exchange/guides/mcp-crypto-trading), [Institutional](https://institutional.eterna.exchange/).

## What it is (one line)

Remote Streamable HTTP MCP with three tools (`search_sdk`, `search_examples`, `execute_code`) that runs TypeScript against an `eterna.*` SDK in a Deno sandbox: market data, TA, **trading**, account, and **funding/withdrawal** on USDT perps (default) and USDT spot, with a dedicated sub-account per agent via OAuth `mcp:full`.

## Jarvise stance

| Layer | Eterna role |
|-------|-------------|
| Numeric truth (OHLCV / book / macro) | Not Eterna — `jarvise ingest` → SQLite |
| Agent research | Optional overlay, only when the owner enables the MCP |
| Paper ledger / auto-decide | Never filled from Eterna output |
| Live orders | Never via MCP; `JARVISE_LIVE_TRADING=false` |
| Venue adapter | Candidate only — spot read-only first, through `VenueClient` |
| Doctrine RAG | Never index Eterna docs, skills, or `strategy_*` prompts |

## Cursor workflow

1. Run `jarvise-notebook` first for doctrine (risk rules, FLAT threshold, paper → manual approval → autonomy).
2. Answer Eterna questions from the canonical map; cite it. Do not add `https://mcp.eterna.exchange/mcp` to Cursor MCP settings unless the owner explicitly asks — the only OAuth scope is `mcp:full`, which includes trading and withdrawal.
3. If the MCP is enabled by the owner: use `search_sdk` / `search_examples` for technical lookup and `execute_code` **only** with read methods (`getTickers`, `getOrderbook`, `getInstruments`, `getRsi`, `getMacd`, `getEma`, `getSma`, `getBollingerBands`, `getVwap`). Treat results as chat context; never write them into `jarvise.db`.
4. For a venue evaluation request, point to the "Future venue adapter" table in the map and keep the work inside `src/jarvise_exchange/` behind the existing contract.

## Never

- Call `placeOrder`, `sellSpotBalance`, `closePosition`, `cancelOrder`, `cancelAllOrders`, `setLeverage`, `setTradingStop`.
- Call `getDepositAddress`, `transferToTrading`, `swapToUsdt`, `submitWithdrawal`, or any funding method.
- Install `@eterna-hybrid-exchange/openclaw-plugin` or `@eterna-hybrid-exchange/cli` on the VPS, or run `eterna login` from VPS hosts.
- Copy Eterna strategy prompts, Claude skills bundle, or SDK docs into `data/analytics/sources/` or `jarvise_doctrine`.
- Silent-merge Eterna tickers, order books, or TA into `market_technicals`, `market_microstructure`, or the paper ledger.
- Read `.env` or `printenv` for this skill; it needs no credentials while paper-only.
