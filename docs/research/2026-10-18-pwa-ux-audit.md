# PWA UX audit — Command Dashboard (2026-10-18)

Research note only. It does not change trading rules, and it is not a source for Qdrant `jarvise_doctrine` or the paper ledger.

Stage A records what the dashboard shows that is not live data, and how Stage B should regroup the pages. Stage B starts only after the owner approves this note.

## 1. Hardcoded vs dynamic

| Page / component | File:line | What is shown | Kind | Live source | Fix |
| --- | --- | --- | --- | --- | --- |
| Ops pipeline tab | `web/src/pages/OpsPage.tsx:136` | Badge `7 Jobs` | Hardcoded | `PIPELINE_JOBS` length in `web/src/components/PipelineControlCard.tsx:23` (7 entries) | Badge from that list length, or drop the badge |
| Ops universe tab | `web/src/pages/OpsPage.tsx:154` | Badge `Crypto` | Hardcoded | `/api/universe` `active_symbols` | Active-symbol count, or no badge |
| Ops models tab | `web/src/pages/OpsPage.tsx:172` | Badge `แผน` | Hardcoded | None. The live model is `status.auto_decide_config.model` | Remove the badge, or a chip from the live model |
| Ops View All tab | `web/src/pages/OpsPage.tsx:199` | Badge `Full` | Hardcoded | None | Remove it. The tab label is enough |
| Crypto universe header | `web/src/components/CryptoUniverseCard.tsx:292` | `data?.universe_id \|\| "paper_liquid"` | Hardcoded fallback | `GET /api/universe` `universe_id` (`JARVISE_PAPER_UNIVERSE`, else `paper_core`, in `src/jarvise_web/app.py` `active_universe_id`) | On failure show `—`. Never substitute `paper_liquid` |
| Universe preset buttons | `web/src/components/CryptoUniverseCard.tsx:328` and `:338` | `paper_core (2)`, `paper_liquid (4)` | Hardcoded counts | `src/jarvise_ingest/universe.py` `_PAPER_CORE_MEMBERS` (2) and `_PAPER_LIQUID_MEMBERS` (4). The VPS active list can differ from the preset | Counts from preset member lengths on the API. Do not use the live Ops list as the preset size |
| Ingest backfill default | `web/src/components/IngestHealthControlCard.tsx:18` | Initial timeframe `4h` | Hardcoded | `/api/universe` `timeframe` (`JARVISE_PAPER_TIMEFRAME`, else `4h`, `api_universe` in `src/jarvise_web/app.py`) | Default the select from that field |
| Ingest timeframe option | `web/src/components/IngestHealthControlCard.tsx:247` | Label `4h (Paper Trade Timeframe)` | Hardcoded | Same `timeframe` field | Mark the paper timeframe from the API. Keep `1h` and `1d` as the other stored series |
| Decisions filters | `web/src/pages/DecisionsPage.tsx:193` | `["ALL", "4h", "1h", "1d"]` | Hardcoded paper slot | Same `timeframe` field | Paper chip label from the API. `1h` and `1d` stay as named series |
| Home auto-decide blurb | `web/src/pages/HomePage.tsx:368` | “after 4h candidate enqueue” | Hardcoded | Same `timeframe` field. Model text on the same card already uses `auto.model` | Interpolate the paper timeframe |
| Home positions blurb | `web/src/pages/HomePage.tsx:128` | `1.5× ATR`, `0.5R`, `1.0R Trail` | Hardcoded | Proposed `status.rules` (below) | Render from `status.rules` |
| Home stop chip | `web/src/pages/HomePage.tsx:177` | `BE +20bps` | Hardcoded | `DEFAULT_FEE_BPS = 20.0` in `src/jarvise_paper/profit_lock.py:16` | `BE +{fee_bps}bps` from `status.rules` |
| Paper positions blurb | `web/src/pages/PaperPage.tsx:409` | `1.5× ATR`, `บังทุน 0.5R`, `Trailing 1.0R`, and “ทุก 30 วินาที” | Hardcoded | Rules from `status.rules`. The 30s poll is the page timer, not an API field | Rules from `status.rules`. Keep the 30s line only if the timer stays 30s |
| Paper stop chips | `web/src/pages/PaperPage.tsx:501` and `:509` | `บังทุน (BE +20bps)`, `จุดตัด 1.5× ATR` | Hardcoded | `status.rules` | Same chips, numbers from `status.rules` |
| Market context footnote | `web/src/components/MarketContextCard.tsx:748` | `จุด Stop 1.5× ATR` | Hardcoded | `ATR_STOP_MULT = 1.5` in `src/jarvise_analyze/engine.py` | `status.rules.atr_stop_mult`. Keep the sentence that Fear & Greed is not a signal |
| Close advice when R is missing | `web/src/lib/positionAdvice.ts:44`, `:60`, `:76` | Dollar cutoffs `uPnl >= 150`, `>= 80`, `> 20` pick levels 5, 4, and 3 | Hardcoded guess | Position fields `entry_price`, `initial_stop_price`, `qty`, `unrealized_pnl` | If R cannot be computed, show an `R unknown` state. Do not pick a level from dollars. Known-R levels stay |
| Pipeline job list | `web/src/components/PipelineControlCard.tsx:23-72` | Seven titles, schedules (“ทุก 15 นาที”, “ทุก 6 ชม.”, “ทุก 4 ชม.”, “ทุก 1 ชม.”), and “klines (1h, 4h, 1d)” | Hardcoded | Job *results* are already Redis keys on `status` (`ingest`, `paper`, `paper_auto`, …). Schedules are n8n files, not on the API | Keep the seven job keys. Replace the paper-timeframe mention with `/api/universe` `timeframe`. Leave cron strings as labels until a schedule field exists. Do not invent times |
| Pipeline risk-monitor copy | `web/src/components/PipelineControlCard.tsx:56` | “ตรวจสอบ Stop Loss 1R” | Hardcoded, and it drifts | Entry stop is `1.5× ATR`; the 1R figure is the trail milestone | Say entry stop and trail from `status.rules`, not a single “1R stop” |
| Model catalog | `web/src/services/modelRoutingService.ts:18-29` | Model ids and USD prices per million tokens | Hardcoded local catalog | Live auto-decide model is `status.auto_decide_config.model` (`src/jarvise_web/app.py:396`) | The Ops models card may show the live auto-decide model. The price matrix stays labeled as a local catalog, not as the running OpenClaw team |
| Paper loss-lesson replay line | `web/src/pages/PaperPage.tsx:396` | “รีเพลย์ 2026-10-14 … ติดลบทั้งช่วง” | Hardcoded historical fact | Research note, not a live metric | Keep it as a dated research sentence, or move it behind an info control. Do not present it as today’s EV |
| Paper floor sentence | `web/src/pages/PaperPage.tsx:44` | `พื้นความมั่นใจ 0.55/0.70` | Locked constants, written as text | `CONFIDENCE_THRESHOLD` / `NO_DOCTRINE_MIN_CONF` | Not a bug. Stage B may cite them only if `status` already exposes the same numbers. Do not add a control that edits them |
| Risk-cap form before load | `web/src/components/RiskCapsControlCard.tsx:13-22` | `DEFAULT_CAPS` (notional 2000, daily loss 100, open positions 2, bucket 10%, risk 1%) fills the inputs until `initialCaps` arrives | Hardcoded stand-in | `status.risk_caps` from `load_risk_caps()` | Show `—` and disable save until `status.risk_caps` has loaded. The comment at line 28 already says saving defaults would overwrite real caps |
| Risk-cap presets | `web/src/components/RiskCapsControlCard.tsx:65-79` | Preset `liquid` uses daily loss 150 and bucket 20 | Hardcoded | Not the live caps | Label presets as local starting points. Do not describe them as the VPS configuration |

