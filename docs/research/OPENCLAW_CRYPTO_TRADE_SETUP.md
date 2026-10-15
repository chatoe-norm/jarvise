# OpenClaw Intelligence Automated Crypto & Futures Trading Setup Context

## 1. Objective & System Overview

Configure and deploy a production-grade, owner-protective automated cryptocurrency and perpetual futures trading system on **OpenClaw**. This specification instructs **Antigravity** to provision the required OpenClaw skills, configure Model Context Protocol (MCP) servers, enforce deterministic risk guardrails (Jarvise Core Protocol), and establish automated execution and monitoring pipelines[cite: 2, 4, 7].

---

## 2. Core Operating Doctrine (Non-Negotiable Rules)

Antigravity MUST enforce the following rules inside the OpenClaw Guardrail Layer prior to any execution skill invocation[cite: 4, 16]:

1. **Capital Preservation First:** Prefer sitting `FLAT` (inactive) over entering low-confidence setups[cite: 4, 9].
2. **Positive Expectancy Gatekeeper:** Strategies must prove a positive Expected Value ($EV > 0$) net of exchange fees and slippage during paper/backtest validation before unlocking live execution[cite: 2, 4, 9]:
   $$EV = (\text{Win}\% \times \text{AvgWin}) - (\text{Loss}\% \times \text{AvgLoss}) - \text{Costs}$$
3. **The 1% Position Sizing Rule:** Maximum risk per trade must stay strictly between `0.25%` and `1.00%` of total account equity[cite: 2, 4, 9]. Use fractional Kelly only as an upper ceiling[cite: 4, 9].
4. **Volatility-Adjusted Invalidation (Stop-Loss):** Every trade must define a hard Stop-Loss (S/L) and Take-Profit (T/P) before order placement (minimum R:R of `1:2` or `1:3`)[cite: 2, 7]. Stop-Loss distance must be $\ge 1.5\times \text{ATR(14)}$ to prevent premature stop-outs from market wicks[cite: 10, 13, 35]. When hit, transition immediately to `FLAT` — zero revenge trading or averaging down[cite: 2, 4, 9].
5. **Programmatic Drawdown Halt:** If cumulative daily loss reaches the configured daily drawdown threshold, immediately halt all trading for the UTC day, cancel open orders, and lock the system until a manual review resets the state[cite: 2, 4, 9, 16].
6. **Slippage & Microstructure Control:** Prioritize `LIMIT` orders over `MARKET` orders[cite: 2, 6, 12]. Account for asset-specific book depth (e.g., ETH has thinner order books, wider slippage, and more frequent spoof walls compared to BTC)[cite: 4, 33].
7. **Strict Deployment Ladder:** Enforce sequential progression: `PAPER_SIMULATED` $\rightarrow$ `MANUAL_APPROVAL_POLICY` $\rightarrow$ `AUTONOMOUS_LIVE`[cite: 4, 8, 9, 16].

---

## 3. OpenClaw Skills & MCP Installation Manifest

Antigravity must execute the following installation and provisioning steps on the OpenClaw instance:

### 3.1 Security, Context & Operations Skills (Install First)
```bash
# Secure API key & secret injection via local HTTPS tunnel (never type keys in chat)
openclaw skill install secretclaw

# Secret leak prevention before git commits
openclaw skill install ggshield-scanner

# Context compression (50%+ token reduction for 24/7 long-running sessions)
openclaw skill install clawpressor

# Cron job token consumption tracking and budget limits
openclaw skill install token-budget-monitor

# Session checkpointing and rollback recovery
openclaw skill install clawreverse-skill

# Cross-platform snapshot backup and restore
openclaw skill install myopenclaw-backup-restore
```

### 3.2 Market Intelligence, Technical Analysis & Risk Skills
```bash
# Real-time crypto prices, Fear & Greed Index, DeFi TVL, and macro indicators (Zero API key)
openclaw skill install crypto-market-intel

# PineScript CLI executor for custom technical indicators (VWAP, EMA, ATR, RSI, MACD)
openclaw skill install pinets

# Precursor risk intelligence (15-min Flare alarms & 60-min Core synthesis)
openclaw skill install blackswan

# News & social sentiment analysis for vibe and crowd checks
openclaw skill install market-sentiment-pulse

# Quantitative backtesting (80+ indicators via Signal DSL) and multi-exchange screening
openclaw skill install heytraders
```

