# Jarvise paper ingest + next slice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep paper-only market ingest + MCP background plane healthy on `main`, then unify the dual CLI (`bin/jarvise` + local `src/jarvise`) and land notebook/ask-repo workspace assets without enabling order placement.

**Architecture:** GitHub `main` already ships `jarvise_ingest` (SQLite upserts) behind bash `bin/jarvise ingest`, plus Redis/Qdrant/n8n/OpenClaw MCP scaffolding. Local WIP adds a Typer workspace CLI (`src/jarvise`), Gemini notebook config/skill, and npm `ask-repo`. Next work merges those into one agent-facing surface: one `pyproject.toml`, one `jarvise` entrypoint, ingest remains GET-only.

**Tech Stack:** Python 3.11+, httpx, sqlite3, Typer (workspace commands), bash `bin/jarvise` (thin Windows-friendly shim or dispatch), Docker Compose (Redis/Qdrant/n8n), optional sentence-transformers + qdrant-client for doctrine RAG, Node/`@cursor/sdk` for ask-repo, Gemini Notebook MCP via `nlm` profile `chatoe`.

## Global Constraints

- Venue preference: Binance TH for trading later; paper OHLC may use public Binance klines until TH-specific data is wired.
- No order placement in this plan (paper analytics + research/signal context only).
- Live trading off by default; kill-switch / approval ladder: paper → manual approval → autonomy.
- Gemini notebook is doctrine only, not a live price or order-book feed.
- Ingest HTTP allowlist stays GET-only market data.
- Push to `chatoe-norm/jarvise` via SSH host `github.com-chatoe-norm` only when the user asks.
- Windows: prefer `.venv\Scripts\python.exe -m …` / `.venv\Scripts\pytest`; bash `bin/jarvise` may need Git Bash or a PowerShell twin.

**Spec:** [docs/superpowers/specs/2026-09-21-paper-ingest-design.md](../specs/2026-09-21-paper-ingest-design.md)  
**Remote:** https://github.com/chatoe-norm/jarvise (`origin` @ `4207099` as of sync)

---

## Status (as of 2026-09-21 sync)

### Phase 1 — paper ingest (SHIPPED on `main`)

- [x] `src/jarvise_ingest/` providers (Binance klines, CoinGlass), indicators, SQLite upsert, CLI
- [x] `bin/jarvise ingest` dispatch + status/init/doctor bash scaffold
- [x] `pyproject.toml` package `jarvise-ingest` / script `jarvise-ingest`
- [x] Tests: `tests/test_indicators.py`, `tests/test_db_upsert.py`, `tests/test_cli.py` (ingest)
- [x] `.env.example` CoinGlass + background plane keys; `*.db` gitignored
- [x] Doctrine extracts under `data/analytics/`

### Phase 2 — background MCP plane (SHIPPED scaffolding on `main`)

- [x] `docker-compose.yml` (Redis, Qdrant, n8n)
- [x] `scripts/rag_index_doctrine.py` + optional `[rag]` extras
- [x] `infra/n8n/run-ingest-host.sh` + schedule workflow JSON
- [x] `mcp/jarvise-mcp.json.example` (paper-only; OpenClaw execution tools disabled)

### Local WIP (present on disk, not all on `origin/main`)

- [ ] `src/jarvise/` Typer workspace CLI (status/init/config) — conflicts with bash status/init naming
- [ ] `config/notebook.json` + `.cursor/skills/jarvise-notebook/SKILL.md`
- [ ] `scripts/ask-repo.ts` + `package.json` / `tsconfig.json`
- [ ] `AGENTS.md` learned prefs; README sections for notebook / ask-repo
- [ ] Salvaged older CLI package metadata in `.local-wip/` (gitignored) — do not commit; fold into Task 1

### Explicitly out of this plan (later phases)

- `jarvise analyze` (regime / confidence / invalidation / Kelly sizing)
- Order-book / CryptoQuant / Glassnode writers
- Paper fill simulator and manual-approval live gate
- Any Binance TH signed trading endpoints

---

## File map (next slice)

| Path | Responsibility |
|------|----------------|
| `pyproject.toml` | Single project: deps for ingest + Typer; console scripts |
| `src/jarvise/` | Workspace Typer app: status, init, config; mounts or shells out to ingest |
| `src/jarvise_ingest/` | Unchanged paper ingest library (GET-only) |
| `bin/jarvise` | Prefer thin wrapper → `python -m jarvise` (keep bash help parity) |
| `tests/test_workspace_cli.py` | Typer workspace CLI tests (from salvage) |
| `tests/test_cli.py` | Keep ingest CLI tests |
| `config/notebook.json` | Notebook IDs / nlm profile |
| `.cursor/skills/jarvise-notebook/SKILL.md` | Agent workflow for doctrine queries |
| `scripts/ask-repo.ts` | Optional Cursor SDK repo Q&A |
| `.env.example` | Document `CURSOR_API_KEY` + existing ingest/MCP keys |
| `README.md` | One quick start: ingest primary, notebook + ask-repo secondary |

