# Jarvise P3 — exchange read-only spot balances — design

**Date:** 2026-09-23  
**Status:** Implemented on branch `feature/p3-exchange-readonly` (2026-09-23); awaiting review/merge  
**Depends on:** `/analytics` added on control web when P2 page is absent  
**Plan:** [`../plans/2026-09-23-p3-exchange-readonly.md`](../plans/2026-09-23-p3-exchange-readonly.md)  
**Related:** [`2026-09-21-paper-ingest-design.md`](2026-09-21-paper-ingest-design.md) (public GET ingest — unchanged)

## Goal

Read-only **live spot wallet balances** from Binance into Jarvise: show them on `/analytics` when keys work, optionally sync via CLI, and persist each successful fetch as a snapshot in SQLite. **No order placement.** Owner-protective soft fail when keys are missing or the venue call fails.

## Non-goals (this slice)

- Futures / margin / earn balances
- Order, trade, cancel, or withdraw APIs
- Multi-venue adapters beyond Binance spot
- USD valuation or PnL vs paper ledger
- Background n8n / jobs schedule for balance sync
- Changing `jarvise_ingest` to use signed account endpoints

## Locked decisions

1. Thin package `jarvise_exchange` + `VenueClient` protocol; Binance spot first adapter
2. Spot wallet balances only
3. Live fetch on `/analytics` page load + optional CLI
4. Persist snapshot to `exchange_balances` on each successful fetch
5. Soft fail on web: hide exchange panel until keys work
6. Secrets: `BINANCE_API_KEY` / `BINANCE_API_SECRET` in env only; no order APIs; ingest stays public GET-only

## Architecture

```
GET /analytics  ──┐
                  ├──► sync_spot_balances() ──► VenueClient.list_spot_balances()
jarvise exchange  ┘              │                      │
  sync-balances                  │                      ▼
                                 │            BinanceSpotClient
                                 │            signed GET /api/v3/account
                                 ▼
                    exchange_balances (SQLite snapshot)
                                 │
                    web: Exchange panel (success only)
                    CLI: text / --json summary
```

**Boundary:** `jarvise_exchange` may perform **signed read** of the spot account. It must never call order/trade/withdraw endpoints. `jarvise_ingest` remains unsigned public market data only (klines, CoinGlass).

**Package layout:**

```text
src/jarvise_exchange/
  __init__.py
  models.py       # SpotBalance, SyncResult
  protocol.py     # VenueClient Protocol
  binance_spot.py # HMAC + GET /api/v3/account
  db.py           # exchange_balances schema + insert/latest
  sync.py         # shared sync helper
  cli.py          # Typer commands (or wired from jarvise.cli)
```

## Components

### 1. Models (`models.py`)

- `SpotBalance`: `venue: str`, `asset: str`, `free: Decimal`, `locked: Decimal`, `total: Decimal` (never `float`)
- `SyncResult`: `ok: bool`, `dry_run: bool`, `venue: str`, `fetched_at_ms: int | None`, `balances: list[SpotBalance]`, `inserted: int`, `error: str | None`

### 2. Protocol (`protocol.py`)

```python
class VenueClient(Protocol):
    def list_spot_balances(self) -> list[SpotBalance]: ...
```

Binance adapter is the only implementation in this slice.

### 3. Binance spot adapter (`binance_spot.py`)

- Base URL: `https://api.binance.com` (fixed for this slice)
- Auth: `BINANCE_API_KEY` header `X-MBX-APIKEY`; query signed with `BINANCE_API_SECRET` (HMAC-SHA256)
- Allowlisted endpoint: **`GET /api/v3/account` only**
- Map each balance entry to `SpotBalance(venue="binance", ...)`; `total = free + locked`
- Injectable `httpx.Client` for tests
- `resolve_binance_credentials() -> tuple[str, str] | None` via `os.environ.get` (same style as CoinGlass)

### 4. Sync helper (`sync.py`)

```text
sync_spot_balances(*, client: VenueClient, db_path: Path, dry_run: bool = False) -> SyncResult
```

1. Call `client.list_spot_balances()`
2. Drop assets where `free == 0` and `locked == 0`
3. Unless `dry_run`: open DB, migrate, insert one batch sharing a single `fetched_at_ms`
4. Return `SyncResult` (never raise for empty filtered list — that is success with `inserted=0`)

Callers choose soft vs hard failure: web catches exceptions / missing keys and hides the panel; CLI maps missing keys and errors to exit codes.

### 5. Database (`db.py` + `exchange_balances`)

Default path: `data/analytics/jarvise.db` (same DB as paper ingest).

