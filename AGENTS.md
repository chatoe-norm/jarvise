## Learned User Preferences
- Trade worldwide (not locked to Binance TH); orient to positive, stable, trusted returns; paper-first, then live only when explicitly gated.
- Cover crypto plus stocks/ETFs; prefer slow, steady gains over aggressive returns.
- Be owner-protective: capital preservation, risk guardrails, kill-switch; live trading off by default.
- Treat the Gemini notebook as trading doctrine, not a live price or order-book feed.
- Use fetch/Firecrawl subagents for historical and current market data; combine that with notebook rules before any trade call.

## Learned Workspace Facts
- GitHub remote is `chatoe-norm/jarvise`; push via SSH host `github.com-chatoe-norm`. HTTPS as `normstudiox` is pull-only.
- Gemini Notebook MCP must use `nlm` profile `chatoe` (`chatoe@gmail.com`), not `default`/`normstudiox`. Alias `jarvise` = `14e11c63-e2ee-4b49-898f-b0cc4c61cb4e`; config at `config/notebook.json`.
- Canonical app is the Python CLI (`pyproject.toml`, `src/jarvise`). On Windows use `.venv\Scripts\jarvise` or `.venv\Scripts\python.exe -m pytest`; npm `ask-repo` is secondary.
- Paper trading is a local multi-asset ledger filled from live market data; venue testnets are optional and not required.
- Deploy target is Hostinger KVM 2 ID `1269762` (`srv1269762.hstgr.cloud`, Ubuntu 24.04, 2 vCPU, 8 GB RAM, 100 GB, Malaysia). SSH user `root`. Tailscale IP `100.93.110.48`. Control plane (n8n, OpenClaw, web) binds to Tailscale only, not the public IPv4.
- Stack lives at `/opt/jarvise` on that VPS. OpenClaw uses OpenRouter model `openrouter/openrouter/auto`.
- Doctrine RAG is Qdrant collection `jarvise_doctrine`. Obsidian is not a RAG store.
