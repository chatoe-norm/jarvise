# CoinCap MCP for Jarvise

**Optional agent research overlay only. Not wired.** This page maps [QuantGeekDev/coincap-mcp](https://github.com/QuantGeekDev/coincap-mcp) into what Jarvise may and may not do. It is **technical knowledge**, not trading doctrine — do not index this page into `jarvise_doctrine`.

Provider map: [`data/analytics/api-map.json`](../../data/analytics/api-map.json)  
Parallel overlay maps: [CoinGecko](coingecko-for-jarvise.md), [TradingView](../product-usage.md#research-overlay-vs-controlled-truth)

## Feature report

| Item | Detail |
|------|--------|
| Package | `coincap-mcp` npm **0.9.3** (MIT), Node 18+ |
| MCP SDK | `@modelcontextprotocol/sdk` **0.6.0** (older stdio stack) |
| Install | `npx coincap-mcp` or Smithery `npx -y @smithery/cli install coincap-mcp --client claude` |
| Auth | None — public CoinCap REST |
| Upstream API | `https://api.coincap.io/v2/assets/...` ([`src/constants.ts`](https://github.com/QuantGeekDev/coincap-mcp/blob/main/src/constants.ts)) |
| Transport | **stdio** local process (not Streamable HTTP) |
| Tools (3) | Bitcoin price (`GET /v2/assets/bitcoin`); get crypto price by id (`GET /v2/assets/{id}`); list assets |
| Sample use | “price of bitcoin”, “market cap of ethereum”, “list assets” |
| Caveats | Upstream README sample config mistakenly names the server `"mongodb"`; community-maintained; CoinCap v2 may rate-limit or change — **not** Jarvise source of truth |

**Jarvise value:** thin agent convenience for spot-check prices / asset list in chat. Does **not** replace Binance klines ingest, CoinGecko `/global` macro, or paper fills. Overlaps CoinGecko MCP for “what’s the price?” with a smaller surface area.

## Source of truth

| Layer | Where |
|-------|--------|
| MCP repo | [QuantGeekDev/coincap-mcp](https://github.com/QuantGeekDev/coincap-mcp) |
| Upstream REST | `https://api.coincap.io/v2` |
| Smithery | [smithery.ai/server/coincap-mcp](https://smithery.ai/server/coincap-mcp) |
| Jarvise filter | This doc + `api-map.json` CoinCap MCP provider |
| Doctrine RAG | **Out of scope** |

## What Jarvise uses today

| Capability | Jarvise |
|------------|---------|
| OHLCV / book / indicators | **Not from CoinCap** — `jarvise ingest` (Binance public) → SQLite |
| Macro (BTC dominance, global mcap) | **Not from CoinCap** — CoinGecko `GET /api/v3/global` via `coingecko_global` |
| CoinCap MCP tools (price / list assets) | **Optional research overlay** — not installed; Cursor/Claude chat only if owner enables |
| CoinCap as SQLite / paper writer | **Not wired** — never |
| CoinCap as `market_safety` macro provider | **Deny** — list/price ≠ dominance / global mcap |

## Layer separation

Same rule as TradingView MCP and CoinGecko AI:

| Layer | Source | Role |
|-------|--------|------|
| Numeric truth | `jarvise ingest` → `jarvise.db` | Drives analyze, market-safety, paper |
| Research overlay | CoinCap MCP (`npx coincap-mcp`) | Cursor / Claude chat context only |
| Doctrine | Gemini Notebook + owner extracts | Never CoinCap docs or this map |

**Never** silent-merge CoinCap prices, market caps, or asset lists into `market_technicals`, `macro_onchain_sentiment`, or the paper ledger.

## Adopt / exclude / deny

| Item | Jarvise stance |
|------|----------------|
| Bitcoin price / get crypto price / list assets tools | **Adopt as research** when owner enables MCP locally |
| VPS / jobs / n8n install | **Exclude** — not on the 24/7 path |
| Silent-merge → SQLite / paper / live fills | **Deny** |
| Doctrine RAG indexing | **Deny** |
| Replacing CoinGecko macro with CoinCap | **Deny** |
| Dedicated Cursor skill | **Not added** — redundant with notebook + SQLite + CoinGecko map |

## MCP notes (reference only — not installed)

Correct Cursor / Claude Desktop-style config (do **not** copy the upstream `"mongodb"` key name):

```json
{
  "mcpServers": {
    "coincap": {
      "command": "npx",
      "args": ["-y", "coincap-mcp"],
      "env": {
        "JARVISE_PAPER_ONLY": "true"
      }
    }
  }
}
```

Jarvise keeps this under `optional_overlays_not_enabled` in [`mcp/jarvise-mcp.json.example`](../../mcp/jarvise-mcp.json.example) — out of active `mcpServers` until the owner opts in locally.

## Hard rules

| Rule | Behavior |
|------|----------|
| Current phase | Paper analytics; CoinCap MCP not wired |
| Ingest | Stays controlled GET-only providers → SQLite |
| Paper | Never filled from CoinCap MCP output |
| Doctrine RAG | Never index CoinCap docs |
| Secrets | None required; still no secrets in UI/logs |

## Related

- [CoinGecko for Jarvise](coingecko-for-jarvise.md) — controlled macro + optional AI overlay
- [Product usage — research overlay vs controlled truth](../product-usage.md)
- [`data/analytics/api-map.json`](../../data/analytics/api-map.json)
