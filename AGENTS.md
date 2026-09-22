## Learned User Preferences
- Trade worldwide (not locked to Binance TH); orient to positive, stable, trusted returns; paper-first, then live only when explicitly gated.
- Cover crypto plus stocks/ETFs; prefer slow, steady gains over aggressive returns.
- Be owner-protective: capital preservation, risk guardrails, kill-switch; live trading off by default.
- End-state product goal is auto-trade plus an analytics UI plus exchange accounts (e.g. Binance); climb paper → manual approval → autonomy — do not jump to live orders.
- Treat the Gemini notebook as trading doctrine, not a live price or order-book feed.
- Use fetch/Firecrawl subagents for historical and current market data; combine that with notebook rules before any trade call.
- Prefer Auto/Composer for cost; do not switch to Claude/GPT unless asked or Auto/Composer fails.
- Prefer paper analytics fixes that make backtests reproducible (closed candles only, warm-up withholding, gap reporting, `--since` backfill) over knowledge-vault scaffolding.

## Learned Workspace Facts
- GitHub remote is `chatoe-norm/jarvise`; push via SSH host `github.com-chatoe-norm`. HTTPS as `normstudiox` is pull-only. GitHub CLI (`gh`) must use account `chatoe-norm`; do not use `normstudiox`.
- Gemini Notebook MCP must use `nlm` profile `chatoe` (`chatoe@gmail.com`), not `default`/`normstudiox`. Alias `jarvise` = `14e11c63-e2ee-4b49-898f-b0cc4c61cb4e`; config at `config/notebook.json`.
- Canonical app is the Python CLI (`pyproject.toml`, `src/jarvise`). On Windows use `.venv\Scripts\jarvise` or `.venv\Scripts\python.exe -m pytest`; npm `ask-repo` is secondary.
- Shipped surface today: paper analytics + `/analytics` UI + local paper auto-trade (`jarvise paper`); still no live exchange order placement. Day-to-day invoke paths are in `docs/product-usage.md` (CLI, VPS n8n→jobs, agent/MCP); product phases P0–P5 are in `docs/superpowers/specs/2026-09-23-product-roadmap-design.md`.
- Paper trading is a local multi-asset ledger filled from live market data; venue testnets are optional and not required.
- Numeric market truth lives in SQLite/Parquet (`data/analytics/jarvise.db`, `jarvise_ingest`); indicators must be recomputed from the full stored series, not per-fetch windows; Obsidian/markdown vaults are not for OHLCV time series.
- TradingView / tradingview-mcp is a research overlay only — do not replace Binance→SQLite numeric truth or fill the paper ledger from TV signals; use both layers. TV MCP is stronger for screener/MTF/chat backtest; Jarvise custom ingest is stronger for reproducible OHLCV, paper ledger, kill-switch, and audit — do not discard custom ingest to “migrate” to TV.
- Deploy target is Hostinger KVM 2 ID `1269762` (`srv1269762.hstgr.cloud`, Ubuntu 24.04, 2 vCPU, 8 GB RAM, 100 GB, Malaysia). SSH user `root`. Tailscale IP `100.93.110.48`. Hostinger VPS is the 24/7 runtime; Tailscale is private ingress only — control plane (n8n, OpenClaw, web) binds to Tailscale, not the public IPv4.
- Stack lives at `/opt/jarvise` on that VPS. OpenClaw uses OpenRouter model `openrouter/openrouter/auto`.
- VPS NotebookLM/`nlm` session data for non-Cursor doctrine ops lives at `/opt/jarvise/data/nlm` (synced from local `%USERPROFILE%\.notebooklm-mcp-cli`).
- Doctrine RAG is Qdrant collection `jarvise_doctrine`. Obsidian is not a RAG store; Obsidian vault as a Jarvise market-data/intelligence store was explicitly discarded — do not revive unless asked.
- Self-hosted GitHub Actions runner is installed on the Hostinger VPS (user `ghrunner`, name `srv1269762`, label `jarvise`) at `/opt/actions-runner`. Push to `main` runs CI on `ubuntu-latest`, then deploy via that runner using `/opt/jarvise/infra/deploy/vps-deploy.sh`. Pull requests must not run on the self-hosted runner.