`status` today (`status_payload` in `src/jarvise_web/app.py:368`) already carries kill-switch, job blobs, `auto_decide_config` (enabled, model, risk percents), `auto_ev_gate`, `risk_caps`, `risk_monitor`, ingest health, Qdrant point count, and Fear & Greed / Altseason / dominance as context. Those badges that already branch on `status` (Risk Halted/Enforced, Health OK/Alert, Auto-Decide AI ON/OFF, rules `N pts`) stay dynamic.

### Proposed `status.rules`

Read-only object on `status_payload()`, no behaviour change:

| Field | Source today |
| --- | --- |
| `atr_stop_mult` | `ATR_STOP_MULT` (`1.5`) in `src/jarvise_analyze/engine.py` |
| `breakeven_r` | `DEFAULT_BREAKEVEN_R` (`0.5`) in `src/jarvise_paper/profit_lock.py` |
| `fee_bps` | `DEFAULT_FEE_BPS` (`20.0`) in the same file |
| `trail_r` | The trail milestone is the literal `1.0` (`favorable >= risk` in `profit_lock.py`). Naming `trail_r = 1.0` next to the other defaults is a label for that milestone, not a stop change |

Paper timeframe does not need a new field. `GET /api/universe` already returns `timeframe`.

### Shadow veto