---

### Task 1: Unify packages — one `jarvise` entrypoint

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/jarvise/cli.py` (add `ingest` command that delegates)
- Modify: `bin/jarvise` (dispatch to Python module when available)
- Create: `tests/test_workspace_cli.py` (from `.local-wip/test_workspace_cli.py`)
- Keep: `src/jarvise_ingest/**`, `tests/test_cli.py`, `tests/test_indicators.py`, `tests/test_db_upsert.py`

**Interfaces:**
- Consumes: `jarvise_ingest.cli.main` (or internal `run_ingest(argv: list[str]) -> int` if extracted)
- Produces: console script `jarvise` → Typer app; `jarvise ingest …` exit codes match current ingest CLI (0/1/2)

- [ ] **Step 1: Write failing workspace CLI tests**

Copy salvage tests into `tests/test_workspace_cli.py` importing `from jarvise.cli import app`. Keep ingest tests untouched.

- [ ] **Step 2: Run workspace tests — expect fail if package not wired**

```bash
.venv\Scripts\python.exe -m pytest tests/test_workspace_cli.py -v
```

Expected: FAIL (module/script not installed or wrong entrypoint) until Step 3–4.

- [ ] **Step 3: Merge `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "jarvise"
version = "0.1.0"
description = "Jarvise paper analytics + agent workspace CLI (no order placement)"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
  "httpx>=0.27",
  "typer>=0.12",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]
rag = [
  "qdrant-client>=1.9",
  "sentence-transformers>=3.0",
]

[project.scripts]
jarvise = "jarvise.cli:app"
jarvise-ingest = "jarvise_ingest.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 4: Add Typer `ingest` that delegates to ingest argv**

In `src/jarvise/cli.py`, add a catch-all style command (or `context_settings={"allow_extra_args": True, "ignore_unknown_options": True}`) that forwards remaining args to `jarvise_ingest.cli.main` and exits with its return code. Do not reimplement providers.

Minimal pattern:

```python
@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def ingest(ctx: typer.Context) -> None:
    """Paper market ingest into SQLite (no order placement)."""
    from jarvise_ingest.cli import main as ingest_main

    code = ingest_main(["ingest", *ctx.args]) if False else ingest_main(ctx.args)
    # Prefer calling the argparse entry with the raw argv fragment after `ingest`.
    raise typer.Exit(code if isinstance(code, int) else 0)
```

Adjust to match the real `jarvise_ingest.cli.main` signature (today it likely parses `sys.argv`). If `main()` always reads `sys.argv`, temporarily set:

```python
import sys
from jarvise_ingest.cli import main as ingest_main

old = sys.argv
try:
    sys.argv = ["jarvise-ingest", *ctx.args]
    ingest_main()
finally:
    sys.argv = old
```

Wrap with `try/except SystemExit as e` and `raise typer.Exit(e.code)`.

- [ ] **Step 5: Reinstall editable and run all tests**

```bash
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -v
.venv\Scripts\jarvise --help
.venv\Scripts\jarvise ingest --help
```

Expected: all pytest PASS; help lists status/init/config/ingest; ingest help matches paper-only flags.

- [ ] **Step 6: Point `bin/jarvise` ingest (and prefer Python for status when available)**

When `python -m jarvise` works, have bash `cmd_ingest` exec:

```bash
exec python -m jarvise ingest "$@"
```

Keep bash status/init/doctor as fallbacks until Typer parity is verified on Windows via `.venv\Scripts\jarvise`.

- [ ] **Step 7: Smoke ingest (network optional)**

```bash
.venv\Scripts\jarvise ingest --symbol BTCUSDT --timeframe 1h --limit 5 --skip-derivatives --dry-run --json
```

Expected: JSON with `"dry_run": true`, no DB write requirement.

- [ ] **Step 8: Commit (only when user asks)**

```bash
git add pyproject.toml src/jarvise bin/jarvise tests/test_workspace_cli.py
git commit -m "feat: unify workspace Typer CLI with paper ingest entrypoint"
```

---

### Task 2: Land notebook doctrine assets

**Files:**
- Create (already on disk): `config/notebook.json`
- Create (already on disk): `.cursor/skills/jarvise-notebook/SKILL.md`
- Modify: `README.md` (keep Gemini Notebook section; ensure IDs match config)
- Modify: `AGENTS.md` only if facts drift (already correct)

**Interfaces:**
- Consumes: nlm profile `chatoe`, notebook id `14e11c63-e2ee-4b49-898f-b0cc4c61cb4e`
- Produces: agents can resolve alias `jarvise` without guessing accounts

- [ ] **Step 1: Verify `config/notebook.json` matches live notebook**

```json
{
  "alias": "jarvise",
  "google_account": "chatoe@gmail.com",
  "nlm_profile": "chatoe",
  "notebook_id": "14e11c63-e2ee-4b49-898f-b0cc4c61cb4e",
  "title": "Jarvise : Crypto Trader",
  "url": "https://notebook.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e"
}
```

- [ ] **Step 2: Confirm skill points at config + `chatoe` profile only**

Reject any skill text that tells agents to use `default` / `normstudiox` for this notebook.

- [ ] **Step 3: Commit notebook assets (when user asks)**

```bash
git add config/notebook.json .cursor/skills/jarvise-notebook/SKILL.md README.md AGENTS.md
git commit -m "docs: add Jarvise Gemini notebook config and agent skill"
```

---

### Task 3: Land ask-repo helper

**Files:**
- Create (on disk): `scripts/ask-repo.ts`, `package.json`, `package-lock.json`, `tsconfig.json`
- Modify: `.env.example` (include `CURSOR_API_KEY=`)
- Modify: `README.md` Ask-the-repo section

**Interfaces:**
- Consumes: `CURSOR_API_KEY` from env / `.env`
- Produces: `npm run ask-repo -- "<question>"` exit 0/1/2 as documented

- [ ] **Step 1: Ensure `.env.example` documents the key without a real secret**

```bash
# Cursor ask-repo (scripts/ask-repo.ts) — do not commit real keys
CURSOR_API_KEY=
```

- [ ] **Step 2: Local smoke (requires user key; skip in CI)**

```bash
npm install
npm run ask-repo -- "list top-level packages under src/"
```

Expected: exit 0 with a short layout answer, or exit 1 with a clear missing-key message if unset.

- [ ] **Step 3: Commit (when user asks)**

```bash
git add scripts/ask-repo.ts package.json package-lock.json tsconfig.json .env.example README.md
git commit -m "feat: add ask-repo Cursor SDK helper for local Q&A"
```

---

### Task 4: Operator checklist — background plane (no code)

**Files:** none required if compose files already match spec; touch only if Redis key names drift.

- [ ] **Step 1: Bring stack up**

```bash
cp .env.example .env
# set N8N_BASIC_AUTH_PASSWORD
docker compose up -d
docker compose ps
```

Expected: redis, qdrant, n8n healthy/running.

- [ ] **Step 2: Index doctrine + run host ingest helper**

```bash
.venv\Scripts\pip install -e ".[rag]"
.venv\Scripts\python.exe scripts/rag_index_doctrine.py --query "expectancy FLAT"
bash infra/n8n/run-ingest-host.sh
```

Expected: Qdrant query returns hits; Redis key `jarvise:ingest:last` updated (or script prints equivalent status).

- [ ] **Step 3: Import n8n workflow** `infra/n8n/workflows/jarvise-ingest-schedule.json` and wire Redis `redis://redis:6379`. Prefer host helper if the container cannot see the Windows venv.

---

### Task 5: Close Phase-1 acceptance (verification gate)

Run before declaring the unify slice done:

- [ ] `jarvise ingest --symbol BTCUSDT --timeframe 1h --skip-derivatives --json` upserts and exits 0
- [ ] With `COINGLASS_API_KEY`, omit `--skip-derivatives` and confirm `derivatives_analytics` upserts
- [ ] Second identical run does not duplicate rows
- [ ] `--dry-run` writes nothing
- [ ] Grep confirms no trade/order endpoints:

```bash
rg -n "order|trade|signed|apiKey" src/jarvise_ingest -g '!**/__pycache__/**'
```

Expected: only market-data / header-key usage for CoinGlass reads; no order placement paths.

---

## Self-review

1. **Spec coverage:** Phase 1 acceptance + background plane from the design doc map to Status (shipped) and Tasks 4–5. Follow-ups (`analyze`, order-book writers, paper fills) stay out of scope.
2. **Placeholder scan:** Tasks include concrete file paths, commands, and merge targets; no TBD steps.
3. **CLI naming:** Task 1 resolves `jarvise` vs `jarvise-ingest` dual packages in favor of one agent surface while keeping `jarvise-ingest` script as a compatibility alias.

---

## Execution handoff

Plan updated and saved to `docs/superpowers/plans/2026-09-21-paper-ingest-mcp.md`.

**Subagent model:** Default (`inherit` — session model). Do not pin Composer Fast or Grok unless the user overrides.

**Two execution options:**

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks  
2. **Inline Execution** — run tasks in this session with checkpoints  

Which approach?
