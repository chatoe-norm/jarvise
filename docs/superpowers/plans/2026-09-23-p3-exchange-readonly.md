# P3 Exchange Read-Only Spot Balances — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch live Binance spot wallet balances (signed read only), persist snapshots to `exchange_balances`, show them on `/analytics` when keys work, and expose `jarvise exchange sync-balances`.

**Architecture:** New thin package `jarvise_exchange` with a `VenueClient` protocol, `BinanceSpotClient` (HMAC + `GET /api/v3/account` only), shared `sync_spot_balances` helper, and SQLite append-only snapshots. Web soft-fails (hide panel); CLI hard-fails with exit codes. `jarvise_ingest` stays public GET-only.

**Tech Stack:** Python 3.11+, httpx, sqlite3, Decimal, Typer, FastAPI (`jarvise_web`), pytest.

**Spec:** [docs/superpowers/specs/2026-09-23-p3-exchange-readonly-design.md](../specs/2026-09-23-p3-exchange-readonly-design.md)

## Global Constraints

- No order, trade, cancel, or withdraw APIs anywhere in `jarvise_exchange`.
- Spot wallet balances only (not futures/margin).
- Secrets only via `BINANCE_API_KEY` / `BINANCE_API_SECRET` env; never log or return them.
- Amounts: `Decimal` in Python, TEXT decimal strings in SQLite; never `float`.
- Timestamps: INTEGER Unix milliseconds UTC.
- Soft fail on web: missing keys or sync error → omit exchange panel; page still loads.
- Ingest HTTP allowlist remains GET-only public market data (no signed account calls in `jarvise_ingest`).
- Prefer implementing on / after `feature/p2-paper-auto-trade` if that branch has `/analytics`; on `main`, add `GET /analytics` to `src/jarvise_web/app.py`.
- Commit only when the user asks; plan commit steps are for the implementing agent when commits are authorized.

---

## File map

| Path | Responsibility |
|------|----------------|
| `src/jarvise_exchange/__init__.py` | Package marker / public exports |
| `src/jarvise_exchange/models.py` | `SpotBalance`, `SyncResult` |
| `src/jarvise_exchange/protocol.py` | `VenueClient` Protocol |
| `src/jarvise_exchange/binance_spot.py` | Credentials, HMAC sign, account fetch, mapping |
| `src/jarvise_exchange/db.py` | Schema migrate, insert snapshot, latest query |
| `src/jarvise_exchange/sync.py` | `sync_spot_balances` |
| `src/jarvise_exchange/cli.py` | Typer `exchange` group + `sync-balances` |
| `src/jarvise/cli.py` | `app.add_typer(exchange_app, name="exchange")` |
| `src/jarvise_web/app.py` | `GET /analytics` + soft-fail exchange panel |
| `data/analytics/mvas-schema.sql` | Document `exchange_balances` |
| `.env.example` | `BINANCE_API_KEY=`, `BINANCE_API_SECRET=` |
| `docker-compose.yml` | Optional pass-through of Binance env to `web` |
| `tests/test_exchange_signing.py` | HMAC fixture |
| `tests/test_exchange_binance_map.py` | Account JSON → balances + zero filter |
| `tests/test_exchange_db.py` | Snapshot insert + latest |
| `tests/test_exchange_sync.py` | Fake VenueClient sync |
| `tests/test_exchange_cli.py` | CLI exit codes / dry-run |
| `tests/test_exchange_web.py` | Soft fail / panel presence |

---

### Task 1: Models + protocol

**Files:**
- Create: `src/jarvise_exchange/__init__.py`
- Create: `src/jarvise_exchange/models.py`
- Create: `src/jarvise_exchange/protocol.py`
- Test: `tests/test_exchange_models.py`