```sql
CREATE TABLE IF NOT EXISTS exchange_balances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  venue TEXT NOT NULL,
  asset TEXT NOT NULL,
  free TEXT NOT NULL,
  locked TEXT NOT NULL,
  total TEXT NOT NULL,
  fetched_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exchange_balances_venue_fetched
  ON exchange_balances (venue, fetched_at_ms DESC);
```

- Amounts stored as **TEXT** (decimal strings)
- Timestamps: **INTEGER Unix milliseconds UTC**
- Append-only snapshots; UI/CLI “current” view = rows for `MAX(fetched_at_ms)` per venue
- Failed fetches write **nothing**

Document the same `CREATE TABLE` in `data/analytics/mvas-schema.sql` for discoverability; the runtime writer lives in `jarvise_exchange.db`.

### 6. Web surface

- On `GET /analytics` (P2 page): if credentials resolve, call `sync_spot_balances` once per request
- On success: render an **Exchange (spot)** panel (asset / free / locked / total + fetch time)
- On missing keys or any sync failure: **omit the panel**; rest of the page unchanged (soft fail)
- Never put API keys or secrets in HTML or JSON
- Prefer keeping the control dashboard at `/` unchanged unless P2 already folded analytics there

### 7. CLI

```text
jarvise exchange sync-balances [--db PATH] [--dry-run] [--json]
```

- Wired as a Typer sub-app under `jarvise` (same pattern as `rag`)
- Missing credentials → exit **2** + example to set `BINANCE_API_KEY` / `BINANCE_API_SECRET`
- Sync/network/DB failure → exit **1** with provider/status/retry hint (no secrets)
- Success → exit **0**; `--json` prints `SyncResult`-shaped payload including `paper_only: true` / read-only note
- `--dry-run`: fetch and report; **no DB write**

### 8. Secrets

- `.env.example`: `BINANCE_API_KEY=`, `BINANCE_API_SECRET=`
- Real values only in `.env` (gitignored)
- Docker Compose web service may optionally pass these env vars when the operator sets them
- Never log secrets

## Data flow

1. **Trigger:** `GET /analytics` or `jarvise exchange sync-balances`
2. **Resolve credentials** from env
3. **Web + missing keys:** skip sync; hide panel; continue page render
4. **CLI + missing keys:** exit 2
5. **Build** `BinanceSpotClient` → signed `GET /api/v3/account`
6. **Map + filter** zero balances
7. **Persist** batch to `exchange_balances` unless dry-run
8. **Web success:** show panel from this result (or latest DB batch)
9. **Web failure after keys present:** hide panel; log warning without secrets
10. **CLI:** print summary; non-zero exit on failure

Ingest path (`jarvise ingest`) is untouched and does not share signing code.

## Error handling

| Case | Web (`/analytics`) | CLI |
|------|--------------------|-----|
| Missing `BINANCE_API_KEY` or `BINANCE_API_SECRET` | Hide panel | Exit 2 + example |
| HTTP 401/403 / bad signature | Hide panel; warn | Exit 1 + status + retry hint |
| Network / timeout | Hide panel; warn | Exit 1 |
| All balances dust (filtered to empty) | Show panel empty / “no non-zero assets” | Exit 0, `inserted: 0` |
| DB migrate/write failure after fetch | Hide panel; warn; no partial write | Exit 1 |
| `--dry-run` | N/A | Fetch OK; no DB write; `dry_run: true` |

No interactive prompts.

## Testing

- Unit: HMAC query-string signing vs fixed fixture (known signature)
- Unit: account JSON → `SpotBalance` list; zeros filtered
- Unit: DB insert two batches → two `fetched_at_ms`; latest query returns newest only
- Unit: `sync_spot_balances` with fake `VenueClient` (no network)
- Web: missing keys → no exchange panel; mocked success → panel present
- Optional network: `JARVISE_NET_TESTS=1` + real keys; otherwise skip
- Regression: existing ingest tests pass; ingest providers remain unsigned GET-only

## Success criteria

1. With valid keys: `/analytics` shows spot balances and a new `exchange_balances` snapshot exists
2. Without keys: `/analytics` loads; exchange panel absent; no crash
3. CLI `sync-balances --json` returns structured ok/error; `--dry-run` writes nothing
4. No order/trade/withdraw endpoints in the package allowlist; secrets never logged

## Repo layout (new / touched)

```text
src/jarvise_exchange/          # new package
tests/test_exchange_*.py       # new tests
src/jarvise/cli.py             # add exchange Typer group
src/jarvise_web/…              # /analytics exchange panel (P2 surface)
.env.example                   # BINANCE_API_* 
data/analytics/mvas-schema.sql # document exchange_balances
pyproject.toml                 # package already discovered via src/
```
