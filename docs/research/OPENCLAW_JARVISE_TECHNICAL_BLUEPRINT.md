# OpenClaw & Jarvise Technical Blueprint: Intelligence & Guardrail Architecture

## 1. Executive Summary & Objective

This technical blueprint defines the integration architecture between **OpenClaw** and **Jarvise** based on `docs/research/OPENCLAW_CRYPTO_TRADE_SETUP.md`. 

Per the system directive, **Jarvise is the core Order Management System (OMS) and single source of truth**. Wherever specifications in the setup context propose standalone or unverified execution patterns, **Jarvise core features take precedence**.

OpenClaw functions as an intelligent research, multi-timeframe analysis, and signal-generation overlay operating strictly under **Jarvise Fail-Closed Risk Guardrails** in `PAPER_SIMULATED` mode.

---

## 2. Architecture & Conflict Resolution (Jarvise as Single Source of Truth)

| Architecture Component | OpenClaw Context Spec | Jarvise Core Architecture (Main SoT) | Conflict Resolution & Harmonization |
| :--- | :--- | :--- | :--- |
| **State & Database** | `data/jarvise_state.sqlite` | `data/analytics/jarvise.db` (SQLite MVAS) | **Jarvise DB is Main**: All market technicals, approval queues, paper orders, positions, and risk metrics reside in `data/analytics/jarvise.db`. |
| **Execution & Venues** | Direct exchange skills (`hyperliquid-trading`, `phemex-trade`) | Jarvise OMS (`jarvise_paper` / `jarvise_execution`) | **Jarvise OMS is Main**: Direct autonomous exchange write skills are disabled. All trades flow through `jarvise paper` (simulated book fill) or manual approval. |
| **Risk Guardrails** | OpenClaw Guardrail Layer | `src/jarvise_risk/caps.py` + `config/guardrails.policy.json` | **Unified Guardrail**: Declarative policy in `config/guardrails.policy.json` feeds directly into Jarvise's deterministic risk engine. |
| **Stop-Loss / Profit Lock** | $1.5\times\text{--}3.0\times\text{ATR(14)}$ Stop-Loss | $1.5\times\text{ATR}$ entry stop + Two-stage lock (`profit_lock.py`) | **Harmonized**: $1.5\times\text{ATR}$ initial invalidation stop, tightening to Breakeven (+20 bps) at $0.5R$ and Trailing $1.0R$ at $\ge 1.0R$. |
| **Market Data Ingestion** | TV MCP + CMC MCP | Jarvise Ingest (`closed_candles` + Binance book) | **Reproducible Ingest**: TV and CMC MCP servers act as research/snapshot overlays; numeric truth is written by Jarvise ingest. |

```mermaid
flowchart TD
    subgraph Research & Signal Layer (OpenClaw)
        A1[TradingView MCP] --> S1[Stage 1: jarvise-signal-analyzer]
        A2[Crypto Market Intel / CMC] --> S1
        A3[Jarvise Doctrine RAG] --> S1
        A4[BlackSwan Precursor Risk] --> S1
        S1 -->|Signal Payload: Dir, Conf, Entry, Stop, Thesis| S2[Stage 2: jarvise-risk-guard]
    end

    subgraph Deterministic Guardrail Layer (Jarvise Risk Engine)
        S2 --> C1{Asset in Whitelist?}
        C1 -->|No| R1[REJECT: Asset Not Allowed]
        C1 -->|Yes| C2{Confidence >= 0.65?}
        C2 -->|No| R2[FLAT: Low Confidence]
        C2 -->|Yes| C3{Stop Distance >= 1.5x ATR?}
        C3 -->|No| R3[HELD: Stop in Noise Floor]
        C3 -->|Yes| C4{Risk at Stop <= 1.0% Equity?}
        C4 -->|No| R4[HELD: Exceeds 1% Risk Ceiling]
        C4 -->|Yes| C5{Daily Drawdown < 2.5% & Daily Loss < $300?}
        C5 -->|No| R5[KILL-SWITCH HALT: Drawdown Lock]
        C5 -->|Yes| S3[Stage 3: jarvise-order-router]
    end

    subgraph Core OMS & Persistence Layer (Jarvise)
        S3 -->|PAPER_SIMULATED| P1[Simulated Book Fill]
        P1 --> P2[data/analytics/jarvise.db: paper_orders]
        P1 --> P3[data/analytics/jarvise.db: paper_positions]
        P3 --> PL[Two-Stage Profit Lock Engine]
        PL -->|Favorable Excursion >= 0.5R| PL1[Breakeven +20 bps]
        PL -->|Favorable Excursion >= 1.0R| PL2[Trail 1.0R from Peak]
        S3 -->|MANUAL_APPROVAL_POLICY| Q1[Home Dashboard / Telegram Approval Queue]
        S3 -->|AUTONOMOUS_LIVE| G1[STRICTLY GATED OFF: Requires P4-C Checklist]
    end
```

---

## 3. The 3-Stage Pipeline Specification

### Stage 1: Signal Generation (`jarvise-signal-analyzer`)
- **Inputs**: Multi-timeframe closed candles (`1D`, `4H`, `1H`, `15m`), VWAP fair value, order book spoof detector, and Alternative.me Fear & Greed.
- **Rules**:
  - Structure first (Trend vs. Range, Key S/R zones).
  - HTF confirmation: 1D trend must agree with LTF direction.
  - Pullback rules: Trend `UP`, EMA20 pullback, RSI within $[45, 70]$.
  - Output format:
    ```json
    {
      "symbol": "BTCUSDT",
      "direction": "long",
      "confidence_score": 0.75,
      "entry_price": 68000.0,
      "invalidation_stop_price": 66200.0,
      "target_price": 71600.0,
      "thesis_summary": "4H EMA20 pullback confirmed with 1D trend_up alignment, order book bid wall verified."
    }
    ```

