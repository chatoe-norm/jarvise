# Binance APIs for Jarvise

**Paper-only / no order placement.** This page maps the official [Binance Developer Documentation introduction](https://developers.binance.com/en/docs/introduction) into what Jarvise may and may not do. It does **not** rebrand Binance as Jarvise.

Local digest: [`data/analytics/sources/binance-api-intro-jarvise.txt`](../../data/analytics/sources/binance-api-intro-jarvise.txt)  
Skills Hub digest: [`data/analytics/sources/binance-skills-hub-jarvise.txt`](../../data/analytics/sources/binance-skills-hub-jarvise.txt)  
Machine index (reference): [llms.txt](https://developers.binance.com/en/docs/llms.txt)  
Optional community Q&A (not SoT): [`binance-vision-qa-reference.md`](binance-vision-qa-reference.md) — [dev.binance.vision](https://dev.binance.vision/)

## Source of truth

| Layer | Where |
|-------|--------|
| Official concepts | [Binance intro](https://developers.binance.com/en/docs/introduction) |
| Spot REST reference | [Spot REST API](https://developers.binance.com/en/docs/products/spot/rest-api) |
| Jarvise filter | This doc + `api-map.json` Binance Spot provider |
| Doctrine / venue | Binance TH spot preferred for live; paper OHLC uses `https://api.binance.com` |
| Optional debugging aid | [`binance-vision-qa-reference.md`](binance-vision-qa-reference.md) (community; not SoT) |
| Agent skills (read-only subset) | [Skills Hub](https://developers.binance.com/en/docs/sdks-tools/integrations/skills-hub) → Jarvise skill `jarvise-binance-intel` |

## What Jarvise uses

| Capability (from Binance intro) | Jarvise |
|---------------------------------|---------|
| REST market data | **Allowed** — `GET /api/v3/klines` via `jarvise_ingest` |
| Web3 public token / rank / audit / tokenized-stock / Academy reads | **Allowed for agent research** — via `jarvise-binance-intel` (no keys); see [Skills Hub](#skills-hub) |
| Account balances | **Planned (P3)** — signed `GET /api/v3/account` only; see [P3 design](../superpowers/specs/2026-09-23-p3-exchange-readonly-design.md) |
| Place / manage / cancel orders | **Forbidden** |
| WebSocket / FIX / SBE trading | **Out of scope** |
| Withdraw / transfer | **Forbidden** |

## Auth and security

- Public klines: no API key.
- Planned account read: HMAC-SHA256 + `X-MBX-APIKEY` from `BINANCE_API_KEY` / `BINANCE_API_SECRET`.
- Never create or use keys with **withdrawal** or **transfer** permissions.
- Never log secrets; never put keys in HTML/JSON UI responses.
- Soft-fail on `/analytics`: hide the exchange panel if keys are missing or the call fails (P3).

## Environments

Binance documents Production, Testnet, and product-specific Demo environments. Jarvise paper analytics uses **live public market data** for OHLC. Venue testnets are optional and not required. Do not treat testnet as a substitute for paper-first owner guardrails.

## Rate limits

Respect Binance request weights and backoff. Avoid tight polling loops. One balance sync per `/analytics` page load is enough when P3 ships.

## Agent Native (reference only)

Binance offers `llms.txt`, Agent REST, and an MCP server for agent tooling. The [Binance MCP Server](https://developers.binance.com/en/docs/agent-native/mcp-server/agentic.md) reads balances and **trades Spot / Margin / Convert / Futures inside a dedicated Agentic sub-account** — it is execution tooling and is **forbidden** in Jarvise.

The docs site renders client-side, so plain HTML fetches return an empty shell. For doctrine RAG, fetch `llms.txt` or the `.md` variant of a page (for example `/en/docs/introduction.md`); [`config/rag-sources.json`](../../config/rag-sources.json) uses those.

## Skills Hub

[Binance Skills Hub](https://developers.binance.com/en/docs/sdks-tools/integrations/skills-hub) ([repo](https://github.com/binance/binance-skills-hub)) is a marketplace of agent skills that mixes read-only research with trading and wallet execution. Jarvise **does not install it** (`npx skills add` pulls every skill, including write ones). Instead, the Jarvise-authored skill `jarvise-binance-intel` ([`plugins/jarvise-openclaw/skills/jarvise-binance-intel/SKILL.md`](../../plugins/jarvise-openclaw/skills/jarvise-binance-intel/SKILL.md)) calls a small allowlist of public, unauthenticated endpoints. Full endpoint list: [`binance-skills-hub-jarvise.txt`](../../data/analytics/sources/binance-skills-hub-jarvise.txt).

| Decision | Hub skills | Jarvise use |
|----------|-----------|-------------|
| Adopt | `query-token-info` (search, meta, dynamic), `query-token-audit`, `crypto-market-rank` (`token-rank`, `social-hype`, `smart-money-inflow`), `binance-tokenized-securities-info`, `academy-skill` | Research context through `jarvise-binance-intel` |
| Exclude | `query-token-info` kline (third-party host `dquery.sintral.io`), `meme-rank`, `address-pnl-rank`, `trading-signal`, `binance-wallet-tracker`, `query-address-info`, `binance-leaderboard` | Not used — poor fit for slow, steady doctrine |
| Deny | `binance` (`binance-cli`), `binance-agentic-wallet`, `binance-trading-signal` (`baw`), `binance-onchain-copy-trader`, `meme-rush`, `fiat`, `p2p`, `payment`, `onchain-pay`, `square-post`, `sports-ai-analyzer`, Binance MCP Server | Never — these write, execute, move funds, or post publicly |

Doctrine rules for adopted data:

- Audit `HIGH`, any hit with `riskType: RISK` (honeypot etc.), or a non-zero sell tax is a veto: **FLAT**. `LOW` means "no known red flags", never "safe". `MID` (the live API's spelling of `MEDIUM`) is not a veto alone: `CAUTION` hits such as "Mintable" on major stablecoins only lower confidence.
- Rank, social hype, and smart-money inflow are crowding and sentiment context, never a standalone trigger (same rule as oscillators).
- Tokenized US stocks: 1 token = `multiplier` shares (`referencePrice = price / multiplier`); no thesis across an earnings, dividend, or split halt.
- Several adopted endpoints are unauthenticated query `POST`s. They are allowed for agent research only; `jarvise_ingest` stays GET-only.
- The hub `binance` skill reads `BINANCE_SECRET_KEY`; Jarvise keeps `BINANCE_API_SECRET` for the P3 read-only account path and never uses `binance-cli`.

## Hard rules

| Rule | Behavior |
|------|----------|
| Current phase | Paper analytics / research only |
| Orders | Never call order endpoints |
| Keys | Read (+ spot trade only if live is ever unlocked); never withdrawal/transfer |
| Soft fail | Missing keys → hide exchange UI; page still loads |
| Ingest | Stays public GET-only market data |
| Agent skills | `jarvise-binance-intel` read-only allowlist only; never install Skills Hub or use `binance-cli` / `baw` / Agentic Wallet |

## Related

- [P3 exchange read-only design](../superpowers/specs/2026-09-23-p3-exchange-readonly-design.md)
- [`config/rag-sources.json`](../../config/rag-sources.json) — allowlisted fetch URLs
- [`data/analytics/api-map.json`](../../data/analytics/api-map.json) — provider allowlist / denylist
- [`binance-vision-qa-reference.md`](binance-vision-qa-reference.md) — optional Vision community Q&A (not SoT)
- [`binance-skills-hub-jarvise.txt`](../../data/analytics/sources/binance-skills-hub-jarvise.txt) — Skills Hub adopt/exclude/deny matrix and endpoint allowlist
