# Dashboard menu audit — 2026-10-18

Research note only. It does not change trading rules, and it is not a source for Qdrant `jarvise_doctrine` or the paper ledger.

Stage A checks whether each Command Dashboard menu states a fact the running system does not do. Stage B starts only after Claude reviews this note. This pass does not edit trading code, the VPS `.env`, or the analyzer.

The walk used local Vite at `http://localhost:5173` (proxy to `127.0.0.1:8080`) at 1440px and 375px, plus a read-only VPS `status_payload()` and `jarvise universe show --json` on 2026-10-18. The local API and the VPS are different books. A local number that matches the local API is not a production bug. Drift is called out in the fact list.

Counts below: **5 high, 6 medium, 3 low**.

## Verified system facts

| Fact | Result | Evidence |
| --- | --- | --- |
| Live trading off | yes | VPS `live: false`, `paper_only: true`. Shell shows `PAPER SHIELD ACTIVE` / `PAPER ONLY`. |
| Auto-decide off | yes | VPS `auto: false`, model `anthropic/claude-sonnet-5.5`. Ops tab badge `AI OFF`. |
| EV gate latched, n=10, EV about −0.65 | yes on the VPS | VPS `gate_latched: true`, `gate_n: 10`, `gate_ev: -0.64795314`. The local API is a different book: `latched: false`, `n: 0`. |
| Quiet mode on | yes on the VPS | VPS `quiet: true`. Local `notify_quiet: false` (no Redis). |
| Kill switch clear | yes on the VPS | VPS `kill: false`, `ks_known: true`. Local `kill_switch: true` with `known: false` (`ModuleNotFoundError`). See Home row below. |
| Universe `paper_liquid`, 7 symbols, 4h | yes | VPS `universe show`: `ADAUSDT BNBUSDT BTCUSDT DOGEUSDT ETHUSDT SOLUSDT XRPUSDT`, timeframe `4h`. |
| MTF off | **no — it is on** | Jobs container `JARVISE_ANALYZE_MTF=true`, `JARVISE_ANALYZE_HTF=1d`. `paper` calls `analyze_mtf` when that flag is on (`src/jarvise_paper/cli.py`). Decisions theses include `HTF range`. Do not flip this in Stage B; it is an owner decision. |
| Floors 0.55 / 0.70 | yes | Decisions header. Paper loss-lesson line. |
| Stop 1.5× ATR, profit lock 0.5R / +20 bps / trail 1R | yes | VPS `rules`: `atr_stop_mult 1.5`, `breakeven_r 0.5`, `breakeven_buffer_bps 20`, `trail_after_r 1`, `trail_distance_r 1`. Home and Paper chips read those fields. |
| 1% is a ceiling, not the size used | the engine is yes; one card is easy to misread | `max_risk_per_trade_pct` is `1.0` on the VPS. Order size is `size_pct_equity` (`BASE_SIZE_PCT = 1.5` times confidence in `src/jarvise_analyze/engine.py`). See Auto-decide row. |
| Writers | yes, as coded | Binance klines, book, public futures derivatives, CoinGecko global, Alternative.me, altseason fallback, GDELT/RSS, OpenClaw digests. TradingView, token audit, on-chain flows, and VIX/SPX are not ingest writers. |
| OpenClaw team | yes, as last recorded | Agents `chief` / `researcher` / `writer` / `reviewer` / `ingest`. Eight plugins kept (`docs/handoff/2026-10-17-openclaw-prune.md`). Not re-read from the VPS config in this pass. |
| Qdrant about 83 | yes on the VPS | VPS `qdrant_points: 83`, `status: green`. Local card shows 0 / red. |
| Shadow veto off the dashboard | yes | No shadow card on Home, Paper, Decisions, Exchange, or Ops. |
| Model routing unwired | yes | Models tab: `Model Routing Plan ยังไม่เชื่อมต่อการทำงาน`. Live auto-decide model stays `anthropic/claude-sonnet-5.5`. |
| Weekly report Monday 08:00 Asia/Bangkok | yes | `infra/n8n/workflows/jarvise-weekly-report.json` cron `0 8 * * 1`. |
| Other n8n schedules | yes in the workflow files | Ingest 15 min, ingest health 1 h, paper run 4 h, paper expire 1 h, pending digest 1 h, risk monitor 15 min, RAG 6 h, news 07:00/19:00, news reaction 07:30/19:30, shadow veto `15 0,4,8,12,16,20`. The pipeline card lists only seven of these. |