**Interfaces:**
- Produces: `SpotBalance(venue, asset, free, locked, total)`, `SyncResult(...)`, `VenueClient.list_spot_balances() -> list[SpotBalance]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_exchange_models.py
from decimal import Decimal

from jarvise_exchange.models import SpotBalance, SyncResult


def test_spot_balance_total_is_decimal():
    b = SpotBalance(
        venue="binance",
        asset="BTC",
        free=Decimal("1.5"),
        locked=Decimal("0.5"),
        total=Decimal("2.0"),
    )
    assert b.total == Decimal("2.0")
    assert isinstance(b.free, Decimal)


def test_sync_result_defaults():
    r = SyncResult(
        ok=True,
        dry_run=False,
        venue="binance",
        fetched_at_ms=1,
        balances=[],
        inserted=0,
        error=None,
    )
    assert r.ok is True
    assert r.error is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_exchange_models.py -v`  
Expected: FAIL (import / module not found)

- [ ] **Step 3: Write minimal implementation**

```python
# src/jarvise_exchange/__init__.py
"""Read-only exchange spot balances. No order placement."""

# src/jarvise_exchange/models.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SpotBalance:
    venue: str
    asset: str
    free: Decimal
    locked: Decimal
    total: Decimal


@dataclass
class SyncResult:
    ok: bool
    dry_run: bool
    venue: str
    fetched_at_ms: int | None
    balances: list[SpotBalance]
    inserted: int
    error: str | None


# src/jarvise_exchange/protocol.py
from __future__ import annotations

from typing import Protocol

from jarvise_exchange.models import SpotBalance


class VenueClient(Protocol):
    def list_spot_balances(self) -> list[SpotBalance]: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_exchange_models.py -v`  
Expected: PASS

- [ ] **Step 5: Commit** (when user authorizes)

```bash
git add src/jarvise_exchange tests/test_exchange_models.py
git commit -m "$(cat <<'EOF'
feat(exchange): add SpotBalance models and VenueClient protocol

EOF
)"
```

---

### Task 2: Binance HMAC signing + account mapping

**Files:**
- Create: `src/jarvise_exchange/binance_spot.py`
- Test: `tests/test_exchange_signing.py`
- Test: `tests/test_exchange_binance_map.py`

**Interfaces:**
- Consumes: `SpotBalance`
- Produces: `resolve_binance_credentials() -> tuple[str, str] | None`, `sign_query(secret, query_string) -> str`, `BinanceSpotClient.list_spot_balances()`, `balances_from_account_payload(payload) -> list[SpotBalance]` (filter zeros)

- [ ] **Step 1: Write failing signing + mapping tests**

```python
# tests/test_exchange_signing.py
from jarvise_exchange.binance_spot import sign_query


def test_sign_query_known_fixture():
    # Binance docs-style: HMAC-SHA256 hex digest of query string with secret
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    expected = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    assert sign_query(secret, query) == expected
```

Note: digest verified locally against Binance’s published example query/secret pair (HMAC-SHA256 hex).

```python
# tests/test_exchange_binance_map.py
from decimal import Decimal

from jarvise_exchange.binance_spot import balances_from_account_payload


def test_balances_from_account_filters_zeros():
    payload = {
        "balances": [
            {"asset": "BTC", "free": "0.10000000", "locked": "0.00000000"},
            {"asset": "ETH", "free": "0.00000000", "locked": "0.00000000"},
            {"asset": "USDT", "free": "10.5", "locked": "1.5"},
        ]
    }
    rows = balances_from_account_payload(payload)
    assets = {b.asset: b for b in rows}
    assert set(assets) == {"BTC", "USDT"}
    assert assets["USDT"].total == Decimal("12.0")
    assert assets["BTC"].venue == "binance"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_exchange_signing.py tests/test_exchange_binance_map.py -v`  
Expected: FAIL

- [ ] **Step 3: Implement `binance_spot.py`**

