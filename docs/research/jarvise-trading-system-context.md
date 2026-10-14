# Jarvise Automated Crypto & Stock Trading System Context

**Research overlay — not a build list.** Do not index this page into Qdrant `jarvise_doctrine`. Do not silent-merge it into SQLite or the paper ledger.

Sections 2 and 3 restate doctrine the Python stack already runs. Use these modules, and do not rebuild them:

- Signal: [`src/jarvise_analyze/engine.py`](../../src/jarvise_analyze/engine.py) and [`src/jarvise_paper/auto_decide.py`](../../src/jarvise_paper/auto_decide.py). FLAT below 0.55. Doctrine-free auto-approve stays at 0.70.
- Risk: [`src/jarvise_risk/caps.py`](../../src/jarvise_risk/caps.py) and [`src/jarvise_paper/stops.py`](../../src/jarvise_paper/stops.py). Risk at the stop stays 0.25–1% with a hard 1% ceiling. Stops stay at least 1.5× ATR.
- Execution: [`src/jarvise_paper`](../../src/jarvise_paper) for paper fills and [`src/jarvise_trade`](../../src/jarvise_trade) for the gated live path. Live stays off until the owner checklist.

Section 4 is not adopted. Jarvise is Python, FastAPI, and SQLite. It does not add Prisma, Next.js Server Actions, a TradingView webhook as an order source, a required 1:3 take-profit, risk above 1%, new execution venues, or a second dashboard. Fear & Greed stays context and does not boost confidence. `JARVISE_ANALYZE_MTF` stays off until about 10 closed `auto_*` trades.

## 1. Role & Architecture Mandates

- **Pre-Implementation Blueprint:** Before writing or modifying any files, you MUST outline the technical implementation plan (Schema changes, new/modified files, Server Action/Service signatures, and UI component hierarchy) so the user can review the logic first.
- **Strict Layered Architecture:** Enforce `Controllers -> Services -> Repositories`. Never call Prisma ORM directly from Server Components, Controllers, or API Route Handlers; all database operations must pass through the Repository layer with standardized error handling (`handlePrismaError`).
- **Validation & Security:** Enforce Zod schemas on all incoming API/Webhook payloads and Server Action inputs, alongside Role-Based Access Control (RBAC).
- **UI Synchronization:** Every Server Action that mutates state (e.g., toggling the Kill-Switch, approving an order, or unlocking a Drawdown Halt) MUST invoke `revalidatePath` and `router.refresh()`.
- **Skill-Based Modular Architecture:** Structure the trading core into three distinct, modular domains[cite: 22]:
  1. `[Signal Skill]` — Parses market structure, technical indicators, and sentiment feeds to detect entry/exit conditions[cite: 22, 51, 53].
  2. `[Risk Skill]` — Enforces position sizing, stop-loss validation, correlation checks, slippage monitoring, and daily loss limits[cite: 22, 41, 44].
  3. `[Execution Skill]` — Connects to regulated brokerage and exchange REST APIs (e.g., Binance TH, Binance, OKX, Bybit, Interactive Brokers, Alpaca) and dispatches real-time alerts via Telegram, Discord, or WhatsApp[cite: 22, 41].

---

## 2. Core Operating Doctrine & Financial Rules

### 2.1 Capital Preservation & Expectancy Gate

- **Capital Preservation Beats Activity:** Never size a position such that a single loss can damage the portfolio[cite: 41]. Prefer holding no position (`FLAT`) over taking a low-confidence trade[cite: 41].
- **Positive Expectancy (EV) Requirement:** A strategy may only transition from Paper Trading to live execution if its historical paper trading Expected Value (EV) is clearly positive after accounting for exchange fees, bid-ask spread, and order book depth[cite: 41]:
  - `EV = (Win_Rate * Avg_Win) - (Loss_Rate * Avg_Loss) - Total_Costs`[cite: 41]
  - `Expected Return = (Probability_of_Gain * Take_Profit_Pct_Gain) + (Probability_of_Loss * Stop_Loss_Pct_Loss)`[cite: 4]

