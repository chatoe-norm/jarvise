# Humbled Trader "Trading platforms" category: what it adds for Jarvise

**Research overlay — not trading doctrine.** Do not index this page into Qdrant `jarvise_doctrine`, do not silent-merge it into SQLite or the paper ledger, and do not use it to change the analyzer floors **0.55 / 0.70**, `same_side_hold`, the 1.5× ATR stop, or `JARVISE_LIVE_TRADING`. TradingView stays a research overlay. Jarvise numeric truth stays Binance and CoinGecko ingest.

Read **2026-10-18** from the category page [humbledtrader.com/category/trading-tools/trading-platforms](https://www.humbledtrader.com/category/trading-tools/trading-platforms/) and its three articles. The site is a US equity day and swing trading blog. None of the three articles covers crypto. Summaries below are of the articles as published; nothing was tested here. The author's earlier video is already in [2026-10-14-humbled-trader-ai-pnl.md](2026-10-14-humbled-trader-ai-pnl.md), and a different author's Claude + TradingView MCP walkthrough is in [2026-10-14-claude-tradingview-mcp.md](2026-10-14-claude-tradingview-mcp.md).

## The three articles

| Article | What it says | Limits the author states |
| --- | --- | --- |
| [How to Use Claude AI with TradingView MCP](https://www.humbledtrader.com/blog/how-to-use-claude-ai-with-tradingview-mpc-for-trading/) (2026-04-29) | Setup needs TradingView Desktop, Node.js 18+, Claude (Code or Desktop), and the open-source TradingView MCP bridge over local debug port 9222. Twelve use cases: scheduled scans, moving-average breaks, multi-timeframe checks, watchlists from plain English, pre-trade checklist on the chart, rule tests without Pine Script, trade journal with chart screenshots, replay-mode logging, Pine Script compile loop, streaming chart data as JSON. | Does not place trades. Unofficial TradingView interface that updates can break. Batch scans across symbols can fail and are slow (about 3 s per symbol). Gives context, not outcomes. |
| [How to Connect Claude to TradingView](https://www.humbledtrader.com/blog/connect-claude-to-tradingview-mcp/) (2026-05-16) | Step-by-step install with a health check, scanners scheduled on macOS (launchd), Pine Script backtests, optional Telegram alerts. Cost estimate about $33 a month manual and $113 a month automated (paid Claude plus a paid TradingView plan). | Strategy-results tool returned empty in her runs, so results were read from screenshots. Her backtests came from momentum names and lost on utilities and staples; a regime filter reduced results. Prompts go to Anthropic's API. Programmatic use may conflict with TradingView terms. At least a month of paper trading before live. |
| [Interactive Brokers IBKR Desktop tutorial](https://www.humbledtrader.com/blog/new-interactive-brokers-ibkr-desktop-tutorial-2026-full-guide/) (2026-02-01) | A tour of the IBKR Desktop GUI: TradingView-style charts, linked windows, order entry, options chain, screener. | Covers stocks and options only. No API, fee, or minimum details. No Level 2 depth or time and sales. |

## Fit with Jarvise rules

| Item | Verdict | Why |
| --- | --- | --- |
| TradingView MCP as a chart reading aid | Allowed as a desktop research tool for the owner | Already a research overlay in `AGENTS.md`. Needs TradingView Desktop and paid plans, depends on an unofficial debug port, and sends prompts to a cloud model. Not on the VPS, not a data source. |
| TradingView webhook or MCP as an order path | Rejected | `.cursor/rules/jarvise-trading-rules.mdc` forbids a TradingView webhook order path. The articles do not execute trades either. |
| Interactive Brokers | Rejected for now | The same rules file lists IBKR among venues not to add. The article gives no API facts to evaluate. Revisit only if the owner changes that rule, and then only through a read-only `VenueClient` first. |
| Scheduled scans, watchlists, momentum screens | Not needed | Jarvise ingest, universe, and the analyzer already cover this on stored candles. |
| Regime filter that reduced her results | Consistent with our result | The 2026-10-17 replay study found MTF and gates did not turn the signal positive ([2026-10-17-ledger-cause-and-replay.md](2026-10-17-ledger-cause-and-replay.md)). |

## What could help Jarvise (candidates only, none started)

1. **Closed-trade journal with a chart image.** Humbled Trader journals each trade with a chart screenshot. Jarvise already stores closed candles, draws SVG charts (`jarvise charts export`), and writes post-mortems. A paper-only journal page that pairs each closed trade with its chart, entry, stop, exit reason, and R would help the owner review. It must not write into `data/openclaw/exports/` or Qdrant (post-mortems from small samples are already being held back), and it changes no trading logic.
2. **Nothing else from this site.** The remaining content is TradingView tooling and a US broker GUI. Neither raises the measured edge of the signal, which is the open problem.

## Evidence note

These articles report no cost-adjusted, out-of-sample results for crypto. They are tooling guides. Treat them as context for how the owner can read charts, not as support for any rule change.