```python
# src/jarvise_exchange/binance_spot.py
"""Binance spot account read — signed GET /api/v3/account only. No orders."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from jarvise_exchange.models import SpotBalance

BINANCE_BASE = "https://api.binance.com"
ACCOUNT_PATH = "/api/v3/account"


def resolve_binance_credentials() -> tuple[str, str] | None:
    key = os.environ.get("BINANCE_API_KEY") or ""
    secret = os.environ.get("BINANCE_API_SECRET") or ""
    if not key or not secret:
        return None
    return key, secret


def sign_query(secret: str, query_string: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def balances_from_account_payload(payload: dict[str, Any]) -> list[SpotBalance]:
    out: list[SpotBalance] = []
    for row in payload.get("balances") or []:
        free = Decimal(str(row["free"]))
        locked = Decimal(str(row["locked"]))
        if free == 0 and locked == 0:
            continue
        out.append(
            SpotBalance(
                venue="binance",
                asset=str(row["asset"]),
                free=free,
                locked=locked,
                total=free + locked,
            )
        )
    return out


class BinanceSpotClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = BINANCE_BASE,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._client = client
        self._base_url = base_url.rstrip("/")

    def list_spot_balances(self) -> list[SpotBalance]:
        params = {"timestamp": int(time.time() * 1000)}
        query = urlencode(params)
        signature = sign_query(self._api_secret, query)
        headers = {"X-MBX-APIKEY": self._api_key}
        url = f"{self._base_url}{ACCOUNT_PATH}?{query}&signature={signature}"
        own = self._client is None
        http = self._client or httpx.Client(timeout=30.0)
        try:
            resp = http.get(url, headers=headers)
            resp.raise_for_status()
            return balances_from_account_payload(resp.json())
        finally:
            if own:
                http.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exchange_signing.py tests/test_exchange_binance_map.py -v`  
Expected: PASS

- [ ] **Step 5: Commit** (when user authorizes)

```bash
git add src/jarvise_exchange/binance_spot.py tests/test_exchange_signing.py tests/test_exchange_binance_map.py
git commit -m "$(cat <<'EOF'
feat(exchange): add Binance spot signed account read adapter

EOF
)"
```

---

### Task 3: SQLite `exchange_balances` + sync helper

**Files:**
- Create: `src/jarvise_exchange/db.py`
- Create: `src/jarvise_exchange/sync.py`
- Modify: `data/analytics/mvas-schema.sql` (append `exchange_balances` DDL)
- Test: `tests/test_exchange_db.py`
- Test: `tests/test_exchange_sync.py`

**Interfaces:**
- Consumes: `VenueClient`, `SpotBalance`
- Produces: `open_db(path)`, `migrate(conn)`, `insert_snapshot(conn, venue, balances, fetched_at_ms) -> int`, `latest_snapshot(conn, venue) -> tuple[int, list[SpotBalance]] | None`, `sync_spot_balances(*, client, db_path, dry_run=False, venue="binance") -> SyncResult`

- [ ] **Step 1: Write failing DB + sync tests**

```python
# tests/test_exchange_db.py
from decimal import Decimal
from pathlib import Path

from jarvise_exchange.db import insert_snapshot, latest_snapshot, migrate, open_db
from jarvise_exchange.models import SpotBalance


def test_insert_two_batches_latest_is_newest(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = open_db(db)
    migrate(conn)
    b1 = [SpotBalance("binance", "BTC", Decimal("1"), Decimal("0"), Decimal("1"))]
    b2 = [SpotBalance("binance", "ETH", Decimal("2"), Decimal("0"), Decimal("2"))]
    insert_snapshot(conn, "binance", b1, fetched_at_ms=1000)
    insert_snapshot(conn, "binance", b2, fetched_at_ms=2000)
    latest = latest_snapshot(conn, "binance")
    assert latest is not None
    ts, rows = latest
    assert ts == 2000
    assert rows[0].asset == "ETH"
    conn.close()
```