### 2.2 Position Sizing & Volatility-Adjusted Stop-Loss

- **Fixed Fractional Risk (0.25% to 1% Rule):** Risk only a small fixed fraction of total account equity per trade idea, starting at 0.25% to 1% (and never exceeding the 1% to 2% maximum threshold)[cite: 4, 22, 41]. Treat the Kelly Criterion strictly as an upper bound (use a fractional Kelly) and reduce position size in unclear market regimes[cite: 41, 45].
- **Explicit Invalidation & ATR Stop Distance:** Every trade setup must define an explicit price or structure invalidation level (`Stop-Loss`) and a `Take-Profit` target before execution to enforce a favorable Risk/Reward Ratio (benchmark 1:3)[cite: 39, 41, 44].
- **ATR Volatility Buffer:** Calculate stop distance using the Average True Range (ATR), requiring a minimum distance of `>= 1.5 * ATR` to avoid premature stop-outs from normal market noise[cite: 51]. When the invalidation level is hit, exit immediately to `FLAT` with zero revenge additions[cite: 41].
- **Position Size Formula:**
  - `Account_Risk_Amount = Total_Equity * Risk_Per_Trade_Pct`[cite: 47]
  - `Trade_Risk_Per_Unit = abs(Entry_Price - Stop_Loss_Price)`[cite: 44, 47]
  - `Position_Size_Units = Account_Risk_Amount / Trade_Risk_Per_Unit`[cite: 47]

### 2.3 Portfolio Protection & Deployment Ladder

- **Daily Drawdown Halt:** If cumulative daily losses reach the predefined daily loss limit, halt all automated and manual trade executions for the remainder of the session[cite: 22, 41, 44]. Trading may only resume after submitting a written manual review[cite: 41, 44].
- **Correlation & Slippage Guards:** Block simultaneous stacked positions in highly correlated assets (e.g., simultaneous unhedged long positions in BTC and ETH) and pause execution if fill slippage spikes above threshold[cite: 22, 52].
- **Deployment Ladder:** Enforce a strict 3-stage progression: `PAPER -> MANUAL_APPROVAL -> AUTONOMY`[cite: 41]. Live autonomy can only be unlocked when the Kill-Switch, Spend Cap, and Confidence Threshold are active[cite: 41].

---

## 3. Data Feeds & Technical Analyzer Stack

- **Market & On-Chain Data Sources:**
  - **TradingView:** Price charts, OHLC candles, order books, screeners, Paper Trading simulation, and Bar Replay backtesting[cite: 11, 13, 15, 51].
  - **CoinMarketCap (CMC):** Global market cap, 24h volume, Fear & Greed Index (0-100 scale), Altcoin Season Index, and low-latency point-in-time historical APIs free of survivorship bias[cite: 11, 12, 53].
  - **CoinGlass & On-Chain Metrics:** Derivatives Open Interest (OI), Funding Rates, 24h Liquidations, Exchange Inflow/Outflow, and Exchange Reserves[cite: 11, 53].
- **Multi-Layer Technical Confirmation (Structure First):**
  1. **Market Structure:** Identify trend vs. range, swing highs/lows, support/resistance zones, and breakout/retest structures first[cite: 27, 41, 51]. Forecast 2 to 3 scenarios with clear invalidation levels rather than a single price point[cite: 41].
  2. **Candle Language:** Use OHLC and Heikin-Ashi to filter trend noise; treat patterns like Bullish/Bearish Engulfing, Shooting Star, Evening Star, and Doji strictly as contextual confirmation[cite: 8, 51].
  3. **Trend & Volatility:** Combine EMA/SMA stacks, ADX, and Ichimoku Cloud for trend strength with ATR, Bollinger Bands, Keltner Channels, and Donchian Channels for volatility squeeze vs. expansion[cite: 3, 5, 7, 9, 23, 33, 51].
  4. **Momentum & Volume:** Use RSI, MACD, Stochastic, and CCI to spot divergences alongside OBV, Chaikin Money Flow (CMF), and session VWAP to verify institutional volume participation[cite: 18, 24, 31, 34, 37, 51, 53].

