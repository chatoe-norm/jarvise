# Jarvise product usage (paper phase)

**Status:** P4-C live submit code shipped gated off (`JARVISE_LIVE_TRADING=false`). Approve stays paper-only until owner enables trade keys + flag on VPS.

```mermaid
flowchart LR
  subgraph human [You / Cursor agent]
    CLI["jarvise CLI"]
    Notebook["Gemini Notebook doctrine"]
    OpenClaw["OpenClaw research notes"]
  end
  subgraph auto [VPS schedules]
    n8n["n8n"]
    jobs["jobs:8090"]
  end
  subgraph data [Truth stores]
    SQLite["SQLite jarvise.db"]
    Qdrant["Qdrant jarvise_doctrine"]
    Redis["Redis status + kill_switch"]
  end
  CLI -->|"ingest / analyze / rag"| SQLite
  CLI --> Qdrant
  n8n --> jobs
  jobs -->|"POST /jobs/ingest"| SQLite
  jobs -->|"POST /jobs/rag-refresh"| Qdrant
  Notebook --> CLI
  OpenClaw --> CLI
  CLI --> Redis
```

## 1. Primary invoke path — Python CLI

One-time install:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

Entrypoint: `.venv\Scripts\jarvise` (see [`src/jarvise/cli.py`](../src/jarvise/cli.py)). On macOS/Linux use `.venv/bin/jarvise`.

| Command | Purpose |
|---------|---------|
| `jarvise status` / `init` / `config` | Local workspace |
| `jarvise ingest ...` | Fetch OHLCV (+ derivatives + Binance book + CoinGecko macro) → `data/analytics/jarvise.db`. Derivatives default = Binance Futures public funding/OI (no key); CoinGlass when `COINGLASS_API_KEY` is set |
| `jarvise analyze ...` | Read DB → regime / confidence / size (paper) |
| `jarvise paper ...` | Enqueue / approve paper fills into local ledger (no exchange orders) |
| `jarvise exchange ...` | Read-only spot balance sync (Binance first; no order placement). `/analytics` shows ~USD via public USDT tickers. |
| `jarvise rag ...` | Sync doctrine → Qdrant |

Daily local examples:

```powershell
.venv\Scripts\jarvise ingest --symbol BTCUSDT --timeframe 1h --limit 200 --json
.venv\Scripts\jarvise analyze --symbol BTCUSDT --timeframe 4h --json
.venv\Scripts\jarvise paper run --symbol BTCUSDT --timeframe 4h --json
.venv\Scripts\jarvise paper status --json
```

Compatibility aliases `jarvise-ingest` / `jarvise-analyze` still work; prefer `jarvise ...`.

### Paper + approval (P2/P4 paper slice)

`.venv/bin/jarvise paper run --symbol BTCUSDT --timeframe 4h --json`
→ enqueues `approval_queue` (default). Approve on `/analytics` or:

`.venv/bin/jarvise paper approve <id> --json`

Immediate fill escape hatch:

`.venv/bin/jarvise paper run --symbol BTCUSDT --timeframe 4h --auto-fill --json`

Expire timed-out pendings (no position change):

`.venv/bin/jarvise paper expire --json`

Expectancy (round-trips after fees):

`.venv/bin/jarvise paper metrics --json`  
`.venv/bin/jarvise paper metrics --persist --json`  # optional upsert into `performance_risk_metrics`

Also on `/analytics` → **Paper expectancy** card + `GET /api/paper/metrics`.

Historical replay (stored candles only):

```bash
# analysis-only (may use default DB)
.venv/bin/jarvise analyze --replay --since 2024-01-01 --universe paper_core --timeframe 4h --json

# paper fills require an isolated DB copy (refuses default live ledger)
cp data/analytics/jarvise.db /tmp/jarvise-bt.db
.venv/bin/jarvise analyze --replay --since 2024-01-01 --until 2024-06-01 \
  --universe paper_core --timeframe 4h --db /tmp/jarvise-bt.db --apply-paper --json
```

On Windows use `.venv\Scripts\jarvise` instead of `.venv/bin/jarvise`. Set `JARVISE_APPROVAL_TIMEOUT_MIN` in `.env` (see `.env.example`).

## 2. VPS product (24/7) — scheduled, not click-driven

Deploy per [hostinger-vps.md](deploy/hostinger-vps.md): stack at `/opt/jarvise`, Tailscale only.

**Automatic** (n8n → jobs service in [`src/jarvise/jobs.py`](../src/jarvise/jobs.py)):

- Every ~15 minutes: `POST /jobs/ingest` → refresh market data
- Every ~6 hours: `POST /jobs/rag-refresh` → refresh doctrine RAG
- Every ~4 hours: `POST /jobs/paper-run` → enqueue paper candidates (`paper_core` @ `4h` by default; **no** `--auto-fill`), then `POST /jobs/paper-auto-decide` → no-op unless `JARVISE_PAPER_AUTO_DECIDE=true`
- Every ~1 hour: `POST /jobs/paper-expire` → mark timed-out approvals (timeout → **hold** if open paper position is already the same side; otherwise timeout → FLAT)
- Every ~1 hour: `POST /jobs/ingest-health` → Telegram alert when `ema_200` warm-up is missing, candles are stale, or the series has gaps (alert only; fix with the one-shot backfill shown in the message)

Approve/Reject on the Command Dashboard Home (`http://$TAILSCALE_IP:8080/`) or `jarvise paper approve|reject`. Legacy `/analytics` serves the same SPA.
Optional Telegram alerts when `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` are set (enqueue + hourly pending digest via n8n `Jarvise paper pending digest`). Soft-fail if unset.