There is no Ops card for shadow veto. `tests/test_shadow_veto.py` asserts `shadow_veto` and `paper_shadow_` do not appear in the status payload. Auto-decide, ingest, and the analyzer do not read those tables. Stage B groups existing cards only. A shadow-veto chip needs a separate owner decision and a read-only field; this pass does not add one.

## 2. UI audit

### Color tokens

`web/src/index.css` `@theme` already defines `--color-background`, `--color-foreground`, `--color-card`, `--color-muted`, `--color-border`, `--color-accent`, `--color-ok`, `--color-danger`, `--color-sidebar`, `--color-ring`.

Missing tokens for the state chip: `--color-warn` and `--color-info`.

Recount on 2026-10-18, `web/src`, excluding `*.test.ts`: **156** `#` hex literals. **13** of those are the theme block in `index.css`. **143** are inline. The largest piles are `web/src/lib/positionAdvice.ts` (41), `CryptoUniverseCard.tsx` (20), and `Charts.tsx` (18). Surfaces that repeat outside the theme include `#161d27`, `#11161f`, `#0d121a`, `#0c1017`, `#101722`, and `#0d131f`. Tailwind classes `emerald-*`, `rose-*`, `amber-*`, `sky-*`, and `violet-*` bypass `--color-ok` / `--color-danger` as well. Stage B moves state colors onto the tokens. Chart series colors in `Charts.tsx` can stay local if a token cannot name a series.

### Wide tables

These force sideways scrolling on a phone:

| Surface | Class |
| --- | --- |
| Home open positions | `web/src/pages/HomePage.tsx:133` `min-w-[560px]` |
| Approval queue | `web/src/components/ApprovalQueue.tsx:186` `min-w-[620px]` |
| Exchange balances | `web/src/pages/ExchangePage.tsx:479` `min-w-[500px]` |
| Decisions | `web/src/pages/DecisionsPage.tsx:237` `md:min-w-[760px]` |

Stage B: below the `sm` breakpoint (640px), render each as a stacked card (symbol, side chip, key numbers, action). From `sm` up, keep the table. No horizontal page scroll at 375, 768, 1024, or 1440 px.

### Tap targets

`web/src/components/ui/button.tsx:21` size `sm` is `h-11` (44px) and then `sm:h-8` (32px) from 640px up. The same `h-11 sm:h-8` or `sm:h-7` pattern is copied onto Ops, Exchange, universe, risk caps, pipeline, ingest, and model buttons. A tablet (pointer coarse, width ≥ 640px) gets the 32px control.

Tailwind in `web/package.json` is `^4.1.7`, which has the `pointer-coarse:` variant. Stage B: coarse pointers keep at least 44px (`pointer-coarse:min-h-11`), including tablets. A fine pointer may stay compact. Default button size is `h-9` (36px) and should follow the same rule when the control is a primary action.

### Hover-only explanations

Move these `title` strings behind an info control that opens on tap (no hover-only content):