### Stage 2: Risk Guardrail Gatekeeper (`jarvise-risk-guard`)
- **Inputs**: Signal JSON, current account equity, daily realized P&L, MTM drawdown.
- **Mandatory Non-Negotiable Checks**:
  1. **Whitelist**: Asset must be in `["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]`.
  2. **1% Position Sizing Rule**: Max equity risk at stop $\le 1.00\%$:
     $$\text{Risk at Stop} = \text{Size \%} \times \frac{|\text{Entry} - \text{Stop}|}{\text{Entry}} \le 1.0\%$$
  3. **Volatility Invalidation Floor**: Stop-loss distance must be $\ge 1.5\times\text{ATR(14)}$.
  4. **Daily Drawdown Halt**: If daily loss exceeds $\$100$ or account drawdown $\ge 2.5\%$, trigger `HALT` and engage fail-closed kill-switch.
  5. **Confidence Filter**: Minimum confidence $\ge 0.65$; otherwise force `FLAT`.

### Stage 3: Order Execution Router (`jarvise-order-router`)
- **Deployment Mode**: `PAPER_SIMULATED` (Enforced default).
- **Execution Logic**:
  - Simulates fill at live bid/ask book midpoint plus fee ($10\text{ bps}$) and slippage ($\max(5\text{ bps}, \frac{1}{2}\text{spread})$).
  - Registers position into `data/analytics/jarvise.db` (`paper_positions`).
  - Attaches Two-Stage Profit Lock:
    - **Stage 1 (Breakeven)**: At $+0.5R$ favorable excursion, stop moves to entry $+20\text{ bps}$ fee buffer.
    - **Stage 2 (Trail 1R)**: At $\ge +1.0R$ favorable excursion, stop trails at $\text{Peak Price} - 1.0R$.
  - Invariant: Stops only tighten; they never loosen.

---

## 4. OpenClaw Skills & MCP Server Catalog

### Configured Skills Manifest (`plugins/jarvise-openclaw/openclaw.plugin.json`)
1. `jarvise-signal-analyzer`: Stage 1 multi-timeframe signal generation.
2. `jarvise-risk-guard`: Stage 2 deterministic guardrail gatekeeper.
3. `jarvise-order-router`: Stage 3 paper execution router and ledger bridge.
4. `crypto-market-intel`: Fear & Greed index, TVL, and macro context.
5. `blackswan`: Precursor risk alarms (funding rate spikes, liquidation clusters).
6. `secretclaw`: Secure environment key injection with zero chat exposure.
7. `ggshield-scanner`: Git pre-commit secret leak prevention.
8. `jarvise-paper-research`: Doctrine RAG note generator.
9. `jarvise-doctrine-rag`: Qdrant vector retrieval bridge.
10. `jarvise-binance-intel`: Public order book and market data inspector.
11. `jarvise-news-ingest`: Periodic intelligence briefing aggregator.

### MCP Servers (`config/openclaw/openclaw.json`)
- `tradingview`: Zero-auth Python 3.13 MCP server for charts and indicators.
- `cmc-mcp`: CoinMarketCap dominance and Fear & Greed context.
- `coingecko`: Macro numeric truth and global volume.
- `jarvise-qdrant`: Qdrant collection `jarvise_doctrine`.
- `jarvise-redis`: Redis telemetry and fail-closed kill-switch state.

---

## 5. Verification Scripts & Acceptance Evidence

### 1. Volatility Stop-Loss & Lock Engine
- Script: `scripts/jarvise_volatility_stop_loss.py`
- Test Command: `.venv\Scripts\python.exe scripts/jarvise_volatility_stop_loss.py --test`
- Results:
  - Long 1.5x ATR Stop ($68,000 entry, $1,200 ATR $\rightarrow$ $66,200 stop) [PASS]
  - Short 1.5x ATR Stop ($68,000 entry, $1,200 ATR $\rightarrow$ $69,800 stop) [PASS]
  - Rejection of tight stops $< 1.5\times\text{ATR}$ [PASS]
  - Chandelier exit calculation [PASS]
  - Two-stage lock: Breakeven (+20 bps) at $+0.5R$ [PASS]
  - Two-stage lock: Trailing 1.0R from peak favorable excursion [PASS]
  - Capital preservation invariant: Stop never loosens [PASS]

### 2. End-to-End Pipeline Verification
- Script: `scripts/verify_openclaw_paper_pipeline.py`
- Test Command: `.venv\Scripts\python.exe scripts/verify_openclaw_paper_pipeline.py`
- Results:
  - Step 1: OpenClaw Skills & Manifest integrity (11/11 skills verified) [PASS]
  - Step 2: Volatility stop-loss calculator unit tests [PASS]
  - Step 3.1: Low-confidence rejection ($0.52 < 0.65 \rightarrow \text{FLAT}$) [PASS]
  - Step 3.2: Asset whitelist gate (`DOGEUSDT` rejected) [PASS]
  - Step 3.3: Volatility stop floor validation ($\ge 1.5\times\text{ATR}$) [PASS]
  - Step 3.4: 1% Equity Risk Ceiling per trade [PASS]
  - Step 3.5: Daily cumulative loss halt and account drawdown lock [PASS]
  - Step 3.6: Jarvise canonical SQLite database audit (18 active tables verified) [PASS]