### 3.3 Automated Trading & Exchange Execution Skills
Install only the execution skills matching the target trading venue (keep unused write-capable skills disabled)[cite: 1, 3, 13]:

```bash
# Core 8-strategy automated trading engine (Grid, DCA, Trend Following, Scalping, Swing, Arbitrage)
openclaw skill install crypto-trader

# Perpetual Futures & Spot Execution (Select active venue)
openclaw skill install hyperliquid-trading
openclaw skill install phemex-trade
openclaw skill install tentactl
```

> **Security Policy Note on External Skill Hubs:** Do NOT run bulk unverified installations (e.g., `npx skills add https://github.com/binance/binance-skills-hub`)[cite: 13]. Block all autonomous fund-transfer, on-chain copy-trading, and public social-posting skills[cite: 13].

---

## 4. MCP Server & Data Pipeline Configuration

Antigravity must configure the following MCP servers and data ingestion pipelines in `config/openclaw.json`:

1. **TradingView MCP (`tradingview-mcp`):**
   - **Role:** Primary real-time price feed, multi-timeframe chart snapshot generator, Yahoo Finance/Reddit news aggregator, and 6-model strategy backtester[cite: 7, 14, 24, 27].
   - **Auth:** None required (Open-source zero-key protocol)[cite: 14, 24].
2. **CoinMarketCap MCP (`cmc-mcp`):**
   - **Role:** Market cap dominance, Fear & Greed Index, Altcoin Season Index, and volume baseline[cite: 1, 6, 8].
3. **Derivatives & Microstructure Ingest (`coinglass-ingest` + `sqlite-price-graph`):**
   - **Role:** Poll Funding Rates, Open Interest (OI), 24h Liquidations, and L2 Order Book depth to detect spoofing walls before trade execution[cite: 4, 6, 8, 13, 32].

---

## 5. System Architecture & Guardrail Configuration

Antigravity must create `config/guardrails.policy.json` with the following structure to govern the `[Signal Skill] -> [Risk Skill] -> [Execution Skill]` pipeline[cite: 5, 16]:

```json
{
  "deployment_mode": "PAPER_SIMULATED",
  "allowed_assets": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
  "venue_config": {
    "primary_spot": "BINANCE_TH_OR_COINBASE_ADVANCED",
    "primary_futures": "HYPERLIQUID_OR_PHEMEX",
    "default_order_type": "LIMIT",
    "max_leverage": 3
  },
  "risk_parameters": {
    "max_equity_risk_percent_per_trade": 1.0,
    "min_equity_risk_percent_per_trade": 0.25,
    "min_risk_reward_ratio": 2.0,
    "atr_period": 14,
    "min_atr_stop_multiplier": 1.5,
    "per_trade_usd_cap": 100.0,
    "daily_drawdown_halt_percent": 2.5,
    "daily_spend_usd_cap": 300.0
  },
  "signal_gates": {
    "min_confidence_score": 0.65,
    "require_multi_timeframe_alignment": ["1D", "4H", "1H", "15m"],
    "require_volume_confirmation": true,
    "pullback_rule": {
      "trend": "UP",
      "ema_period": 20,
      "rsi_band": [45, 70]
    }
  },
  "kill_switch": {
    "auto_flat_on_invalidation": true,
    "lock_on_daily_drawdown": true,
    "require_manual_written_review_to_unlock": true
  }
}
```

---

## 6. Step-by-Step Execution Instructions for Antigravity

When triggered, Antigravity must execute the following workflow sequentially:

### Step 1: Initialize Directory & Database Schema
- Create `data/jarvise_state.sqlite` with tables: `market_klines`, `signal_evaluations`, `orders_ledger`, and `guardrail_audit_log`[cite: 13, 16].
- Create `scripts/jarvise_volatility_stop_loss.py` implementing the $1.5\times\text{–}3.0\times \text{ATR(14)}$ Stop-Loss and Chandelier Trailing Exit calculator[cite: 35].