```python
# tests/test_exchange_sync.py
from decimal import Decimal
from pathlib import Path

from jarvise_exchange.models import SpotBalance
from jarvise_exchange.sync import sync_spot_balances


class FakeClient:
    def list_spot_balances(self):
        return [
            SpotBalance("binance", "BTC", Decimal("1"), Decimal("0"), Decimal("1")),
            SpotBalance("binance", "DUST", Decimal("0"), Decimal("0"), Decimal("0")),
        ]


def test_sync_persists_and_filters(tmp_path: Path):
    db = tmp_path / "t.db"
    result = sync_spot_balances(client=FakeClient(), db_path=db, dry_run=False)
    assert result.ok
    assert result.inserted == 1
    assert len(result.balances) == 1
    assert result.balances[0].asset == "BTC"


def test_sync_dry_run_writes_nothing(tmp_path: Path):
    db = tmp_path / "t.db"
    result = sync_spot_balances(client=FakeClient(), db_path=db, dry_run=True)
    assert result.ok and result.dry_run
    assert result.inserted == 0
    assert not db.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_exchange_db.py tests/test_exchange_sync.py -v`  
Expected: FAIL

- [ ] **Step 3: Implement `db.py` and `sync.py`; document schema**

`db.py`: `CREATE TABLE IF NOT EXISTS exchange_balances` exactly as in the spec; store amounts with `format(d, 'f')` or `str(d)`; `insert_snapshot` inserts one row per balance sharing `fetched_at_ms`; `latest_snapshot` selects `MAX(fetched_at_ms)` then all rows for that venue+ts.

`sync.py`:

```python
def sync_spot_balances(
    *,
    client: VenueClient,
    db_path: Path,
    dry_run: bool = False,
    venue: str = "binance",
    fetched_at_ms: int | None = None,
) -> SyncResult:
    raw = client.list_spot_balances()
    balances = [b for b in raw if b.free != 0 or b.locked != 0]
    ts = fetched_at_ms if fetched_at_ms is not None else int(time.time() * 1000)
    if dry_run:
        return SyncResult(True, True, venue, ts, balances, 0, None)
    conn = open_db(db_path)
    try:
        migrate(conn)
        n = insert_snapshot(conn, venue, balances, ts)
        conn.commit()
    finally:
        conn.close()
    return SyncResult(True, False, venue, ts, balances, n, None)
```

Append the DDL block from the spec to `data/analytics/mvas-schema.sql`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exchange_db.py tests/test_exchange_sync.py -v`  
Expected: PASS

- [ ] **Step 5: Commit** (when user authorizes)

```bash
git add src/jarvise_exchange/db.py src/jarvise_exchange/sync.py data/analytics/mvas-schema.sql tests/test_exchange_db.py tests/test_exchange_sync.py
git commit -m "$(cat <<'EOF'
feat(exchange): persist append-only exchange_balances snapshots

EOF
)"
```

---

### Task 4: CLI `jarvise exchange sync-balances`

**Files:**
- Create: `src/jarvise_exchange/cli.py`
- Modify: `src/jarvise/cli.py` (import + `app.add_typer`)
- Test: `tests/test_exchange_cli.py`

**Interfaces:**
- Consumes: `resolve_binance_credentials`, `BinanceSpotClient`, `sync_spot_balances`
- Produces: Typer command `exchange sync-balances`; exit 2 missing keys; exit 1 sync error; exit 0 success

- [ ] **Step 1: Write failing CLI tests**

Use `typer.testing.CliRunner` against the root `jarvise.cli:app` (or `jarvise_exchange.cli:exchange_app` if testing the group in isolation).

```python
# tests/test_exchange_cli.py
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from jarvise.cli import app
from jarvise_exchange.models import SpotBalance

runner = CliRunner()


