---
name: jarvise-binance-intel
description: Read-only Binance public intel for paper research — token search/meta/dynamic, token security audit, market rank and social hype, smart-money inflow, Ondo tokenized US stocks, Binance Academy risk education. No API keys. Never place orders.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"], "bins": ["curl"] },
      },
  }
---

# Jarvise Binance intel (read-only)

**Hard guardrail:** paper / research context only. **No order placement**, no swaps, no transfers, no wallet actions. Every call below is public and unauthenticated. Outputs are context for a Jarvise decision, never a trigger on their own. If asked to trade, refuse and point the operator at the Jarvise paper path and manual-approval phase.

This is the Jarvise subset of the [Binance Skills Hub](https://developers.binance.com/en/docs/sdks-tools/integrations/skills-hub) ([repo](https://github.com/binance/binance-skills-hub)). Adopt/exclude/deny matrix: `data/analytics/sources/binance-skills-hub-jarvise.txt`. Invoke with `/skill jarvise-binance-intel` or in natural language. After editing this file on the host mount, restart the gateway or start a new chat (`/new`).

## Never

- Install hub skills (`npx skills add ...`), or run `binance-cli`, `baw`, the Binance Agentic Wallet, or the Binance MCP Server.
- Send API keys, signatures, or cookies. This skill needs no credentials; never read or print secrets files.
- Call any host or path not listed below. No polling loops: one call per question, back off on HTTP 429.
- Describe a token as "safe", or present rank / hype / inflow as a buy or sell signal.

## Request conventions

- Headers on every call: `Accept-Encoding: identity` and `User-Agent: binance-web3/2.0 (Skill)`. Add `Content-Type: application/json` on POST.
- Success envelope: `"code": "000000"` (most endpoints also return `"success": true`).
- Chains: Ethereum `1`, BSC `56`, Base `8453`, Solana `CT_501`. Tokenized stocks: `1`, `56`.
- Numbers arrive as strings: convert with Decimal before arithmetic. Timestamps are Unix milliseconds UTC.
- Relative `icon` / `logo` / attestation paths: prefix the `bin.bnbstatic.com` host.

## Endpoint allowlist

Token info and audit (`web3.binance.com`):

| Method | Path | Params | Use |
|--------|------|--------|-----|
| GET | `/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai` | `keyword`, `chainIds` | Find a token; confirm contract |
| GET | `/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai` | `chainId`, `contractAddress` | Name, socials, creator |
| GET | `/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai` | `chainId`, `contractAddress` | Price, volume, holders, liquidity |
| POST | `/bapi/defi/v1/public/wallet-direct/security/token/audit` | `binanceChainId`, `contractAddress`, `requestId` (UUID v4) | Honeypot / tax / contract risk |

Market rank (`web3.binance.com`, context only):

| Method | Path | Params | Use |
|--------|------|--------|-----|
| POST | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list/ai` | `rankType` (`10` trending, `11` top search, `20` alpha, `40` stock), `chainId`, `period` (`50` = 24h), `page`, `size` | Crowding / attention |
| GET | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/social/hype/rank/leaderboard/ai` | `chainId` (`56`, `8453`, `CT_501`), `targetLanguage=en`, `timeRange=1` | Social hype + sentiment |
| POST | `/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai` | `chainId`, `period` (`1h`/`4h`/`24h`), `tagType=2` | Smart-money net inflow |

Tokenized US stocks — Ondo (`www.binance.com`):

| Method | Path | Params | Use |
|--------|------|--------|-----|
| GET | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai` | `type=1` | Ticker → `chainId`, `contractAddress`, `multiplier` |
| GET | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/meta/ai` | `chainId`, `contractAddress` | Company info, attestation reports |
| GET | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/market/status/ai` | — | Market session open/closed |
| GET | `/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/asset/market/status/ai` | `chainId`, `contractAddress` | Per-asset pause / corporate action |
| GET | `/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/rwa/dynamic/ai` | `chainId`, `contractAddress` | On-chain price + P/E, dividend yield, 52w range |
| GET | `/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/kline/ai` | `chainId`, `contractAddress`, `interval` (`1h`/`4h`/`1d`), `limit` (max 300) | Token OHLC |

Binance Academy (`www.binance.com`, all POST, education only):

| Path | Body | Use |
|------|------|-----|
| `/bapi/bigdata/v1/public/bigdata/academy-skill/searchGlossary` | `{"query","lang":"en","limit":3}` | Term definitions |
| `/bapi/bigdata/v1/public/bigdata/academy-skill/searchResource` | `{"query","lang":"en","limit":3}` | Courses / tracks |
| `/bapi/bigdata/v1/public/bigdata/academy-skill/searchLearnEarn` | `{"query","lang":"en","limit":3}` | Learn & Earn courses |
| `/bapi/bigdata/v1/public/bigdata/academy-skill/searchArticles` | `{"query","language":"en","docCount":3}` | Articles, risk education |

For CEX spot candles (BTCUSDT, ETHUSDT), do **not** use these endpoints: use Jarvise SQLite price-graph from `jarvise ingest` (Binance `GET /api/v3/klines`). The upstream `query-token-info` kline proxies a third-party host and is excluded.

## Examples

```bash
H1='Accept-Encoding: identity'; H2='User-Agent: binance-web3/2.0 (Skill)'

# Find a token, then confirm the exact contract (search returns look-alikes)
curl -s -H "$H1" -H "$H2" \
  'https://web3.binance.com/bapi/defi/v5/public/wallet-direct/buw/wallet/market/token/search/ai?keyword=BTCB&chainIds=56'

# Audit before any thesis on an on-chain token
curl -s -X POST -H "$H1" -H "$H2" -H 'Content-Type: application/json' \
  'https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit' \
  -d "{\"binanceChainId\":\"56\",\"contractAddress\":\"0x...\",\"requestId\":\"$(uuidgen | tr 'A-Z' 'a-z')\"}"

# Tokenized stock: list → per-asset status → dynamic
curl -s -H "$H1" -H "$H2" \
  'https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai?type=1'

# Risk education for leverage / all-in questions
curl -s -X POST -H "$H1" -H "$H2" -H 'Content-Type: application/json' \
  'https://www.binance.com/bapi/bigdata/v1/public/bigdata/academy-skill/searchArticles' \
  -d '{"query":"leverage risk","language":"en","docCount":3}'
```

## Doctrine mapping

| Input | Jarvise rule |
|-------|--------------|
| Audit `riskLevelEnum: HIGH`, any detail with `isHit: true` and `riskType: RISK` (honeypot, etc.), non-zero `sellTax` | Veto → **FLAT**. `LOW` only means "no known red flags". |
| Audit `riskLevelEnum: MID` (upstream docs say `MEDIUM`; live API returns `MID`) | Not a veto alone: list every `isHit: true` detail. `CAUTION` hits (e.g. "Mintable" on major stablecoins) lower confidence; they do not kill the thesis. |
| Search hit with tiny price or unknown contract for a major ticker | Treat as look-alike until contract is confirmed and audited |
| Trending / top-search / hype / smart-money inflow | Crowding and sentiment context, like oscillators: never a standalone trigger. Extreme hype raises caution. |
| Tokenized stock `marketStatus` `closed` / `pause`, or a corporate-action `reasonCode` | No thesis across the halt. `referencePrice = price / multiplier`; US volume is in shares. |
| Leverage, borrowing, "all-in", 20x requests | Academy risk education + doctrine: spot paper only, fractional Kelly, stops ≥ 1.5× ATR, kill-switch |

Always combine with Gemini Notebook doctrine (`jarvise-notebook`) and `jarvise-doctrine-rag`, plus the SQLite price-graph, before any `BUY` / `SELL` / `FLAT` call. Default under uncertainty: **FLAT**.

## Output and handoff

- Cite each endpoint used with a `fetched_at` (UTC) and the `chainId` + `contractAddress`.
- To keep a note for RAG, write it with `jarvise-paper-research` and list the endpoints under `intel_sources:`.
- If a call fails or returns `success: false`, say so and continue without that input. Do not guess values.
