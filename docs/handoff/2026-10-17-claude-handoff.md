# Jarvise Development Handover — 2026-10-17 (For Claude AI)

This document is the single source of truth for **Claude AI** to seamlessly take over development on the **Jarvise Autonomous Trading System & Command Dashboard**.

---

## 1. Executive Summary & Core Identity

- **System Purpose:** Jarvise is an **owner-protective, paper-first autonomous trading & analytics platform** for cryptocurrency and equity assets.
- **Operating Ladder:** `Paper simulation → Manual approval queue → Gated autonomy`. We never jump to live orders.
- **Live Trading Safety Gate:** `JARVISE_LIVE_TRADING=false` is enforced repo-wide and on VPS. The live order execution path is gated off behind an explicit flag and checklist ([docs/ops/live-enable-checklist.md](../ops/live-enable-checklist.md)).
- **Core Guardrail Doctrine:**
  - **Capital Preservation First:** Maximum risk at stop is capped at $\le 1.0\%$ of account equity.
  - **Stop-Loss Noise Floor:** Stop-loss distance must satisfy $|\text{Entry} - \text{Stop}| \ge 1.5\times\text{ATR}(14)$.
  - **No Pyramiding / Same Side Hold:** `auto:rule:same_side_hold` prevents multiple positions in the same direction on the same symbol. This is intentional risk preservation, not a bug.
  - **Confidence Floors:** Do not loosen `0.55` / `0.70` analyzer confidence floors to artificially increase trade frequency. Low confidence or market safety alert forces `FLAT`.
  - **Owner Copy Convention:** On the Command Dashboard, user-facing Thai copy must use **กฎการเทรด** (never "หลักคำสอน" or the English word "doctrine").
  - **Market Intel is Context Only:** Fear & Greed Index, Altcoin Season Index, and OpenClaw news digests are context overlays. They never serve as standalone trade signals.

---

## 2. Infrastructure, Git & Deployment Landscape

| Component | Target / Value | Notes |
| :--- | :--- | :--- |
| **Git Remote** | `chatoe-norm/jarvise` | Push via SSH `github.com-chatoe-norm`. HTTPS as `normstudiox` is pull-only. |
| **Active Branch** | `main` | Synchronized with `origin/main`. |
| **GitHub CLI (`gh`)** | `chatoe-norm` | Mandatory account. Do not use `normstudiox` or other accounts. |
| **Deploy Target** | Hostinger KVM ID `1811101` (`srv1811101`) | Ubuntu 24.04 LTS, 4 vCPU, 16 GB RAM, ~193 GB disk. Public IP `76.13.218.48`. |
| **Tailscale Ingress** | `100.89.171.36` (`jarvise-1811101`) | Private ingress only. Control plane binds to Tailscale, never public IPv4. |
| **Command Dashboard** | `http://100.89.171.36:8080` | Hybrid FastAPI + Vite/React 19 SPA. |
| **OpenClaw Control** | `http://100.89.171.36:18789` | Gateway token is stored per browser origin. |
| **Self-Hosted Runner** | `srv1811101` at `/opt/actions-runner` | Automatically triggers `/opt/jarvise/infra/deploy/vps-deploy.sh` on push to `main`. |
| **Ship Shortcuts** | **`fdpy`** & **`wdpy`** | `fdpy` = commit → open PR → merge to `main` → wait VPS deploy.<br>`wdpy` = wait for in-flight CI queue before next ship. |

---

## 3. What Was Recently Shipped (PR #162 – PR #165)

### 3.1 PR #162 & #163: Dynamic Altseason & De-duplicated Sentiment
- **Altcoin Season Index Hierarchy:** `cmc_altseason.py` dynamically computes rolling 90-day performance of benchmark alts vs BTC via Binance public REST klines (0–100 scale).
- **Sentiment De-duplication:** Fear & Greed modal provides 5 distinct market psychology drivers without repeating Home page news lines.
- **Desktop Centered Modal Dialog:** Replaced cramped card popovers with centered backdrop-blur dialogs and tab navigation.

### 3.2 PR #164: OpenClaw Tactical Intelligence & Post-Mortem Generator
- **CLI Subcommand:** `jarvise paper post-mortem` evaluates closed trades, extracts lessons, and writes structured tactic markdown files into `data/openclaw/exports/tactics/*.md`.
- **Recursive RAG Sync:** `jarvise rag sync-openclaw` uses `rglob` to index tactic notes into Qdrant collection `jarvise_doctrine` (`sentence-transformers/all-MiniLM-L6-v2`).
- **Claude Review Integration:** `auto_decide.py` retrieves these empirical post-mortem lessons during pending candidate reviews.