### Step 2: Configure API Keys with Least Privilege
- Prompt the user via `secretclaw` to store exchange credentials in `secrets.json` (excluded via `.gitignore` and verified by `ggshield-scanner`)[cite: 1, 16].
- Verify that exchange API keys have **View** and **Trade** permissions ONLY[cite: 16]. **Transfer / Withdraw** permissions MUST be disabled[cite: 16].

### Step 3: Wire the 3-Stage Skill Pipeline
1. **Stage 1 — Signal Generation (`jarvise-signal-analyzer`):**
   - Query `tradingview-mcp`, `pinets`, and derivatives feeds across `1D`, `4H`, `1H`, and `15m` timeframes[cite: 1, 14, 16].
   - Evaluate market structure first (Trend vs. Range, Swing High/Low, Support/Resistance zones, VWAP fair value, and Order Book spoof check)[cite: 2, 4, 9, 10].
   - Output a structured JSON payload containing `direction`, `confidence_score`, `entry_price`, `invalidation_stop_price`, `target_price`, and `thesis_summary`[cite: 2, 4, 16].
2. **Stage 2 — Guardrail Gatekeeper (`jarvise-risk-guard`):**
   - Run the 4 mandatory checks: (1) Allowed Asset List, (2) Per-Trade Cap & 1% Equity Sizing, (3) Daily Cumulative Drawdown / Spend Cap, and (4) Minimum Confidence Threshold ($\ge 0.65$)[cite: 2, 11, 16].
   - Verify Stop-Loss distance is $\ge 1.5\times \text{ATR}$[cite: 10, 35].
   - If any check fails, set status to `HELD` with an explicit reason (e.g., `"Held: Stop distance below 1.5x ATR noise floor"`) and abort execution[cite: 16, 35].
3. **Stage 3 — Order Execution (`jarvise-order-router`):**
   - In `PAPER_SIMULATED` mode: simulate order fills against live bid/ask book depth and record P&L in `orders_ledger`[cite: 4, 16].
   - In `MANUAL_APPROVAL_POLICY` mode: send an interactive approval request to Telegram/Discord with the chart snapshot and wait for user sign-off[cite: 4, 14, 16].
   - In `AUTONOMOUS_LIVE` mode: place `LIMIT` bracket orders (Entry + S/L + T/P) via the configured exchange skill[cite: 2, 16, 30].

### Step 4: Configure Scheduled Cron Jobs & Alerts
- Configure OpenClaw Cron Jobs:
  - `0 9 * * *` (09:00 AM Daily): Generate Morning Market Briefing (Macro, Fear & Greed, Funding Rates, Key S/R Zones, and TradingView Chart Snapshots) and push to Telegram/Discord[cite: 6, 14].
  - `*/15 * * * *` (Every 15 Minutes): Run the Market Structure Scanner & Guardrail Check loop[cite: 1, 16].
  - `59 23 * * *` (23:59 UTC Daily): Calculate Daily Realized/Unrealized P&L, Win%, Expectancy (EV), and Token Usage Report (`token-budget-monitor`)[cite: 1, 4, 5].

---

## 7. Verification & Acceptance Criteria

Antigravity must verify the setup by running the following checks before declaring completion:
1. Run `openclaw skill list` and confirm all required skills are active[cite: 3].
2. Execute `python3 scripts/jarvise_volatility_stop_loss.py --test` and confirm accurate $1.5\times \text{ATR}$ stop calculations[cite: 35].
3. Run a simulated signal cycle in `PAPER_SIMULATED` mode and confirm that:
   - Low-confidence signals ($< 0.65$) are rejected into a `FLAT` state[cite: 4, 11, 16].
   - Valid signals compute a position size risking $\le 1\%$ of simulated equity[cite: 2, 4].
   - Simulated Drawdown Halt locks the execution router when daily loss reaches threshold[cite: 2, 4].
   - Telegram/Discord receives the formatted trade log and chart snapshot[cite: 14, 16].