# Jarvise paper ingest (SQLite + CLI) — design

**Date:** 2026-09-21  
**Status:** Phase 1+2 shipped on `main` (2026-09-21); indicator reproducibility fixed 2026-09-22; see plan for CLI unify / notebook / ask-repo next slice  
**Notebook:** [Jarvise : Crypto Trader](https://notebooklm.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e)  
**Doctrine source:** `data/analytics/sources/jarvise-doctrine.txt`, `data/analytics/api-map.json`

## Goal

Local, agent-friendly **paper data ingest** that fills the first two MVAS tables from live market APIs, with **no order placement** of any kind. This is phase 1 of the Jarvise deployment ladder: paper analytics only.

## Non-goals (this slice)

- Live or paper order execution
- Analyzer / regime / Kelly CLI (`jarvise analyze`)
- Dashboard UI
- Order-book, on-chain, or performance tables (schema reserved; writers deferred)

## Background plane (Extend A — in scope)

Greenfield Docker + MCP **extends** this CLI; it does not replace the write path:

- Redis (`jarvise:ingest:*` status)
- Qdrant collection `jarvise_doctrine` (RAG over `data/analytics/sources/`)
- n8n schedule calling `jarvise ingest` (paper only)
- OpenClaw MCP for research/signal context — **no live execution tools**

On-demand CLI remains valid; background scheduling is additive.

## Architecture

```
bin/jarvise ingest
        │
        ├─ flags (--symbol, --timeframe, --limit, --dry-run, --json, --skip-derivatives)
        │
        ▼
  Python package: jarvise_ingest/
        │
        ├─ providers/
        │     binance_klines.py   → OHLC klines (no API key)
        │     coinglass.py        → OI, funding, liquidations (CG-API-KEY)
        ├─ indicators.py          → ATR-14, RSI-14, EMA-20/200 math + warm-up rule
        ├─ series.py              → recompute indicators over the whole stored series
        ├─ db.py                  → SQLite open, migrate, upsert
        └─ cli.py                 → argparse, dry-run plan, JSON summary
        │
        ▼
  data/analytics/jarvise.db       (gitignored)
        ├─ market_technicals
        └─ derivatives_analytics
```

Existing bash `bin/jarvise` stays the entrypoint: it dispatches `ingest` to `python -m jarvise_ingest` (or a thin wrapper) so agents keep one command surface.

## Components

### 1. CLI surface (agent-friendly)

```text
jarvise ingest --help

Options:
  --symbol SYMBOL       Trading pair, e.g. BTCUSDT (repeatable or comma-separated)
  --timeframe TF        One of: 15m, 1h, 4h, 1d (default: 1h)
  --limit N             Candles to fetch (default: 200, max: 1000)
  --skip-derivatives    Skip CoinGlass (OHLC-only run)
  --db PATH             SQLite path (default: data/analytics/jarvise.db)
  --dry-run             Print plan; write nothing
  --json                Machine-readable summary on stdout
  --yes                 Reserved; no interactive prompts

Examples:
  jarvise ingest --symbol BTCUSDT --timeframe 1h --limit 200
  jarvise ingest --symbol BTCUSDT,ETHUSDT --dry-run --json
  jarvise ingest --symbol BTCUSDT --skip-derivatives
```

Missing required `--symbol` → fail fast with the example invocation above (CLI-for-agents pattern).

### 2. Providers

| Provider | Auth | Writes | Notes |
|---|---|---|---|
| Binance public klines | none | `market_technicals` OHLC+volume | Base URL fixed to `https://api.binance.com` (`GET /api/v3/klines`). Paper OHLC only — not live Binance TH trading. Symbol stored as passed (e.g. `BTCUSDT`). The still-forming final kline is dropped, so one fewer row than `--limit` is normal. |
| Local indicators | n/a | ATR-14, RSI-14, EMA-20, EMA-200 | Recomputed over the **whole stored series** after each upsert, never from one fetch window. Warm-up values stay NULL until the recursion seed contributes under 1% (EMA-200 needs 660 candles). VWAP and ADX deferred. |
| CoinGlass V4 | `COINGLASS_API_KEY` (alias `CG-API-KEY`) | `derivatives_analytics` | Base `https://open-api-v4.coinglass.com`. MVP: `/api/futures/openInterest/ohlc-history`, `/api/futures/fundingRate/oi-weight-ohlc-history`, `/api/futures/liquidation/aggregated-history`. If key missing and `--skip-derivatives` not set → exit 2 with example to set env or pass `--skip-derivatives`. |

**Hard rule:** no provider may call any trade / order / account-balance-write endpoint. Ingest HTTP allowlist is GET-only market data.

### 3. Database

- Path: `data/analytics/jarvise.db` (add `*.db` / `jarvise.db` to `.gitignore` if not already covered).
- Schema: apply the relevant subset of `data/analytics/mvas-schema.sql` on first open (CREATE IF NOT EXISTS). Other tables may be created empty for forward compatibility.
- Upserts: `INSERT ... ON CONFLICT(primary key) DO UPDATE` so re-runs are idempotent.
- Timestamps: store as **INTEGER Unix milliseconds (UTC)** in all MVAS tables for this slice. Schema comments in SQL and `db.py` must state that.

### 4. Success / dry-run output

Human:

```text
ingested market_technicals: BTCUSDT 1h rows=200
ingested derivatives_analytics: BTC rows=30
db: /.../data/analytics/jarvise.db
duration: 1.2s
```

JSON (`--json`):

```json
{
  "ok": true,
  "dry_run": false,
  "db": ".../jarvise.db",
  "market_technicals": {"BTCUSDT": {"timeframe": "1h", "upserted": 200}},
  "derivatives_analytics": {"BTC": {"upserted": 30}},
  "skipped": [],
  "duration_s": 1.2
}
```

Dry-run prints the same shape with planned counts and `dry_run: true`, zero writes.

## Data flow

1. Parse flags; resolve DB path; open SQLite; migrate.
2. For each symbol: fetch klines (closed candles only) → upsert OHLCV into `market_technicals` → recompute indicator columns from the full stored series.
3. Unless `--skip-derivatives`: map `BTCUSDT` → `BTC`, fetch CoinGlass series → upsert `derivatives_analytics`.
4. Print summary; exit 0 on full success. Partial provider failure: exit non-zero after writing whatever succeeded, and list failures in JSON `errors[]`.

## Error handling

| Case | Behavior |
|---|---|
| Missing `--symbol` | Exit 2 + example |
| Network / HTTP error | Exit 1; message includes provider + status + retry hint |
| Missing CoinGlass key | Exit 2 unless `--skip-derivatives` |
| Empty kline response | Exit 1; do not wipe existing rows |
| Schema migrate failure | Exit 1; do not continue |

No interactive prompts. No hanging menus.

## Secrets

- `.env.example` documents `COINGLASS_API_KEY=`
- Real keys only in `.env` (already gitignored)
- Never log API keys

## Testing

- Unit: indicator math on a fixed OHLC fixture (ATR/RSI/EMA known values).
- Unit: upsert idempotency (insert twice → same row count).
- Integration (optional, network): `jarvise ingest --symbol BTCUSDT --skip-derivatives --limit 5` against public API when `JARVISE_NET_TESTS=1`.
- Smoke: `--dry-run --json` never creates/touches DB file if DB missing (or creates empty then rolls back — prefer **no DB write on dry-run**).

## Repo layout (new)

```text
bin/jarvise                          # add ingest dispatch
pyproject.toml                       # package metadata + deps (httpx, optional)
src/jarvise_ingest/
  __init__.py
  __main__.py
  cli.py
  db.py
  indicators.py
  providers/
    binance_klines.py
    coinglass.py
tests/
  test_indicators.py
  test_db_upsert.py
data/analytics/mvas-schema.sql       # existing
data/analytics/jarvise.db            # gitignored, runtime
.env.example
```

## Dependency stance

- Python 3.11+
- Minimal deps: `httpx` for HTTP; stdlib `sqlite3` and `argparse`
- No trading SDKs

## Acceptance criteria

1. `jarvise ingest --symbol BTCUSDT --timeframe 1h --skip-derivatives --json` upserts OHLC+indicators and exits 0.
2. With `COINGLASS_API_KEY` set, same command without `--skip-derivatives` also fills `derivatives_analytics`.
3. Second identical run does not duplicate rows (idempotent).
4. `--dry-run` writes nothing.
5. No code path can place an order.

## Follow-ups (explicitly out of this plan)

- `jarvise analyze` (regime / confidence / invalidation / sizing)
- Order-book and CryptoQuant/Glassnode writers
- Paper fill simulator and manual-approval mode