def test_missing_keys_exit_2(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    result = runner.invoke(app, ["exchange", "sync-balances", "--json"])
    assert result.exit_code == 2
    assert "BINANCE_API_KEY" in result.output


def test_dry_run_json_no_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    db = tmp_path / "x.db"

    def fake_list(self):
        return [SpotBalance("binance", "BTC", Decimal("1"), Decimal("0"), Decimal("1"))]

    with patch("jarvise_exchange.binance_spot.BinanceSpotClient.list_spot_balances", fake_list):
        result = runner.invoke(
            app,
            ["exchange", "sync-balances", "--dry-run", "--json", "--db", str(db)],
        )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["inserted"] == 0
    assert not db.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_exchange_cli.py -v`  
Expected: FAIL

- [ ] **Step 3: Implement CLI and wire into `jarvise.cli`**

```python
# src/jarvise_exchange/cli.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from jarvise_exchange.binance_spot import BinanceSpotClient, resolve_binance_credentials
from jarvise_exchange.sync import sync_spot_balances

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "analytics" / "jarvise.db"

exchange_app = typer.Typer(
    name="exchange",
    help="Read-only exchange spot balances (no order placement).",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@exchange_app.command("sync-balances")
def sync_balances(
    db: Annotated[Optional[Path], typer.Option("--db")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    creds = resolve_binance_credentials()
    if creds is None:
        typer.echo(
            "Missing BINANCE_API_KEY / BINANCE_API_SECRET.\n"
            "Example:\n"
            "  export BINANCE_API_KEY=...\n"
            "  export BINANCE_API_SECRET=...\n"
            "  jarvise exchange sync-balances --json",
            err=True,
        )
        raise typer.Exit(2)
    api_key, api_secret = creds
    db_path = db or DEFAULT_DB
    try:
        client = BinanceSpotClient(api_key, api_secret)
        result = sync_spot_balances(client=client, db_path=db_path, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        msg = f"exchange sync failed: {exc}"
        if as_json:
            typer.echo(json.dumps({"ok": False, "error": msg, "read_only": True}))
        else:
            typer.echo(msg, err=True)
        raise typer.Exit(1) from exc
    payload = {
        "ok": result.ok,
        "dry_run": result.dry_run,
        "venue": result.venue,
        "fetched_at_ms": result.fetched_at_ms,
        "inserted": result.inserted,
        "balances": [
            {
                "asset": b.asset,
                "free": str(b.free),
                "locked": str(b.locked),
                "total": str(b.total),
            }
            for b in result.balances
        ],
        "read_only": True,
        "paper_only": True,
    }
    if as_json:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(f"venue={result.venue} inserted={result.inserted} assets={len(result.balances)}")
```

In `src/jarvise/cli.py`:

```python
from jarvise_exchange.cli import exchange_app
app.add_typer(exchange_app, name="exchange")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exchange_cli.py -v`  
Expected: PASS

- [ ] **Step 5: Commit** (when user authorizes)

```bash
git add src/jarvise_exchange/cli.py src/jarvise/cli.py tests/test_exchange_cli.py
git commit -m "$(cat <<'EOF'
feat(exchange): add jarvise exchange sync-balances CLI

EOF
)"
```

---

### Task 5: Web `/analytics` soft-fail panel + env docs

**Files:**
- Modify: `src/jarvise_web/app.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml` (`web.environment` pass-through)
- Test: `tests/test_exchange_web.py`

**Interfaces:**
- Consumes: `resolve_binance_credentials`, `BinanceSpotClient`, `sync_spot_balances`
- Produces: `GET /analytics` HTML; exchange panel only when sync succeeds

- [ ] **Step 1: Write failing web tests**

Use FastAPI `TestClient`. Clear Binance env for soft-fail; monkeypatch sync for success.

```python
# tests/test_exchange_web.py
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from jarvise_exchange.models import SpotBalance, SyncResult
from jarvise_web.app import app

client = TestClient(app)


def test_analytics_hides_panel_without_keys(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    resp = client.get("/analytics")
    assert resp.status_code == 200
    assert "Exchange (spot)" not in resp.text


def test_analytics_shows_panel_on_successful_sync(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    fake = SyncResult(
        ok=True,
        dry_run=False,
        venue="binance",
        fetched_at_ms=1,
        balances=[SpotBalance("binance", "BTC", Decimal("1"), Decimal("0"), Decimal("1"))],
        inserted=1,
        error=None,
    )
    with patch("jarvise_web.app.sync_spot_balances", return_value=fake):
        with patch("jarvise_web.app.BinanceSpotClient"):
            resp = client.get("/analytics")
    assert resp.status_code == 200
    assert "Exchange (spot)" in resp.text
    assert "BTC" in resp.text
    assert "BINANCE_API" not in resp.text
```

If P2 already defines `/analytics`, extend that handler instead of replacing it; keep paper panels intact and only gate the exchange block.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_exchange_web.py -v`  
Expected: FAIL (no `/analytics` or no panel)

- [ ] **Step 3: Implement `/analytics` + env wiring**

In `src/jarvise_web/app.py`:

1. Import `BinanceSpotClient`, `resolve_binance_credentials`, `sync_spot_balances`.
2. Default DB path: `os.environ.get("JARVISE_DB", "data/analytics/jarvise.db")`.
3. Add `@app.get("/analytics")` that:
   - Always renders a minimal analytics page shell (title Analytics; reuse existing `page()` CSS/cards).
   - If `resolve_binance_credentials()` is None → no exchange card.
   - Else try `sync_spot_balances(...)`; on exception → no exchange card (log warning without secrets).
   - On success → card titled `Exchange (spot)` listing asset/free/locked/total and `fetched_at_ms`.
4. Do not add secrets to HTML.

`.env.example` add:

```bash
# Binance spot read-only balances (P3). Never commit real keys. No order APIs.
BINANCE_API_KEY=
BINANCE_API_SECRET=
```

`docker-compose.yml` under `web.environment`:

```yaml
- BINANCE_API_KEY=${BINANCE_API_KEY:-}
- BINANCE_API_SECRET=${BINANCE_API_SECRET:-}
- JARVISE_DB=/data/analytics/jarvise.db
```

Only add `JARVISE_DB` volume mount if the web image already mounts `data/analytics`; otherwise keep default path and document that snapshot persistence in the container needs the analytics volume (match existing ingest volume patterns if present).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exchange_web.py -v`  
Expected: PASS

Also run: `pytest tests/test_exchange_*.py tests/test_binance_klines.py -v`  
Expected: all PASS (ingest unchanged)

- [ ] **Step 5: Commit** (when user authorizes)

```bash
git add src/jarvise_web/app.py .env.example docker-compose.yml tests/test_exchange_web.py
git commit -m "$(cat <<'EOF'
feat(exchange): soft-fail spot balances panel on /analytics

EOF
)"
```

---

### Task 6: Regression smoke + success criteria check

**Files:** none new (verification only)

- [ ] **Step 1: Run full exchange + ingest unit suite**

Run: `pytest tests/test_exchange_*.py tests/test_db_upsert.py tests/test_binance_klines.py -v`  
Expected: PASS

- [ ] **Step 2: Confirm allowlist / soft-fail criteria manually in code review**

Checklist against the spec success criteria:

1. `BinanceSpotClient` only requests `GET /api/v3/account`
2. Web without keys: `/analytics` 200 and no `Exchange (spot)`
3. CLI without keys: exit 2
4. CLI `--dry-run` creates no DB file
5. No secrets in HTML or CLI JSON keys beyond env names in error examples

- [ ] **Step 3: Optional net test** (skip unless `JARVISE_NET_TESTS=1` and real keys)

```bash
JARVISE_NET_TESTS=1 jarvise exchange sync-balances --json
```

Expected: `"ok": true` and rows in `data/analytics/jarvise.db` table `exchange_balances`.

- [ ] **Step 4: Commit** only if Task 6 produced doc/test fixes the user asked to keep

---

## Spec coverage (self-review)

| Spec requirement | Task |
|------------------|------|
| `jarvise_exchange` + `VenueClient` + Binance adapter | 1–2 |
| Spot only; signed GET account | 2 |
| `exchange_balances` append-only snapshots | 3 |
| Shared `sync_spot_balances` | 3 |
| CLI `jarvise exchange sync-balances` | 4 |
| `/analytics` live fetch + soft fail | 5 |
| Env secrets + `.env.example` | 5 |
| Ingest unchanged / regression | 6 |
| Unit tests (HMAC, map, DB, sync, web) | 2–5 |

**Placeholder scan:** none intentional.  
**Type consistency:** `SpotBalance` / `SyncResult` / `sync_spot_balances(*, client, db_path, dry_run)` used uniformly across tasks.