## Home

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Paper shield and live banner | `PAPER SHIELD ACTIVE`, `PAPER ONLY — No order placement` | yes | VPS `live: false`. `web/src/components/layout/AppShell.tsx` | — | — |
| Kill-switch badge | `Kill Switch: ENGAGED` whenever `status.kill_switch` is true | misleading | Local status was `kill_switch: true` and `kill_switch_state.known: false`. The Ops card then says `ไม่ทราบสถานะ`. The badge uses only the boolean (`AppShell.tsx` around the kill badge). VPS today is `known: true` and clear, so the VPS badge matches. | high | If `known` is false, say unknown. Do not say ENGAGED. |
| Approval queue and positions | Queue empty. Three local shorts with stops and R. | yes for the API it calls | Local `/api/paper`. VPS book differs (do not treat the three shorts as the VPS book). | — | — |
| Rule chips | `Stop 1.5× ATR`, `BE 0.5R`, `Trail 1R` | yes | `status.rules` | — | — |
| Market context | Fear & Greed 63 and Altseason 84, labeled not a signal. Detail dialog opens at 375px and stays context-only. | yes | `MarketContextCard.tsx` | — | — |
| News | OpenClaw digest is context, not a signal. Empty local digest. | yes | `web/src/pages/HomePage.tsx` | — | — |
| Shell label | `Hostinger KVM · :8080` | hardcoded | `AppShell.tsx` | low | Leave it, or drop the port. It is a label, not a control. |

## Paper

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Ledger charts and EV table | Equity, curve, win/loss, and `auto_claude` n=10 EV −0.65 on the local book | yes for that book | `/api/paper/metrics` | — | — |
| EV gate line | `เกต EV ยังไม่เปิด` on the local book | yes locally; the VPS is latched | `evGateOwnerCopy` in `web/src/lib/copy.ts`. VPS latch is true. The function already has a latched sentence. | — | — |
| Loss lesson | `ปิดเอง 0 ไม้` inside the losing-trade sentence, next to a table of four `ปิดเอง` wins | misleading | `loss_lesson` counts only `pnl_usd < 0` (`src/jarvise_paper/metrics.py` around the `by_exit` loop). The four winners are `close_manual`. | medium | Say the exit counts are losing closes only, or count every close and label wins separately. |
| Floors sentence | `0.55/0.70 จึงยังอยู่` | yes | Locked copy. Do not change the floors. | — | — |
| Position chips | 1.5× ATR, 0.5R, trail 1R from `status.rules` | yes | Paper page chips | — | — |

## Decisions

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Floor header | `0.55 = FLAT gate · 0.70 = auto-approve without trading rules` | yes | Decisions page | — | — |
| Same-side note | ถือไม้เดิม means an existing same-side position | yes | Matches `same_side_hold` | — | — |
| HTF in the thesis | Example: `HTF range; no directional LTF.` | yes | MTF is on (fact list). This is engine output, not a decorative claim. | — | Do not strip it, and do not turn MTF off from this note. |
| Size column | `Size%` shows `size_pct_equity` (for example 1.13) | yes | Notional percent, not the 1% risk ceiling | — | — |

## Exchange

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Read-only balances | No keys locally; page says paper-only and does not send orders | yes | Exchange page header. A missing key or an IP whitelist miss is an owner action. | — | — |
| Live flag | `Gated (false)` | yes | VPS `live: false` | — | — |
| Host IP | `76.13.218.48` as the whitelist address | yes | Matches the deploy host. It is a literal in `ExchangePage.tsx`. | — | — |
| EV checklist | Badge `ผ่านเกณฑ์ (N=0)` when `blocked` is false | wrong fact | `ExchangePage.tsx` checklist uses `gate.blocked` only. Local gate is `n: 0`, `blocked: false`, `ev: null`, while the paper table shows auto EV −0.65. A negative EV that is not `blocked` still renders as passed. | high | Pass only when the sample exists, EV is ≥ 0, and the latch is clear. `n: 0` is `ยังไม่มีข้อมูล`. |
| Venue stop card | Every matched Binance BUY gets an exchange stop-limit immediately | misleading | `ExchangePage.tsx` guardrails tab, present tense. `place_spot_stop_loss_limit` exists on the live submit path (`src/jarvise_trade/submit.py`). Live is off, so no venue stop is being sent. Paper stops stay in the ledger. | high | Say this runs only after live is on and a live fill exists. |
| Sample `.env` | The guide block sets `JARVISE_LIVE_TRADING=true` | misleading | Exchange setup tab | low | Label it as a template. Do not look like the current file. |
| Kill-switch flatten card | Says engage cancels pending approvals and market-sells | yes for the live flatten path | Flatten code is dormant while live is off. Worth a “when live is on” clause in the same pass as the stop card. | — | — |

## Ops — Risk

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Kill-switch card | `ไม่ทราบสถานะ` when Redis cannot be read, and it says approvals stay blocked | yes | `OpsPage.tsx` `killMode` when `known === false` | — | Keep this. Fix the shell badge so it matches. |
| Risk caps | Ceiling `1.0%` on `max_risk_per_trade_pct`, labeled as a ceiling | yes | `RiskCapsControlCard.tsx`. VPS cap is `1.0`. | — | — |
| Risk monitor | 15-minute schedule. Stale window is an API field (35 minutes). | yes | Pipeline copy and `status.freshness.risk_monitor_stale_after_min` | — | — |