**Recommendation card (Thai):** every pending row on Home has "ดูคำแนะนำ" — what happened, dollar risk, doctrine, and a checklist, so you can Approve/Reject without reading charts. The Approve button is labelled "(แนะนำ)" or "(ระวัง)" from the template rule (`conf ≥ 0.70` approve, `0.55–0.69` caution, otherwise reject). Buttons are never disabled by the card.

**Paper auto-decide (optional, default off):** set `JARVISE_PAPER_AUTO_DECIDE=true` + `OPENROUTER_API_KEY` on the `jobs` service. After each paper-run, Claude reviews candidates that passed Jarvise's filters and approves (simulated fill, reason `auto:claude:approve`), rejects (`auto:claude:reject:<reason>`), or defers. Same-symbol **same-side** open is auto-held in code without Claude (`auto:rule:same_side_hold` → approve no-op); **opposite-side** open is forced defer (`auto:rule:opposite_side_open`). Deferred rows stay pending and you get one Telegram summary per run. Spec: [paper auto-decide](superpowers/specs/2026-10-02-paper-auto-decide-design.md). Live stays off; the job refuses when `JARVISE_LIVE_TRADING=true`.

**n8n after deploy:** workflows are files, not auto-imported — in n8n re-import `infra/n8n/workflows/jarvise-paper-run.json` (adds the auto-decide node) and import + activate `jarvise-ingest-health.json`.

**Manual on VPS when needed:**

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile tools run --rm worker \
  jarvise analyze --symbol BTCUSDT --timeframe 4h --json
```

UIs in this phase: n8n (`:5678`), Command Dashboard (`:8080` — Home / Paper / Decisions / Exchange / Ops) — not a public trading terminal.

## 3. Agent / Cursor path (chat, not app buttons)

- Cursor agent runs `.venv\Scripts\jarvise ...` per skill / README
- Doctrine: Gemini Notebook MCP + [`.cursor/skills/jarvise-notebook/SKILL.md`](../.cursor/skills/jarvise-notebook/SKILL.md) (`nlm` profile `chatoe`)
- Paper-only MCP example: [`mcp/jarvise-mcp.json.example`](../mcp/jarvise-mcp.json.example) (includes optional TradingView research MCP)
- OpenClaw writes notes → `jarvise rag sync-openclaw` → Qdrant (no orders)

### Research overlay vs controlled truth

| Layer | Source | Role |
|-------|--------|------|
| Controlled numeric contract | `jarvise ingest` → SQLite (today: Binance public klines) | OHLCV + indicators that drive analyze/paper; **venue-agnostic** — any provider that is efficient, stable, and secure can be added later |
| Doctrine | NotebookLM → `jarvise rag sync-notebook` → Qdrant | Rules / risk; not a price feed |
| Research overlay | [tradingview-mcp](https://github.com/atilaahmettaner/tradingview-mcp) (Cursor MCP) | Screener / MTF / chat backtest — **do not** silent-merge into `market_technicals` or drive paper/live fills alone |

Trade **worldwide** (not Binance-only). New venues/accounts must plug into Jarvise with efficiency, stability, and security (no withdrawal keys; live stays ladder-gated).

Windows note for TradingView MCP: pin `uvx --python 3.13` (3.14 not supported yet). Optional official connector: `https://mcp.tradingview.com/mcp` if you have Essential+.

## 4. Owner day workflow

1. **Market data fresh** — n8n ingest on VPS, or `jarvise ingest` by hand
2. **Paper signal** — `jarvise analyze --json` (or `--universe paper_core`)
3. **Paper candidate (P2/P4)** — `jarvise paper run ...` enqueues approval by default; approve on Command Dashboard Home (`:8080/`) or `jarvise paper approve <id>` (or `--auto-fill` for immediate simulated fill; no exchange orders)
4. **Check doctrine** — Notebook / RAG before any human decision
5. **Kill switch** — Redis `jarvise:kill_switch` per [ops.md](deploy/ops.md) to stop background work and block enqueue/approve

**Not in this product yet (default):** live orders stay off until `JARVISE_LIVE_TRADING=true` + `BINANCE_TRADE_*` keys (spot trade only, no withdraw). Spec: [P4-C live submit](superpowers/specs/2026-09-27-p4c-live-submit-design.md). Full path: [product roadmap](superpowers/specs/2026-09-23-product-roadmap-design.md).

## Related

- [README quick start](../README.md)
- [Product roadmap (P0–P5)](superpowers/specs/2026-09-23-product-roadmap-design.md)
- [P1 Analytics UI plan](superpowers/plans/2026-09-23-p1-analytics-ui.md) — `/analytics` on VPS (`http://$TAILSCALE_IP:8080/analytics`)
- [shadcn/ui for Jarvise](ux-ui/shadcn-for-jarvise.md) — future UX rewrite knowledge only (shipped UI stays FastAPI HTML)
- [P2 Paper auto-trade plan](superpowers/plans/2026-09-23-p2-paper-auto-trade.md) — `jarvise paper run|status`
- [P3 Exchange read-only plan](superpowers/plans/2026-09-23-p3-exchange-readonly.md) — `jarvise exchange sync-balances`, `/analytics` spot panel
- [P4 Manual approval paper slice plan](superpowers/plans/2026-09-23-p4-manual-approval-paper-slice.md) — queue, CLI approve/reject/expire, `/analytics` card
- [P4-C live submit](superpowers/specs/2026-09-27-p4c-live-submit-design.md) — size-capped live spot on Approve; flag default off
- [Paper ingest design](superpowers/specs/2026-09-21-paper-ingest-design.md)
- [OpenClaw intelligence audit](superpowers/specs/2026-09-22-openclaw-intelligence-audit.md)