- Close-advice tooltip on Home (`web/src/pages/HomePage.tsx:223`) and Paper (`web/src/pages/PaperPage.tsx:576`)
- Universe sync and preset hints (`web/src/components/CryptoUniverseCard.tsx:307`, `:326`, `:336`)
- Exchange checklist titles (`web/src/pages/ExchangePage.tsx:411` and the following items)
- Decisions reason cell (`web/src/pages/DecisionsPage.tsx:403`)

Market context detail is already a tap target: bottom sheet below `sm`, centered dialog with blur from `sm` up (`web/src/components/MarketContextCard.tsx:416`). Keep that split.

Long sentences that Stage B should turn into chips, with the sentence behind the info control: Home and Paper position blurbs, the Market context footnote, pipeline job descriptions, and the Exchange live-enable steps. Safety meaning stays available; it just stops being the default line.

## 3. Information architecture

Current Home order (`web/src/pages/HomePage.tsx:459-497`): Approval queue → Last Auto-Decide when the queue is empty → KPI strip (equity, cash, expected value, open positions) → open positions → Market context and news side by side.

Stage B order:

- **Home (act now):** Approval queue, with Last Auto-Decide kept in this block when the queue is empty → open positions → KPI strip → Market context + news. Equity curve, win/loss chart, and the EV-gate card stay on Paper. Fear & Greed stays in Market context and is not a signal. The queue-jump button stays.
- **Paper (performance):** KPI → equity curve → win/loss → EV gate and loss lesson → ledger. That is already the order in `web/src/pages/PaperPage.tsx` (KPI at 257, equity at 273, win/loss at 282, auto-decide quality at 302, loss lesson at 372, open positions, then the closed-trade ledger at 612). Stage B does not move those charts to Home. Open positions may stay between the lesson and the ledger.
- **Decisions and Exchange:** keep the current order.
- **Ops:** one tab strip. `#hash` deep links and the mobile default `all` from `web/src/lib/opsTabs.ts` stay (PR #203). View All renders four groups, in this order:
  1. **Safety** — kill switch, risk monitor, risk caps (today the `risk` tab).
  2. **Automation** — auto-decide, EV-gate latch, pipeline/jobs. Shadow veto stays off this page until a separate read API is approved (see above).
  3. **Data** — ingest health, universe.
  4. **Intelligence** — trading-rules RAG, models, observability.

Owner copy stays **กฎการเทรด**. Exchange dust / earn / unpriced labels stay. No raw JSON or `<pre>` as the primary surface (pipeline already has a `<pre>` log at `PipelineControlCard.tsx`; Stage B keeps that behind an explicit “log” control).

## 4. Design rules for Stage B

- One state chip: lucide-react icon + token color + short label, for `ok`, `warn`, `danger`, `stale`, and `off`.
- Number first, label second.
- Freshness uses `relativeAge` (`web/src/lib/utils.ts`). Past the stale threshold the chip uses the stale color. The threshold is stated next to the chip, not buried in a paragraph.
- Empty, loading, and error: `—` in muted. Never a fake universe id, a fake timeframe, or a dollar guess for R.
- Lucide icons only.
- Desktop modals stay centered with backdrop blur. Below `sm`, a bottom sheet is allowed, as Market context already does.
- Coarse pointers get at least 44px hit targets.

## 5. Stage B sequence (not started)

After the owner approves this note, on this branch or a new one:

1. Shared tokens (`warn`, `info`), the state chip, and the coarse-pointer rule. One commit.
2. Home, Ops, Paper, Decisions, Exchange. One commit per page.
3. Below `sm`, wide tables become stacked cards. Tablet and desktop keep tables.
4. Wire every row in section 1. Add `status.rules` in `src/jarvise_web/app.py` from the constants above, with a test. No change to analyzer floors, auto-decide, the EV gate, stops, profit lock, the kill switch, or universe membership.
5. Unit tests for the chip and for `rules`. Existing tests stay green.
6. Checks: `cd web; npm run lint; npm test; npm run build`. If `app.py` changes: ruff, ruff format, mypy, pytest.
7. Open a PR, Sonnet 5.5 review (no number that is not from the API; safety copy still reachable), then `fdpy`.

Hard rules for that pass: Fear & Greed and Altseason never become advice; live stays off; VPS `.env` stays untouched; do not add Playwright.