---

## 4. Technical Implementation Plan Specification

### 4.1 Database Schema (Prisma ORM)

Ensure proper indexing (`@@index`) and status enums (`enum`) across all models:

- `enum DeploymentMode`: `PAPER`, `MANUAL_APPROVAL`, `AUTONOMY`[cite: 41]
- `enum OrderSide`: `LONG`, `SHORT`, `FLAT`[cite: 2, 41]
- `enum OrderType`: `LIMIT`, `MARKET`, `STOP_LIMIT`[cite: 2, 4]
- `enum OrderStatus`: `PENDING_APPROVAL`, `OPEN`, `FILLED`, `CANCELLED`, `REJECTED_BY_RISK`, `CLOSED`
- `model RiskConfig`: `id`, `riskPerTradePct` (default `0.005`), `maxDailyDrawdownPct` (default `0.02`), `minRiskRewardRatio` (default `3.0`), `minAtrMultiplier` (default `1.5`), `maxSlippagePct`, `spendCap`, `confidenceThreshold`, `isKillSwitchActive`, `deploymentMode`, `updatedAt`[cite: 22, 39, 41, 44, 51]
- `model TradeSignal`: `id`, `symbol`, `venue`, `timeframe`, `side`, `entryPrice`, `stopLossPrice`, `takeProfitPrice`, `atrValue`, `confidenceScore`, `scenariosJson`, `indicatorsSnapshotJson`, `status`, `createdAt` (with `@@index([symbol, status, createdAt])`)[cite: 41, 44, 51]
- `model TradeOrder`: `id`, `signalId`, `symbol`, `orderType`, `side`, `positionSizeUnits`, `accountRiskAmount`, `tradeRiskPerUnit`, `expectedValue`, `entryPrice`, `fillPrice`, `slippagePct`, `feesPaid`, `stopLossPrice`, `takeProfitPrice`, `realizedPnl`, `status`, `createdAt` (with `@@index([status, createdAt])`)[cite: 22, 41, 44, 47]
- `model DailyDrawdownLog`: `id`, `tradingDate`, `startingEquity`, `currentEquity`, `cumulativeLossPct`, `isHalted`, `manualReviewNote`, `reviewedBy`, `unlockedAt` (with `@@index([tradingDate, isHalted])`)[cite: 41, 44]

### 4.2 File Structure & Responsibilities

- `src/schemas/trading.schema.ts` — Zod validation schemas for webhook signals, risk configuration updates, and manual review forms.
- `src/repositories/risk-config.repository.ts` — Prisma queries for `RiskConfig` wrapped in `handlePrismaError`.
- `src/repositories/trade-order.repository.ts` — Prisma mutations and queries for `TradeSignal` and `TradeOrder` wrapped in `handlePrismaError`.
- `src/repositories/drawdown-log.repository.ts` — Prisma tracking for daily equity and drawdown halts wrapped in `handlePrismaError`.
- `src/services/signal-analyzer.service.ts` — `[Signal Skill]` Evaluates structure, ATR stop distance (`>= 1.5x ATR`), R:R ratio (`>= 1:3`), and multi-indicator confluence[cite: 22, 39, 51].
- `src/services/risk-guard.service.ts` — `[Risk Skill]` Computes position size (`Account Risk / Trade Risk`), checks EV, verifies correlation, and enforces Daily Drawdown Halt and Kill-Switch gates[cite: 22, 41, 44, 47].
- `src/services/execution-engine.service.ts` — `[Execution Skill]` Dispatches orders to exchange APIs (Binance TH / Alpaca / IBKR), monitors fill slippage, and sends Telegram/Discord notifications[cite: 22, 41].
- `src/controllers/trading-actions.controller.ts` — Next.js Server Actions for UI interactions with RBAC, Zod validation, and `revalidatePath` + `router.refresh()`.
- `src/app/api/webhooks/tradingview/route.ts` — Low-latency webhook ingestion endpoint routing signals to the Controller/Service layer[cite: 12, 22].