### 3.3 PR #165: Multi-Tier Model Routing & Market Context PWA Upgrade (`antigravity-sync-plan.md`)
Implements the 7-step blueprint across two phases:

#### Phase 1: OpenRouter Multi-Tier Model Routing
Configured in `config/openclaw/openclaw.json` and mirrored in `openclaw.json.example`:

| Agent Role | Primary Model | Fallback Models | Temp | Core Task |
| :--- | :--- | :--- | :---: | :--- |
| `DOCTRINE_JUDGE` | `anthropic/claude-sonnet-5` | `anthropic/claude-sonnet-4.6`, `anthropic/claude-opus-5.5` | `0.0` | 1% Risk enforcement, Token Audit Veto (`HIGH` $\rightarrow$ `FLAT`), 6 Pullback rules |
| `HEARTBEAT_INGEST` | `deepseek/deepseek-v4.1-flash` | `minimax/minimax-m3`, `google/gemini-3.8-flash` | `0.1` | 24/7 market scan every 30m, TradingView, CMC, CoinGlass V4 |
| `QUANT_ANALYZER` | `deepseek/deepseek-v4-pro` | `openai/gpt-5.6-luna`, `anthropic/claude-sonnet-5` | `0.0` | $EV$ formula, Stop-loss $\ge 1.5\times\text{ATR}$, Signal JSON |
| `CONTEXT_DIGEST` | `google/gemini-3.8-flash` | `z-ai/glm-5.3-flash`, `deepseek/deepseek-v4.1-flash` | `0.3` | Macro summary, Semi-dynamic Thai copy for Fear & Greed |
| `PAPER_SANDBOX` | `nvidia/nemotron-3-ultra-550b-a55b:free` | `nvidia/nemotron-3-super:free`, `google/gemini-3.8-flash` | `0.1` | Free Paper Trading sandbox simulation |

- **Session Compactor:** Created `plugins/jarvise-openclaw/skills/claw-compactor/SKILL.md` to trigger session compression when history exceeds `300KB`.
- **Ephemeral Prompt Caching:** Attached `cache_control: { type: "ephemeral" }` metadata for doctrine system prompts.
- **FastAPI Endpoint:** `GET /api/openclaw/model-routing` serves the full matrix.
- **Ops Page UI:** Added dedicated **`AI Models (5 Tiers)`** tab with `ModelRoutingCard.tsx`.

#### Phase 2: Semi-Dynamic Market Context Menu (Mobile-First PWA)
- **3-Part String Concatenation:** `[Static_Definition] + [Dynamic_Market_Cause] + [System_Action_Suggestion]` mapped across 5 score domains (`EXTREME_FEAR`, `FEAR`, `NEUTRAL`, `GREED`, `EXTREME_GREED`).
- **Responsive UX:**
  - **Desktop:** Centered backdrop-blur modal dialog with dual tabs (Fear & Greed / Altcoin Season).
  - **Mobile PWA:** Collapsible dual-button strip (height 66px, touch safe $\ge 48\times 48\text{px}$), smooth slide-up bottom sheet with drag handle, and `useHapticFeedback` (`navigator.vibrate(15)`).
- **Count-Up Animation:** 650ms cubic ease-out animation for numeric scores.
- **Quick Jump Shortcut:** `"ไปที่ Approval Queue ↑"` button closes sheet and smoothly scrolls to table.
- **Stale-While-Revalidate (SWR):** Instant `<0.1s` local storage cache rendering with background sync.

---

## 4. Architectural Patterns & File Structure

Jarvise enforces a **Strict Layered Architecture**. Never invoke database queries or external OpenRouter APIs directly from UI components.

```
web/src/
├── actions/                         # Server Actions & Controller Dispatchers
│   ├── model-routing.action.ts      # Fetch & sync model matrix with UI revalidation
│   └── market-context.action.ts     # Recompute 3-part context with UI revalidation
├── controllers/                     # React Hooks / State Controllers
│   ├── useMarketContext.ts          # SWR, animation, modal tabs, and quick actions
│   └── useHapticFeedback.ts         # Safe mobile vibration trigger
├── services/                        # Business Logic & Pure Computations
│   ├── modelRoutingService.ts       # Fallback chains, compaction limits, token cost
│   ├── openrouter-gateway.service.ts# Re-export gateway alias
│   ├── marketContextService.ts      # Domain mapping, 3-part concatenation, Zod parse
│   └── market-context.service.ts    # Re-export service alias
├── repositories/                    # Data Access & Caching Layer
│   ├── modelRoutingRepository.ts    # LocalStorage cache & execution log store
│   ├── ai-usage-log.repository.ts   # Re-export repository alias
│   ├── marketContextRepository.ts   # News bullet extraction, approval prioritization
│   └── market-context.repository.ts # Re-export repository alias
├── types/ & schemas/                # Zod Schemas & TypeScript Contracts
│   ├── modelRouting.ts              # AgentRole, AgentProfile, Matrix, ExecutionLog
│   ├── marketContext.ts             # ScoreDomain, DomainScoreMapping, SemiDynamicContext
│   ├── model-router.schema.ts       # Re-export schema
│   └── market-context.schema.ts     # Re-export schema
└── components/                      # UI Presentation Components
    ├── MarketContextCard.tsx        # Responsive Market Context Card
    ├── SentimentDetailOverlay.tsx   # Centered desktop dialog & mobile bottom sheet
    ├── ModelRoutingCard.tsx         # Multi-tier matrix card on Ops page
    └── ...
```

