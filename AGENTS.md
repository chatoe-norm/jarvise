## Learned User Preferences
- Prefer Binance TH as the trading venue; paper-first, then live only when explicitly gated.
- Be owner-protective: capital preservation, risk guardrails, kill-switch; live trading off by default.
- Treat the Gemini notebook as trading doctrine, not a live price or order-book feed.
- Use fetch/Firecrawl subagents for historical and current market data; combine that with notebook rules before any trade call.

## Learned Workspace Facts
- GitHub remote is `chatoe-norm/jarvise`; push via SSH host `github.com-chatoe-norm`. HTTPS as `normstudiox` is pull-only.
- Gemini Notebook MCP must use `nlm` profile `chatoe` (`chatoe@gmail.com`), not `default`/`normstudiox`. Alias `jarvise` = `14e11c63-e2ee-4b49-898f-b0cc4c61cb4e`; config at `config/notebook.json`.
- Canonical app is the Python CLI (`pyproject.toml`, `src/jarvise`). On Windows use `.venv\Scripts\jarvise` or `.venv\Scripts\python.exe -m pytest`; npm `ask-repo` is secondary.
- Binance TH has no testnet; paper trading is a local ledger filled from live market data.
