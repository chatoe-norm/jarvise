---
name: crypto-market-intel
description: Crypto Market Intelligence — Real-time Fear & Greed Index, DeFi TVL, BTC dominance, and macro market context.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"] },
      },
  }
---

# Crypto Market Intel

Provides macro market context to OpenClaw research agents and the Jarvise Command Dashboard.

## Data Sources
- **Alternative.me Fear & Greed Index:** Context sentiment metric (0–100).
- **CoinGecko Global:** Global crypto market cap, 24h volume, BTC/ETH dominance percentage.
- **Microstructure Feeds:** Public Binance order book depth and futures funding rates / open interest.

> **Doctrine Rule:** Market intel is context only. It never serves as a lone directional trigger and never overrides risk caps or analyzer confidence floors.
