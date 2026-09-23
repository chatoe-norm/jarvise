---
name: jarvise-binance-intel
description: Read-only Binance public intel for Jarvise paper research — token search/meta/dynamic, token security audit, market rank and social hype, smart-money inflow, Ondo tokenized US stocks, Binance Academy risk education. Use when researching a token or tokenized stock, checking whether a token is a scam/honeypot, looking at trending or hype context, or when the user mentions Binance Skills Hub, Binance Web3, or tokenized stocks. No API keys; never places orders.
---

# Jarvise Binance intel (Cursor)

The canonical skill is [`plugins/jarvise-openclaw/skills/jarvise-binance-intel/SKILL.md`](../../../plugins/jarvise-openclaw/skills/jarvise-binance-intel/SKILL.md). **Read it in full and follow it**: endpoint allowlist, request conventions, doctrine mapping, and the "Never" list. This file only adds Cursor-specific notes, so the two copies cannot drift.

Adopt/exclude/deny matrix: [`data/analytics/sources/binance-skills-hub-jarvise.txt`](../../../data/analytics/sources/binance-skills-hub-jarvise.txt). Exchange map: [`docs/exchange/binance-for-jarvise.md`](../../../docs/exchange/binance-for-jarvise.md).

## Cursor workflow

1. Run the `jarvise-notebook` skill first for doctrine (risk rules, venue constraints, FLAT threshold).
2. Call the allowlisted endpoints with the Shell tool using `curl -s` and the headers from the canonical skill. Pipe through `python3 -m json.tool` or a short `python3 -c` filter; keep output small.
3. For CEX spot pairs, read candles from the local SQLite price-graph (`jarvise ingest`), not from these endpoints.
4. Combine: notebook doctrine + price-graph structure + this intel. State which rule applied and default to **FLAT** below the confidence threshold.

## Cursor-specific never

- Do not install Binance Skills Hub (`npx skills add`), `binance-cli`, or `baw`, and do not add the Binance MCP Server to Cursor MCP settings.
- Do not read `.env` or run `printenv` / `env` for this skill; it needs no credentials.
- Paper research only: no order placement, no swaps, no transfers.
