# Eterna MCP for Jarvise

**Research overlay + future venue candidate. Not wired, not live.** This page maps the [Eterna MCP Gateway](https://github.com/EternaHybridExchange/eterna-mcp) into what Jarvise may and may not do. It is **technical knowledge**, not trading doctrine — do not index this page, Eterna vendor skills, or Eterna strategy prompts into `jarvise_doctrine`.

Machine facts: [llms.txt](https://ai.eterna.exchange/llms.txt)  
Setup guide: [MCP crypto trading](https://eterna.exchange/guides/mcp-crypto-trading)  
White-label (platforms): [institutional.eterna.exchange](https://institutional.eterna.exchange/)  
Provider map: [`data/analytics/api-map.json`](../../data/analytics/api-map.json)  
Cursor skill: [`.cursor/skills/jarvise-eterna/SKILL.md`](../../.cursor/skills/jarvise-eterna/SKILL.md)

## Source of truth

| Layer | Where |
|-------|--------|
| Gateway overview | [eterna-mcp README](https://github.com/EternaHybridExchange/eterna-mcp) |
| Quickstart (Claude / Cursor / OpenClaw) | [QUICKSTART.md](https://github.com/EternaHybridExchange/eterna-mcp/blob/main/QUICKSTART.md) |
| Tools + SDK reference | [docs/tools-reference.md](https://github.com/EternaHybridExchange/eterna-mcp/blob/main/docs/tools-reference.md) |
| Auth | [docs/authentication.md](https://github.com/EternaHybridExchange/eterna-mcp/blob/main/docs/authentication.md), [auth.md](https://ai.eterna.exchange/auth.md) |
| Agent discovery | [MCP server card](https://ai.eterna.exchange/.well-known/mcp/server-card.json), [API catalog](https://ai.eterna.exchange/.well-known/api-catalog), [agent skills index](https://ai.eterna.exchange/.well-known/agent-skills/index.json) |
| Jarvise filter | This doc + `api-map.json` Eterna provider |
| Doctrine RAG | **Out of scope** — keep Eterna skills / strategy prompts out of Qdrant |

## What Eterna is

A managed, remote MCP server (`https://mcp.eterna.exchange/mcp`, Streamable HTTP) that gives an agent a dedicated exchange sub-account. It is **not** one MCP tool per exchange endpoint:

1. Agent calls `search_sdk` to find an `eterna.*` method.
2. Agent calls `execute_code` with TypeScript that uses the SDK in one round-trip.
3. Code runs in a sandboxed Deno runtime; result returns as JSON.

| Surface | Contents |
|---------|----------|
| MCP tools (3) | `execute_code`, `search_sdk`, `search_examples` |
| Prompts | `getting_started`, `sdk_reference`, `error_handling`, `technical_analysis`, `strategy_momentum_scalping`, `strategy_mean_reversion`, `strategy_funding_rate_arbitrage` |
| Resources | `eterna://docs/sdk`, `eterna://docs/errors`, `eterna://docs/examples` |
| SDK — market data | `getTickers`, `getOrderbook`, `getInstruments` (`market`: `linear` default or `spot`) |
| SDK — technical analysis | `getRsi`, `getMacd`, `getEma`, `getSma`, `getBollingerBands`, `getVwap` |
| SDK — trading | `placeOrder`, `sellSpotBalance`, `closePosition`, `cancelOrder`, `cancelAllOrders`, `setLeverage`, `setTradingStop` |
| SDK — account | `getBalance`, `getAccountInfo`, `getAllCoinsBalance`, `getPositions`, `getOrders` |
| SDK — funding | `getDepositAddress`, `getDepositRecords`, `getAllowedDepositCoins`, `transferToTrading`, `swapToUsdt`, `getCoinInfo`, `getWithdrawableAmount`, `submitWithdrawal`, `getWithdrawalStatus` |
| Markets | USDT-margined linear perpetuals (200+ pairs, SDK default) and USDT spot |
| Auth | OAuth (`mcp:full`) via `ai-auth.eterna.exchange`; legacy long-lived Bearer API key |
| Distribution | Cursor / Claude MCP config; OpenClaw plugin `@eterna-hybrid-exchange/openclaw-plugin`; CLI `@eterna-hybrid-exchange/cli` |
| Vendor claims | $10B+ liquidity, 0.014% maker / 0.035% taker on futures, <200 ms, no KYC, agent auto-provisioned on first sign-in |

Eterna Institutional licenses the same engine as white-label products for **platforms** (branded AI trading agent, brokerage/liquidity routing, token launch stack, full exchange) with fee share back to the partner. Jarvise is an owner-operated CLI/dashboard, not a platform — this is reference only.

## What Jarvise uses today

| Capability | Jarvise |
|------------|---------|
| OHLCV / order-book / macro numeric truth | **Not from Eterna** — stays `jarvise ingest` (Binance public klines + book, CoinGecko `/global`, free-first derivatives) → SQLite |
| `search_sdk`, `search_examples`, resources | **Optional agent research** — not installed; read vendor docs through the MCP when the owner enables it |
| `execute_code` with read methods (`getTickers`, `getOrderbook`, `getInstruments`, TA getters) | **Optional overlay** — chat context only; never written to SQLite, never a paper trigger alone |
| `getBalance` / `getPositions` / `getOrders` | **Display-only if ever enabled** — same soft-fail rule as the Binance read-only exchange panel |
| `placeOrder`, `sellSpotBalance`, `closePosition`, `cancelOrder`, `cancelAllOrders`, `setLeverage`, `setTradingStop` | **Forbidden** — live stays `JARVISE_LIVE_TRADING=false`; no MCP may submit orders |
| Deposit / `transferToTrading` / `swapToUsdt` / `submitWithdrawal` | **Forbidden** — Jarvise never moves funds from an agent session |
| Vendor strategy prompts (`strategy_*`) and Claude skills bundle | **Exclude** — scalping / funding-arb templates conflict with slow, steady doctrine; never index |
| OpenClaw plugin / `eterna login` on the VPS | **Not installed** — OpenClaw stays `OPENCLAW_PAPER_ONLY=true` |
| Institutional white-label | **Out of scope** |

## Layer separation

Same rule as TradingView MCP and CoinGecko AI: agents may use Eterna tools for research; numeric market truth stays in controlled ingest → SQLite → market-safety; fills come only from the paper ledger via Approve.

| Layer | Source | Role |
|-------|--------|------|
| Numeric truth | `jarvise ingest` → `jarvise.db` | Drives analyze, market-safety, paper |
| Research overlay | Eterna MCP (`search_sdk`, read-only `execute_code`) | Cursor / OpenClaw chat context only |
| Paper ledger | `jarvise paper` → `approve_approval` | Never filled from Eterna output |
| Live submit | `jarvise_trade` (gated off) | Never via Eterna MCP |
| Doctrine | Gemini Notebook + owner extracts | Never Eterna skills or this map |

**Never** silent-merge Eterna tickers, order-book snapshots, or vendor TA into `market_technicals`, `market_microstructure`, or the paper ledger.

## Adopt / exclude / deny

| Item | Jarvise stance |
|------|----------------|
| `search_sdk`, `search_examples`, `eterna://docs/*` | **Adopt as research** (when MCP enabled) — technical lookup only |
| `execute_code` → market data + TA getters | **Adopt as research** — oversight: output is overlay, never truth |
| `execute_code` → `getBalance`, `getPositions`, `getOrders` | **Display-only** — only with an owner-provisioned sub-account; never cached into SQLite |
| `execute_code` → trading methods | **Deny** until the [live-enable checklist](../ops/live-enable-checklist.md) is complete **and** an Eterna adapter has passed paper → manual approval |
| `execute_code` → funding / withdrawal methods | **Deny** permanently from agent sessions |
| `setLeverage`, perps in general | **Deny** — Jarvise paper is spot-only; derivatives are a market-safety input, not a product |
| `strategy_*` prompts, vendor Claude skills, `/fight` leaderboard | **Exclude** — not doctrine; do not copy into `jarvise_doctrine` |
| OpenClaw plugin on VPS | **Exclude** this pass |
| Legacy long-lived Bearer API key | **Exclude** — prefer OAuth scoped per device if ever enabled; keys never in repo or UI |
| Institutional white-label | **Out of scope** |

## Auth and security

- OAuth `mcp:full` is the only scope offered; it covers trading and funding. Treat any Eterna sign-in as **full-account** access for that agent — do not sign in from the VPS OpenClaw or n8n hosts while paper-only.
- No KYC is a diligence item, not a feature: compare against the owner's regulated-venue preference (Binance TH / Thai SEC in `api-map.json`) before any live evaluation.
- The README benchmarks against "wrapping every Bybit call"; confirm the underlying liquidity venue, custody, and jurisdiction before treating Eterna as a Jarvise venue.
- Never log OAuth tokens; never put them in HTML/JSON UI responses or `.env.example`.

## Market-safety note (perps)

Eterna defaults to linear perpetuals. Jarvise's `market_safety` fails closed on stale or unsafe **derivatives** data (funding, OI, liquidations) as context for **spot** paper decisions. Perp execution would need a separate risk model (leverage, liquidation price, funding drag) that Jarvise does not have — do not reuse spot risk caps for perps.

## Venue adapter status (T2.5)

| Piece | Status |
|-------|--------|
| Registry | `jarvise_exchange.registry.resolve_venue_client("binance"|"eterna")`; env `JARVISE_EXCHANGE_VENUE` (default `binance`) |
| CLI | `jarvise exchange venues`; `jarvise exchange sync-balances --venue eterna` |
| `EternaSpotClient` | **Stub** — loads `ETERNA_SPOT_FIXTURE` JSON offline; otherwise raises `EternaReadApiBlocked` |
| Blocker | No documented **GET-only** spot-balance REST suitable for `VenueClient`. MCP `execute_code` is **forbidden** (scope includes trading + withdraw). |
| Next unblock | When Eterna publishes a signed read-only HTTP account API (no withdraw), implement real HTTP in `eterna_spot.py` behind the same client; keep fixture for tests. |

| Step | Requirement |
|------|-------------|
| 1. Read-only balances | In progress (fixture/blocker). Prefer GET-only REST from the [API catalog](https://ai.eterna.exchange/.well-known/api-catalog); never MCP. |
| 2. Paper evaluation | Spot symbols only; slippage from Eterna book; compare EV vs Binance. |
| 3. Manual approval | Owner Approve on the Command Dashboard; still no orders. |
| 4. Live (gated) | Only after the live-enable checklist + `jarvise_trade` path. No perps / leverage / funding methods. |

## Hard rules

| Rule | Behavior |
|------|----------|
| Current phase | Paper auto-decide; live off; Eterna not wired |
| Orders | Never via Eterna MCP, OpenClaw plugin, or CLI |
| Funds | Never deposit, transfer, swap, or withdraw from an agent session |
| Ingest | Stays controlled GET-only providers → SQLite; Eterna output is overlay |
| Paper | Never fill from Eterna signals, TA, or strategy prompts |
| Doctrine RAG | Never index Eterna docs, skills, or strategy prompts |
| Secrets | OAuth tokens and API keys never in repo, UI, or logs |

## Related

- [Binance APIs for Jarvise](binance-for-jarvise.md) — first venue adapter, same deny pattern for execution MCPs
- [CoinGecko for Jarvise](../market-data/coingecko-for-jarvise.md) — overlay vs controlled truth template
- [Live-enable checklist](../ops/live-enable-checklist.md)
- [`mcp/jarvise-mcp.json.example`](../../mcp/jarvise-mcp.json.example) — optional, gated Eterna entry
- [Product usage](../product-usage.md) — research overlay vs controlled truth