### 4.3 Core Service & Server Action Signatures

- `calculatePositionSize(input: PositionCalcInput): Promise<PositionCalcResult>`
- `evaluateRiskGates(signal: TradeSignalInput, equity: number): Promise<RiskEvaluationResult>`
- `processIncomingWebhookSignal(payload: unknown): Promise<ActionResponse<TradeOrder>>`
- `approveManualTradeOrder(orderId: string): Promise<ActionResponse<TradeOrder>>`
- `toggleSystemKillSwitch(isActive: boolean, reason: string): Promise<ActionResponse<RiskConfig>>`
- `submitDrawdownWrittenReview(logId: string, reviewNote: string): Promise<ActionResponse<DailyDrawdownLog>>`

### 4.4 UI Component Hierarchy (`src/components/trading/`)

- `TradingCommandDashboard` (Main container using `Alfa Slab One` for headings and `Raleway` for body text)
  - `DeploymentAndKillSwitchHeader` (Displays `PAPER` / `MANUAL_APPROVAL` / `AUTONOMY` status, Spend Cap progress, and emergency Kill-Switch button)[cite: 41]
  - `MarketRegimeAndSentimentStrip` (Displays CMC Fear & Greed Index, Altcoin Season Index, and CoinGlass Funding/Liquidation metrics)[cite: 11, 53]
  - `PositionSizingInteractiveCard` (Live calculator for 0.25%–1% equity risk, entry, `1.5x ATR` stop-loss, and 1:3 R:R target)[cite: 39, 41, 47, 51]
  - `PendingApprovalQueueTable` (Lists queued signals with 2–3 forecast scenarios, EV score, and `useOptimistic` Approve/Reject actions)[cite: 41]
  - `DailyDrawdownReviewModal` (Enforces written post-mortem submission before unlocking a halted trading session)[cite: 41, 44]

---

## 5. Testing Strategy (REQUIRED)

1. **Service / Server Action Unit & Integration Tests (Vitest / Jest):**
   - **Position Sizing Math:** Verify exact share/token calculation (`$500` account risk / `$20` trade risk = `25` units) and fee/spread deduction[cite: 41, 47].
   - **ATR & R:R Validation:** Assert that stops tighter than `1.5 * ATR` or setups below the minimum Risk/Reward ratio are rejected[cite: 39, 51].
   - **Expectancy & Risk Gates:** Verify that negative `EV`, active `isKillSwitchActive`, breached `maxDailyDrawdownPct`, or correlated duplicate positions strictly block order execution[cite: 22, 41, 44].
2. **End-to-End (E2E) Tests (Playwright - Critical User Journey):**
   - **Status:** REQUIRED — Order execution, risk halting, and emergency kill-switch controls represent a Critical User Journey.
   - **E2E Flow 1 (Webhook to Manual Approval):** Simulate a valid TradingView webhook POST -> verify signal appears in `PendingApprovalQueueTable` -> click Approve -> verify order status transitions to `OPEN` and UI revalidates.
   - **E2E Flow 2 (Daily Drawdown Halt & Written Review Unlock):** Simulate a losing trade that breaches `maxDailyDrawdownPct` -> verify system enters `HALTED` state and disables order submission -> submit written review in `DailyDrawdownReviewModal` -> verify session unlocks[cite: 41, 44].
   - **E2E Flow 3 (Emergency Kill-Switch):** Trigger `DeploymentAndKillSwitchHeader` kill-switch -> verify all pending orders cancel and incoming webhooks are rejected immediately[cite: 41].