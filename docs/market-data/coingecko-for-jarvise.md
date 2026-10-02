# CoinGecko for Jarvise

**GET-only macro ingest + optional AI research overlay.** This page maps [CoinGecko AI Integration](https://docs.coingecko.com/ai-integration) into what Jarvise may and may not do. It is **technical knowledge**, not trading doctrine — do not index this page (or CoinGecko Learn) into `jarvise_doctrine`.

Machine index (reference): [llms.txt](https://docs.coingecko.com/llms.txt)  
Controlled writer: [`src/jarvise_ingest/providers/coingecko_global.py`](../../src/jarvise_ingest/providers/coingecko_global.py)  
Provider map: [`data/analytics/api-map.json`](../../data/analytics/api-map.json)

## Source of truth

| Layer | Where |
|-------|--------|
| AI Integration hub | [AI Integration](https://docs.coingecko.com/ai-integration) |
| Live data MCP | [CoinGecko MCP](https://docs.coingecko.com/ai-integration/mcp-server) |
| Docs MCP | [Docs MCP](https://docs.coingecko.com/ai-integration/docs-mcp) |
| Agent SKILL | [Agent SKILL](https://docs.coingecko.com/ai-integration/agent-skill) |
| CLI | [CoinGecko CLI](https://docs.coingecko.com/ai-integration/cli) |
| Cursor setup | [Cursor](https://docs.coingecko.com/ai-integration/cursor) |
| OpenClaw setup | [OpenClaw](https://docs.coingecko.com/ai-integration/openclaw) |
| Jarvise filter | This doc + `api-map.json` CoinGecko provider |
| Doctrine RAG | **Out of scope** — keep CoinGecko Learn / raw vendor docs out of Qdrant |

## What Jarvise uses today

| Capability | Jarvise |
|------------|---------|
| `GET /api/v3/global` (BTC dominance + total market cap) | **Allowed** — `coingecko_global` → `macro_onchain_sentiment` via `jarvise ingest`; uses Demo/Pro API key from env when set (`x-cg-demo-api-key` / `x-cg-pro-api-key`), else keyless public |
| Other CoinGecko REST / Pro / onchain / NFT endpoints as SQLite writers | **Not wired** — add only if they meet efficient/stable/secure bars and stay GET-only |
| CoinGecko MCP / Docs MCP / CLI as agent research | **Optional overlay** — not installed in this pass; never fill paper or silent-merge into SQLite |
| Official Agent SKILL (`npx skills add coingecko/skills`) | **Exclude by default** until reviewed |
| x402 pay-per-use | **Out of scope** |

Ingest keeps a raw HTTP GET (no `coingecko_sdk`). Vendor SDK prompts that say “ALWAYS use the SDK” apply to agent codegen experiments — **not** to `jarvise_ingest`, which stays simple, GET-only, and auditable.

## Layer separation

Same rule as TradingView MCP: agents may use CoinGecko AI tools for research; numeric market truth stays in controlled ingest → SQLite → market-safety.

| Layer | Source | Role |
|-------|--------|------|
| Numeric truth | `jarvise ingest` → `GET /api/v3/global` (keyed when env set) → `jarvise.db` | Macro for `market_safety`; kill-switch only when macro is **required** |
| Research overlay | CoinGecko MCP, Docs MCP, CLI, (future) reviewed SKILL | Cursor / OpenClaw chat context only |
| Doctrine | Gemini Notebook + owner doctrine sources | Never CoinGecko Learn or this technical map |

**Never** silent-merge MCP prices, trending pools, or CLI snapshots into `market_technicals`, `macro_onchain_sentiment` (outside the controlled writer), or the paper ledger.

## AI tools — adopt / exclude / deny

| Tool | Endpoint / install | Jarvise stance |
|------|--------------------|----------------|
| CoinGecko MCP (free, keyless) | `https://mcp.api.coingecko.com/mcp` | **Optional research overlay** — shared rate limits; not wired yet; never fill paper |
| CoinGecko MCP (API key) | `https://mcp.pro-api.coingecko.com/mcp` or local `@coingecko/coingecko-mcp` | **Optional research overlay** — higher limits; keys in env only if enabled later |
| Docs MCP | `https://docs.coingecko.com/mcp` | **Optional** agent doc lookup — technical only, not doctrine |
| Agent SKILL | `npx skills add coingecko/skills` | **Exclude by default** — full API surface; may push SDK patterns that conflict with ingest |
| CoinGecko CLI (`cg`) | brew / npm / install script | **Optional** local/agent shell research; not a SQLite writer |
| CoinGecko Learn / raw docs in Qdrant | — | **Deny** — doctrine allowlist unchanged |
| Silent-merge MCP → SQLite / paper | — | **Deny** |
| x402 | [x402](https://docs.coingecko.com/ai-integration/x402) | **Out of scope** |

Prefer Streamable HTTP `/mcp` over legacy `/sse` if MCP is enabled later. MCP tool calls count toward CoinGecko API rate limits / credits.

## MCP notes (reference only — not installed)

Cursor / Claude-style remote config examples from CoinGecko docs:

- Free: `npx mcp-remote https://mcp.api.coingecko.com/mcp`
- Keyed: `npx mcp-remote https://mcp.pro-api.coingecko.com/mcp`
- Docs: `npx mcp-remote https://docs.coingecko.com/mcp`

OpenClaw can set a local `@coingecko/coingecko-mcp` with `COINGECKO_DEMO_API_KEY` / `COINGECKO_PRO_API_KEY` and `COINGECKO_ENVIRONMENT`. Jarvise does **not** enable this on the VPS in this pass: MCP helps agent research, not paper/ingest/market-safety.

## Macro safety

- Macro is optional by default (`JARVISE_MARKET_SAFETY_REQUIRE_MACRO=0`): a CoinGecko `/global` fetch blip does **not** force FLAT and does **not** engage kill-switch. Last good `macro_onchain_sentiment` row in SQLite remains usable.
- When macro **is** required (`JARVISE_MARKET_SAFETY_REQUIRE_MACRO=1` or ingest without treating macro as skippable), a `macro:*` provider error is critical → FLAT + kill-switch.
- `--skip-macro` excludes the stream from required critical failures.
- See [market-safety ingest design](../superpowers/specs/2026-10-01-market-safety-ingest-design.md).

## Hard rules

| Rule | Behavior |
|------|----------|
| Ingest | Public GET `/api/v3/global` only (today); raw HTTP, no SDK; Demo key → `x-cg-demo-api-key`, Pro key → `pro-api` + `x-cg-pro-api-key` |
| Paper / SQLite truth | Never filled from MCP, CLI, or Agent SKILL |
| Doctrine RAG | No CoinGecko Learn, no AI Integration pages in Qdrant |
| Secrets | No API keys in docs or UI; `COINGECKO_API_KEY` / `COINGECKO_DEMO_API_KEY` / `COINGECKO_PRO_API_KEY` in `.env` for controlled ingest (and Demo/Pro keys for MCP only if enabled later) |
| VPS MCP | Optional future OpenClaw research aid — does not replace controlled ingest |

## Related

- [Market-safety ingest design](../superpowers/specs/2026-10-01-market-safety-ingest-design.md)
- [`data/analytics/api-map.json`](../../data/analytics/api-map.json)
- [`data/analytics/stack.md`](../../data/analytics/stack.md) — doctrine RAG exclusions
- [Binance for Jarvise](../exchange/binance-for-jarvise.md) — parallel technical map for the exchange adapter