### Python Backend & OpenClaw

```
src/
├── jarvise/
│   ├── model_routing.py             # OpenRouter Multi-Tier Model Matrix resolver
│   ├── rag.py                       # Qdrant collection jarvise_doctrine vector indexer
│   └── jobs.py                      # Schedulers & pipeline runner
├── jarvise_web/
│   ├── app.py                       # FastAPI application & REST endpoints
│   └── schemas.py                   # Pydantic schemas
├── jarvise_paper/
│   ├── auto_decide.py               # Claude paper reviewer (OpenRouter auto-decide)
│   ├── post_mortem.py               # Tactical post-mortem markdown generator
│   └── llm_openrouter.py            # Low-level OpenRouter HTTP client
└── jarvise_risk/
    └── __init__.py                  # 1% risk ceiling, drawdown lock, kill switch
config/openclaw/
├── openclaw.json                    # OpenClaw configuration with multi-tier agent profiles
└── openclaw.json.example            # Committed example template
plugins/jarvise-openclaw/skills/
└── claw-compactor/SKILL.md          # Session compaction skill
```

---

## 5. Verification Commands & Quality Gates

Run these commands before any commit or merge:

### Frontend (`web/`)
```powershell
cd web
npm run lint      # tsc -b --noEmit (strict type check)
npm test          # tsx --test src/**/*.test.ts (16 tests)
npm run build     # tsc -b && vite build (production bundle)
cd ..
```

### Python Backend (Virtualenv `.venv`)
```powershell
# In workspace root (d:\Projects\jarvise)
.venv\Scripts\ruff.exe check src tests              # Lint
.venv\Scripts\ruff.exe format --check src tests     # Code style formatting
.venv\Scripts\python.exe -m mypy src               # Static type checking (95 files)
.venv\Scripts\python.exe -m pytest                 # Full test suite (543 passed)
```

### Remote API Verification (VPS)
```powershell
# Model Routing Matrix endpoint
curl -s http://100.89.171.36:8080/api/openclaw/model-routing

# System Health
curl -s http://100.89.171.36:8080/healthz
```

---

## 6. Critical Rules for Claude AI to Maintain

1. **Do not enable live trading:** `JARVISE_LIVE_TRADING` must remain `false`.
2. **Do not alter execution guardrails:** Keep the 1.5× ATR stop distance, 1% risk ceiling, and `same_side_hold` rule intact.
3. **OpenClaw Config Safety:** In `openclaw.json`, `agents.entries.*.identity` must always be an object (`name`/`emoji`/`theme`), never a plain string. Never add undocumented keys like `_jarvise`.
4. **Deploy Lifecycle:** Always use the `fdpy` cycle (`commit → branch → PR → merge to main → verify VPS deploy`). Pull requests run CI on `ubuntu-latest`; push to `main` deploys on the self-hosted runner `srv1811101`.
5. **Obsidian / Neo4j / External Autotrade Bots:** Do not import Freqtrade, NautilusTrader, ccxt, Hummingbot, or Neo4j into Jarvise execution paths. SQLite (`data/analytics/jarvise.db`) and Qdrant (`jarvise_doctrine`) are the sources of truth.

---

## 7. Next Recommended Steps for Development

1. **Monitor Auto-Decide Sample Size:** Accumulate ~8+ closed `auto_*` paper trades before considering setting `JARVISE_AUTO_DECIDE_MIN_EV` or restoring `JARVISE_ANALYZE_MTF=true`.
2. **OpenClaw Microstructure Assessment:** Leverage the `heartbeat_ingest` and `quant_analyzer` profiles to automatically flag funding rate spikes or order book skew in paper trade review context.
3. **PWA Offline Persistence:** Cache recent closed trades and indicator snapshots in IndexedDB for offline inspection.