## Ops — Pipeline

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Seven job rows | Ingest 15 min, RAG 6 h, paper 4 h, expire 1 h, risk 15 min, health 1 h, auto-decide after paper run | yes for those seven | `PIPELINE_JOBS` in `PipelineControlCard.tsx`. Crons match the n8n files. | — | — |
| Jobs that are not listed | No row for the pending digest, news ingest, news reaction, shadow veto, or the Monday weekly report | misleading | Those workflows exist under `infra/n8n/workflows/`. | medium | Add read-only rows, or say the list is the jobs API only. |
| Paper-run blurb | `Pullback/Breakout` | stale | Same file, paper-run description. The analyzer is the EMA stack plus the pullback note, not a breakout system. | low | Say signal scan / enqueue. |

## Ops — Health

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Ingest health card | Asks for a check when the local blob is empty | yes | Local Redis is down, so job blobs are missing. Not a VPS outage. | — | — |

## Ops — Universe

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Active list | Loaded from `/api/universe` | yes | VPS list is the seven symbols above. Preset sizes are not that list (`paper_liquid` preset is four names; the VPS active list is seven). | — | Do not add or remove symbols from this note. |

## Ops — Auto-decide

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Off state | Badge `AI OFF`. The off card is the selected one while auto-decide is off. | yes | VPS `auto: false` | — | — |
| On card status line | `สถานะ: ทำงานอัตโนมัติเต็มรูปแบบ (Fully Autonomous)` stays on the On card while the system is off | misleading | `PaperAutoDecideCard.tsx` | medium | Show that line only when the mode is on. The mode still does not place live orders. |
| Risk percent field | `เพดานความเสี่ยงเงินทุนต่อไม้` with value `1.0` | misleading | Same card. `1.0` is `max_risk_per_trade_pct`, the ceiling. Fills use `size_pct_equity`. | medium | Call it the risk-at-stop ceiling. Do not imply each fill risks 1% of equity. |
| Model name | Claude Sonnet 5.5 | yes | VPS model `anthropic/claude-sonnet-5.5` | — | — |

## Ops — Models

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Not-wired label | `ยังไม่เชื่อมต่อการทำงาน` and “ยังไม่มีงานใดในระบบเรียกใช้ตารางนี้” | yes | `ModelRoutingCard.tsx`. Live model is the auto-decide config, not this catalog. | — | Keep the label. |
| Role cards | `Token Audit Veto`, `Pullback 6 ข้อ`, TradingView / CMC / CoinGlass V4, `JSON Signal`, plus models that are not the OpenClaw team | wrong fact | `web/src/repositories/modelRoutingRepository.ts` `coreTask` strings (doctrine judge, heartbeat, quant). OpenClaw roles are `chief` / `researcher` / `writer` / `reviewer` / `ingest`. | medium | Replace the tasks with those roles, or hide the task text behind the not-wired label. Do not change the live auto-decide model. |

## Ops — Trading rules

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Point count | Local card `0` / red. VPS status is 83 / green. | yes for each API | Local Qdrant is down. VPS `qdrant_points: 83`. | — | — |
| OpenClaw sync line | `พร้อมซิงค์กฎล่าสุดจาก Gemini NotebookLM และ OpenClaw`, and `OpenClaw Trading Notes` as a linked source | misleading | `QdrantTradingRulesCard.tsx`. The 2026-10-17 audit found no post-mortem notes from that loop in Qdrant. The line reads as if those lessons are already there and improve the book. | medium | Say NotebookLM is the doctrine source. Say OpenClaw exports sync only when a note exists. Do not claim a win-rate effect. |

## Ops — Observability

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Gauges | Paper equity and open positions from the API | yes | Observability card. Local figures follow the local ledger. | — | — |
| Shadow veto | Not shown | yes | Stays off the dashboard until a separate read API is approved. | — | — |

## Ops — View All

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Mobile default | `/ops` with no hash at 375px opens View All, groups Safety, Automation, Data, Intelligence | yes | Confirmed this pass. Desktop still opens Risk. | — | — |
| Group contents | Same cards as the tabs above | yes | No extra claim | — | — |

## Outside the menus — weekly report

Not a dashboard card. Seeded here because a Monday Telegram line can state a false OK.

| Item | What is shown | Correct? | Evidence | Severity | Fix |
| --- | --- | --- | --- | --- | --- |
| Missing database | Equity starts at `STARTING_PAPER_EQUITY` (10,000) and zero counts, then the report can end `[OK]` / `ไม่ต้องทำอะไร` | wrong fact | `collect_weekly_snapshot` in `src/jarvise_notify/weekly.py` sets those defaults before `path.exists()`. | high | A failed section is `ต้องทำ`. Do not end with `ไม่ต้องทำอะไร` after a failed section. |
| Build failure | Exception path publishes `ok: false` and `text: ""` and does not send a short failure line | wrong fact | `run_weekly_report` in `src/jarvise/jobs.py` | high | Send one short failure line. Keep Telegram send errors in the JSON. |

Stage B for this row is the fail-closed weekly fix. It is not part of this pull request.

## What Stage B should not touch

Live stays off. VPS `.env` stays untouched. Do not change the analyzer formula, floors 0.55/0.70, the 1.5× ATR stop, `flat_exit`, `same_side_hold`, profit-lock math, the kill-switch engage rules, EV-gate latch rules, universe membership, or `JARVISE_ANALYZE_MTF`. The MTF flag is on today; turning it off is an owner decision, not a copy fix.
