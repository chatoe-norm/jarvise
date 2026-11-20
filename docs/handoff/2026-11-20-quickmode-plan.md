# Quick mode: refreshed plan (M0)

Date: 2026-11-20. Base: `main` @ `a0cc39c` ([#370](https://github.com/chatoe-norm/jarvise/pull/370)).
Supersedes the untracked 2026-11-14 handoff (`docs/handoff/2026-11-14-quickmode-plan.md`, written against `main` @ `157f4f8`).
Reply to: "jarvise: Quick mode (manual scalping assist) — design + PR plan" (Grok and the owner), updated after QPR1–QPR2a shipped and after owner decisions on 2026-11-20.

Status: **QPR1, QPR1b and QPR2a are on `main`.** This document is the M0 refresh: commit it into the repo, then implement close (QPR4 / M1) and open (QPR5 / M2). Nothing in M1–M4 ships without owner OK. Cursor prompts are written against the code as it stands at the start of each milestone.

The owner answered the original decisions on 2026-11-14 (section 4, including 4.1) and reconfirmed or tightened several on 2026-11-20 (section 4.2). Decision 1 still differs from Claude's original recommendation; sections 2.1 and 3 reflect the owner's choice.

**Proposed repo path for M0:** `docs/superpowers/specs/2026-11-20-quickmode-design.md`. That is the convention for durable assist designs (`docs/superpowers/specs/2026-11-02-assist-execute-design.md` and neighbours). `docs/handoff/` is for time-bounded handovers and is not where this should live once committed. `docs/superpowers/plans/` is for implementation checklists; a short plan file may be added later, but this document is the design source of truth.

## Changelog versus the 14 November plan

- **Status, not a gated start.** QPR1 [#331](https://github.com/chatoe-norm/jarvise/pull/331), QPR1b [#332](https://github.com/chatoe-norm/jarvise/pull/332) and QPR2a [#334](https://github.com/chatoe-norm/jarvise/pull/334) (plus leftovers [#335](https://github.com/chatoe-norm/jarvise/pull/335) and the week_loss-only block [#336](https://github.com/chatoe-norm/jarvise/pull/336)) are merged. The "start QPR1 only after move-stop PR2 deploys" gate is removed. [#346](https://github.com/chatoe-norm/jarvise/pull/346) added the ⚡ Quick reply-keyboard button.
- **Portfolio cap is dynamic.** [#353](https://github.com/chatoe-norm/jarvise/pull/353) / [#360](https://github.com/chatoe-norm/jarvise/pull/360) replaced the fixed 4% cap with `portfolio_risk_cap_pct()` = 2× the live assist `risk_pct`, ceiling 6%. Quick's combined gate already uses that same function. Worst-case open risk is 2× main % + the quick cap, not 4% + the quick cap. Section 1.3 and the settings copy are updated to match.
- **Weekly pause wording.** The 14 Nov plan said a "weekly 6% loss pause". On `main` the rule is PR [#333](https://github.com/chatoe-norm/jarvise/pull/333): pause when the week's trading net (realized + commission + funding, including quick) is worse than **4 × the assist `risk_pct`** of the reconstructed week-start balance. Owner decision 3 is unchanged: this pause still counts quick trades, and [#336](https://github.com/chatoe-norm/jarvise/pull/336) is what actually blocks Quick.
- **§2.8 no longer contradicts §4.** Quick cap stays default 1%, range 0.5–2%, confirm above 1% (reconfirmed 20 Nov). The stale "0.5% default, 0.25–1.0%, confirm above 0.75%" figures are removed from §2.8. Those numbers were a pre-decision proposal for *per-trade* risk, not the cap; what QPR1 actually shipped for per-trade risk and for locks is stated factually in §2.8 and §4.2.
- **20 Nov policy on Quick-only daily / streak locks.** The owner chose **no Quick daily stop and no 3-loss lock beyond existing rules**. QPR1 already shipped both (see §2.8). This plan records the gap and requires a small settings follow-up before or with M1.
- **Fees and measurement.** Expectancy after 20–30 closed quick trades is computed from **real fill fees + funding**, not the house model. The sizing card uses the 20 Nov fee model (6 bps entry, 8–10 bps stop/market exit, funding when a hold would cross a funding time), with live `commissionRate` cached daily and a conservative fallback. An append-only journal is added (section 2.3).
- **Close UX.** After a close, leftover reduceOnly / `closePosition` orders — including owner-placed ones — are still cancelled. The card must list those orders itemised before the tap (section 2.5).
- **Milestones replace the old "revised PR plan" table.** M0 docs; M1a QPR4 close (one confirm, Long/Short buttons, lock removal); M1b journal + fees; M2 QPR5 open behind `QUICK_ORDERS_ENABLED=false`; M3 measurement; M4 QPR2b `quickfeed` only after owner go/no-go. QPR3 stays dropped. A test matrix, read-only post-deploy checks and UX notes are folded in. Hard rules are unchanged.
- **Stale line numbers.** Router, kill-switch and listen references are updated to `a0cc39c` (`listen.py` uses a `startswith` chain, not a single tuple at the old l.112; `kill_switch_engaged()` is still `jobs.py` l.112; the Telegram listener starts at `jobs.py` l.1198).

## 1. Answers to the three questions

### 1.1 Conflict with the move-stop work, and ordering

The move-stop queue is already on `main` (PR1 [#325](https://github.com/chatoe-norm/jarvise/pull/325), PR-B [#328](https://github.com/chatoe-norm/jarvise/pull/328), PR3 [#329](https://github.com/chatoe-norm/jarvise/pull/329), and PR2). Quick still shares `listen.py`, `watch.py`, `messages.py`, `keyboard.py`, `journal.py` and the button gate in `execute.py`. Further Quick PRs must keep main move-stop and entry behaviour unchanged when no quick tag exists.

Any new callback prefix (for example a close prefix on the quick card) must be added to the `process_update` `startswith` chain in `listen.py` (today: `qs:`, `qr:`, `ec:` / `ey:` / `en:` / `ms:` / `mn:` / `xx:`, partner and journal prefixes). A router test must go through `process_update`. PR1 existed because `mn:` was missed this way.

The BE-timing research (Prompt 4) remains independent and is not on `main`.

### 1.2 Where the WebSocket client lives

Unchanged in intent; this is **M4**, and only after the measurement go/no-go (section 3).

- There is no long-running "watch service". The watch is a stateless job: n8n posts `/jobs/assist-watch` every 5 minutes to the `jobs` container. A WebSocket needs a long-running process.
- Put it in a **new compose service** (for example `quickfeed`), with `restart: unless-stopped` and a memory limit. Never run it as a thread inside `jobs`. `jobs` also runs the main watch and the Telegram listener thread (`jobs.py` l.1198 at `a0cc39c`), so a leak or crash there would hit the main system.
- **Do not use the main Redis.** The kill switch lives in Redis and fails closed: `kill_switch_engaged()` (`jobs.py` l.112) treats a connection error as engaged. If the feed fills Redis memory or causes connection errors, the main jobs stop. If `quickfeed` is given a Redis URL at all, it must be a **separate** instance, and process start must **refuse** if that URL matches the main Redis URL.
- Have `quickfeed` write the latest mark and 1m/5m bars to its own SQLite file (for example `data/quick/quickfeed.db`, WAL, one row per symbol). `jobs` reads that file read-only and treats data older than 5 s as missing.
- If the feed dies, quick cards show "ไม่มีราคาสด" and give no advice or buttons. The main system is unaffected.

### 1.3 Portfolio cap (dynamic) and the separate quick cap

On `main` @ `a0cc39c`:

- `PORTFOLIO_RISK_MULTIPLIER = 2`, `PORTFOLIO_RISK_CAP_CEILING_PCT = 6`, `MAX_OPEN_IDEAS = 2` (`sizing.py`).
- `portfolio_risk_cap_pct(risk_pct)` returns `min(2 × risk_pct, 6)`. It is read from the live assist `risk_pct` at the 4h scan, the 08:20 batch, `ec:` preview, `ey:` confirm, and Quick's combined gate (`quick_card.py`).
- Main sizing still leaves quick-tagged positions out of `build_existing_ideas` (QPR1b).
- Quick also has its own cap (`assist_quick_settings.cap_pct`, default 1%, allowed 0.5 / 1 / 1.5 / 2, confirm above 1%). A new quick registration is refused when `quick_open + new > balance × cap_pct / 100` (`quick_cap`) **or** when `main_open + quick_open + new > balance × portfolio_risk_cap_pct(main risk_pct) / 100` (`portfolio_cap`).

**Owner decision remains B**, now against the dynamic cap:

| Option | Main affected? | Worst-case total open risk |
| --- | --- | --- |
| A. Shared portfolio cap only | Yes: open quick risk can shrink or block a main signal. This contradicts "main completely unaffected". | 2× main % (ceiling 6%) |
| B. **Chosen.** Main sizing unchanged (it does not count quick). Quick has its own cap, and may open only if main open + quick open + new ≤ the **same** dynamic portfolio cap. | No | 2× main % + the quick cap |
| C. Fully separate (dynamic main cap + quick cap, no combined gate) | No | 2× main % + the quick cap at any time |

- Quick cap: default **1%**, allowed **0.5–2%**, confirm above 1% (owner reconfirmed 2026-11-20).
- Worst-case total open risk = **2× current main risk % + the quick cap**, then the 6% ceiling applies only to the main side of that sum. At main 3% and a 2% quick cap that is **8%**. The Quick settings screen already shows this as `ความเสี่ยงรวมสูงสุด {main_cap + cap}% (หลัก {main_cap}% + quick {cap}%)`.
- Worked cap matrix (main `risk_pct` → portfolio cap %): 0.5 → 1; 1 → 2; 2 → 4; 3 → 6; 3.01 → 6 (ceiling). Tests in M0's matrix must cover these values against the combined gate.

## 2. Audit findings that change the plan

### 2.1 One-way mode: quick and main cannot share a symbol — shipped in QPR1 / QPR1b

- The account must be in one-way mode. `execute.py` refuses hedge mode (`MSG_ONE_WAY`). Binance holds one net position per symbol.
- A quick trade on a symbol where main holds a position merges into it.
- **Owner decision: mutual exclusion.** Quick may trade any symbol, including the main 7, as long as the main system holds no position on it. This is implemented:
  1. **Tag by registration.** `qr:` / "⚡ เริ่มไม้ quick" registers symbol, side, planned entry and stop for 10 minutes (`REG_TTL_MS`). The next position on that symbol and side in the window is tagged quick. A hand-opened position without registration stays main.
  2. **The card refuses a symbol where main holds a position**, and also refuses when a pending non-reduce order or in-flight exec exists, so a quick fill cannot merge into main.
  3. **Main watch skips quick-tagged positions.** Quick advice will come from quick code (stop advice itself waits for M4).
  4. **Main sizing leaves quick-tagged positions out** of `build_existing_ideas`.
  5. **A main signal on a symbol with an open quick position** is still sent and tracked; the entry button is replaced by "มีไม้ quick อยู่ — ปิดก่อนถึงเข้าได้" (`MSG_QUICK_OPEN`). Today that button still uses the `ec:` callback. **M1 must point it at the quick close card instead** (UX notes).

The main universe stays locked. Quick does not add symbols to it. Tests that main behaviour is unchanged when no quick tag exists stay mandatory on every later PR.

### 2.2 The journal does not filter by universe — tagging shipped; measurement journal is still thin

- `journal.py` and `real_trades.py` have no `in_universe` filter. QPR1's tag (`assist_quick_trade`) is what keeps quick closes out of main reports, the weekly report, P5 decision stats, real-vs-virtual pairs and the day pause, via `quick_income_predicate`. Week-loss still counts quick.
- The main day-loss pause counts only bot-entered trades. Manual quick trades are excluded from that pause today. Stage C (M2) bot-entered quick entries must stay excluded from the *main* day pause by mode, and must **not** grow a new Quick-only daily stop (section 4.2).
- `assist_quick_trade` already stores entry/exit, qty, `realized_pnl_usdt`, `commission_usdt`, `funding_usdt`, `net_usdt`, `risk_usdt`, `net_r`, `fee_r`. It is updated in place from `open` to `closed`. That is not enough for the 20 Nov measurement rule (section 2.3).

### 2.3 Fees, the sizing card, and the append-only journal

The house model on the QPR2a card is still a flat **6 bps per side** (`QUICK_FEE_BPS_PER_SIDE`, both entry and exit). `commissionRate` is not called anywhere on `main`. Owner decision 20 Nov replaces that.

**Sizing-card model (preview only):**

- Entries: **6 bps/side**.
- Stop or market exits: **8–10 bps/side**. Until the owner picks a single number, the card uses the conservative end: **10 bps**.
- If the next funding time falls within the 10-minute registration window plus the expected hold (default: assume the hold may cross it whenever `nextFundingTime` is under 30 minutes away) (`nextFundingTime` on the same `premiumIndex` payload the card already reads for the mark), the card shows that Bangkok time, the last funding rate, and includes that cost in the break-even line. If the next funding is not in play, the card says so and does not add it.
- Live `/fapi/v1/commissionRate`, cached daily. If the read fails or the cache is stale, fall back to the conservative house model above and say so on the card. Log the fee source (`live` / `cache` / `house`) per fill once orders exist; until then, log the source used for the card math.

Round-trip examples with 6 bps entry + 10 bps exit (0.16% , no funding) — the card must recompute when funding is added:

| Stop distance | Fees per round trip | Win at 1R target | Loss at stop | Break-even win rate (1R) | Break-even win rate (1.5R) |
| --- | --- | --- | --- | --- | --- |
| 0.30% | 0.53R | +0.47R | −1.53R | 77% | 61% |
| 0.60% | 0.27R | +0.73R | −1.27R | 63% | 51% |
| 1.00% | 0.16R | +0.84R | −1.16R | 58% | 46% |

The fee warning ("reward < 3× round-trip fees") stays. The card still shows the break-even win rate for the chosen stop and target.

**Measurement and journal (required before M3 is meaningful; schema can land in M1):**

- Expectancy after 20–30 **closed** quick trades is computed from **real fill commissions + funding**, never from the house model.
- Add an **append-only** quick journal (new table, or an event log that does not rewrite past rows). Each close (and each material fill) appends: symbol, side, entry, exit, size, risk %, R, real fees, funding, slippage versus the confirm/card price, hold time, close reason, setup tag, MAE, MFE, and the fee-source used. `assist_quick_trade` may keep a current-state row, but historical fee and R figures must remain reconstructible from the journal.

### 2.4 Build the data feed only after quick trades show an edge

Unchanged. The sizing card needs the mark only when the owner taps. One REST call (`/fapi/v1/premiumIndex`) is enough; no WebSocket in M1–M3. Real-time stop advice is M4, after 20–30 journaled closes show positive net expectancy on real fees + funding, and only after an explicit owner go/no-go.

### 2.5 The close button: reuse the exit path, then cancel leftovers including owner-placed orders (M1 / QPR4)

- Reuse `run_exit_sequence` (`execute.py`): a `kind="exit"` intent, `exit_confirm_keyboard` (`xx:` or a dedicated quick prefix that still routes through the same sequence), market, reduceOnly, whole position, up to 3 attempts. It refuses when qty or entry differ from the preview, and a second tap replays the result instead of sending again. Mark the intent preview `"source": "quick"` so the journal records a quick manual close.
- **Close needs exactly one confirm (owner, 2026-11-20).** Tapping ปิดไม้ on the quick card shows a confirm (`exit_confirm_keyboard` or a dedicated quick prefix) that lists the position and the leftover protective orders to be cancelled; the market close is sent only after that one confirm. No second confirm beyond that. M2 opening keeps its two-step confirm.
- Today's `cancel_jv_orders_for_symbol` **never touches non-`jv-` ids**. That is not enough for the owner decision. After a confirmed quick close, cancel **all leftover protective orders on that symbol**, including stops and TPs the owner placed by hand: reduceOnly or `closePosition` only; never a non-reduce entry order. Run the cancel immediately after the close fill is confirmed, not on the next watch tick. Mutual exclusion means no main position exists on that symbol.
- **Before the tap, the card lists those leftover protective orders itemised** (type, side, trigger/price, qty or `closePosition`, and whether the id is `jv-` or owner-placed). The post-close result is also itemised (UX notes).
- Cancel must be **idempotent**: Binance `-2011` / `-2013` (unknown order) counts as success, matching `_cancel_algo_client_ok` / `cancel_jv_orders_for_symbol` today. Persist incomplete cancels in a new `cleanup_pending` retry queue (this name does not exist on `main` yet). Take a **per-symbol lock** so a watch tick cannot race the close or the leftover cancel.
- QPR4 still records Binance's behaviour for leftover reduceOnly and `closePosition` orders in the PR description.
- Close is gated by the existing execute mode (`assist_execute_mode()`). A separate `QUICK_ORDERS_ENABLED` flag is required for M2 opening, not for close.

### 2.6 Opening (M2 / QPR5) should reuse the existing entry path

- `execute.py` already has an entry flow (`ey:`) that places the entry, places a STOP_MARKET and verifies it, and runs an emergency close when the stop fails. M2 should call that path with quick sizing instead of new order code.
- Intent: **30 s server-side expiry** (stricter than the main path, which uses `PREVIEW_TTL_MS = 120_000` today). Tests must distinguish 29.9 s (valid) from 30 s (expired).
- Deterministic `newClientOrderId` via the existing `client_order_id()` helper (`jv-` prefix, 36-character limit).
- Risk gates (quick cap and combined portfolio cap) run at **preview and again at confirm**. `QUICK_ORDERS_ENABLED` is checked **server-side** (not only in the UI). The flag ships **`false`**. A testnet run is required before the owner may enable it. This is a new trigger for real entries outside the 4h signals, so it needs separate owner approval.

### 2.7 Drop QPR3 (n8n context + OpenClaw summary) for now

Unchanged. For trades that last minutes, an LLM summary adds latency and failure modes without a measured benefit. Revisit only if the quick report shows an edge and the owner asks for it.

### 2.8 What stays, and what QPR1 actually shipped

Stays: the separate quick stats bucket, the quick report metrics, net-of-fee (and, after 20 Nov, net-of-real-fee-and-funding) R, staged orders, and Telegram latency (the listener is a long-poll thread inside `jobs`).

**Quick cap (settings + option B), as shipped and reconfirmed 20 Nov:** default **1%**, allowed **0.5 / 1 / 1.5 / 2%**, confirm above 1%. This matches §4.

**Per-trade Quick risk, as shipped by QPR1 (not revisited on 20 Nov):** `assist_quick_settings.risk_pct` default **0.5%**, allowed **0.25 / 0.5 / 0.75 / 1.0**, confirm at 1.0 (`RISK_CONFIRM_AT`). This is not the portfolio cap, and it is not the stale §2.8 "0.25–1.0%, confirm above 0.75%" proposal.

**Quick-only daily stop and 3-loss lock — shipped, now out of policy.** QPR1 did ship both, and they are live on `main`:

- `day_max_loss_r` default **2R**, allowed 1 / 1.5 / 2. When today's net R is at or through the cap, Quick locks until 00:00 Asia/Bangkok (`LOCK_DAY_MSG`).
- `streak_lock_n` default **3**, allowed 2 / 3. When that many closed losses in a row occur, Quick locks until 00:00 Asia/Bangkok (`LOCK_STREAK_MSG`).
- The ⚡ Quick settings UI still exposes both, and `is_quick_locked()` enforces them on the sizing card and registration.

**Existing rules that remain (QPR2a / #336):** Quick is blocked while the **main week_loss** pause is active (`pause_reason = 'week_loss'`; a pre-migration-29 NULL reason fails closed via `paused_until_ms` + `week_pause_id`). The main **day-3R and streak pauses do not block Quick**.

On 2026-11-20 the owner chose **no Quick daily stop and no 3-loss lock beyond those existing rules**. A small settings follow-up (before or with M1) must stop enforcing `day_max_loss_r` / `streak_lock_n`, stop advertising them on the settings screen, and leave only the week_loss block plus the caps. Removed by the L PR (2026-11-20): the locks no longer fire or appear in settings; the columns stay for rollback.

## 3. Milestones (after this document is accepted)

| Milestone | Content | Orders? | Gate before it |
| --- | --- | --- | --- |
| **M0** | This refreshed design, committed as `docs/superpowers/specs/2026-11-20-quickmode-design.md`. No behaviour change except what the owner separately accepts in the settings follow-up. | No | Owner review of this file |
| **M0.5 — positions close button** | In 📋 ไม้ที่ถืออยู่ (`menu.py`, text-only today), every open position gets a ปิดไม้ button at any R, not only at the 0.5R / 1R / 1.5R progress alerts or exit advice in `watch.py`. Tap → one confirm showing current uPnL / R → existing `xx:` / `run_exit_sequence` (qty/entry check, replay on second tap). Gated by `assist_execute_mode()`; no change to stops, `flat_exit`, signals or advice. | Close only (existing exit path) | Owner OK; M0 merged or alongside |
| **M1a — QPR4 close** | Explicit Long / Short buttons on the Quick menu; ปิดไม้ button on open quick positions with **one confirm**; close via the shared exit path (`source='quick'`); itemised leftover protective orders on the confirm; idempotent reconcile cancel of **all** reduceOnly / `closePosition` leftovers including owner-placed (`-2011` / `-2013` = success; `cleanup_pending` retry); per-symbol lock versus the watch tick; blocked main-signal button opens the close card; removal of Quick daily-R and streak locks (or its own small PR right before). | Close + cancel protective orders | M0.5 merged |
| **M1b — journal + fees** | Append-only quick journal schema and writes; sizing-card fee model (6 bps entry, 10 bps exit, funding); live `commissionRate` cached daily with house fallback; fee-source logging. | No | M1a merged |
| **M2 — QPR5** | Open button reusing the existing entry path with quick sizing; two-step confirm; **30 s server-side expiry**; deterministic `newClientOrderId`; risk gate at preview **and** confirm; `QUICK_ORDERS_ENABLED=false` checked server-side; testnet run before any enable. | New entry trigger | Separate owner approval; M1a and M1b deployed; testnet evidence |
| **M3 — Measurement** | n = 25 **closed** quick trades, pass if mean net R − 1.65·SD/√n > 0, else extend to 50 (§4.2.12); expectancy from the append-only journal using **real fill fees + funding**. Owner go/no-go on whether to build the feed. | — | M2 enabled only as far as the owner has approved; enough closed trades |
| **M4 — QPR2b** | `quickfeed` service (WebSocket markPrice@1s + kline 1m/5m for QUICK_SYMBOLS); separate process; separate Redis or none, **refuse start if a Redis URL matches main**; SQLite feed file; staleness guard; real-time stop advice (rule pre-registered then). | No (advice only) | Explicit owner go after M3 |
| QPR3 | Dropped for now | — | — |

M1 can (and should) come before M4: the owner gets a close button for hand-opened, registered quick trades early.

### Exit criteria

**M0**

- This design is committed at the path above (or another path the owner names) and merged to `main`.
- Changelog, owner decisions, shipped-versus-policy lock gap, fee model, milestones and hard rules are in the committed file.
- No production behaviour change from M0 itself.

**M0.5**

- 📋 ไม้ที่ถืออยู่ shows a ปิดไม้ button per open position regardless of R; one confirm; reuses `xx:` / `run_exit_sequence`; second tap replays, never double-closes.
- With execute mode off, no button is shown (text-only as today). Router test through `process_update`; callback_data ≤ 64 bytes.
- Main regression: progress alerts, exit advice and move-stop buttons unchanged.

**M1a**

- The Quick menu offers explicit Long / Short buttons (typed input still works) within the 64-byte callback_data limit, routed through `process_update`.
- Tapping ปิดไม้ requires exactly one confirm before the market close is sent.
- A registered open quick position can be closed from the card via `run_exit_sequence` with `source='quick'`.
- Before the tap, leftover protective orders are listed itemised; after the close, each cancel is reported itemised. A partial cancel is never shown as a full success. Retry only cancels leftovers, it does not re-send the market close.
- `-2011` / `-2013` count as success; remaining work lands in `cleanup_pending` and is retried safely. A watch tick cannot interleave cancels or advice on the same symbol during the close.
- Owner-placed reduceOnly / `closePosition` orders are cancelled; non-reduce entry orders are not.
- The blocked main-signal button ("มีไม้ quick อยู่ — ปิดก่อนถึงเข้าได้") opens the close card, not `ec:`.
- After the lock-removal change, Quick daily-R and streak locks no longer fire or appear in settings; week_loss still blocks Quick.
- `ruff`, `mypy`, `pytest` and docker image CI are green. Deploy only with owner OK, outside :20–:27 Asia/Bangkok.

**M1b**

- Journal rows are append-only and include the fields in section 2.3 (MAE/MFE may be filled once marks are available; they must not be silently invented).
- Sizing-card fee math matches section 2.3. `ruff`, `mypy`, `pytest` and docker image CI are green. Deploy only with owner OK, outside :20–:27 Asia/Bangkok.

**M2**

- With `QUICK_ORDERS_ENABLED` unset or false, the open button is inert and the server refuses; **zero Binance trade-write calls** on that path (read-only GETs for the card remain allowed).
- A valid confirm at 29.9 s places at most one order; at 30 s it expires with no order. N concurrent taps produce **one** order (deterministic `newClientOrderId`).
- Quick cap and combined portfolio cap are re-checked at confirm. Partial fills, the error-code matrix below, and a testnet run are evidenced in the PR. The flag stays false on the VPS until a later, explicit owner OK (and a `.env` edit the owner makes).

**M3**

- The quick report can show n, win rate, sum/mean R, real-fee R, funding, expectancy, max DD, and "n/20–30" towards the gate, all from the journal, not from 6 bps house math.
- The owner has a written go or no-go for M4.

**M4**

- `quickfeed` is a separate compose service. Start fails closed if a Redis URL is missing when required **or** if it matches main Redis. Jobs never write that Redis. Stale (>5 s) data shows "ไม่มีราคาสด" and suppresses advice and buttons. Main `/healthz` and the kill switch are unaffected if the feed dies.

## 4. Owner decisions (2026-11-14)

1. Quick symbols: **any symbol the main system does not currently hold** (mutual exclusion, section 2.1). Claude had recommended symbols outside the main 7; the owner chose otherwise, which added the main-side guards in QPR1b.
2. Risk cap: **option B.** Quick cap has a system default and is owner-adjustable (default 1%, range 0.5–2%, confirm above 1%).
3. Weekly loss pause: **counts quick trades.** (The pause itself is 4 × assist `risk_pct` of week-start balance, [#333](https://github.com/chatoe-norm/jarvise/pull/333), not a fixed 6%.)
4. After a quick close: **the bot cancels the leftover protective orders on that symbol**, including ones the owner placed by hand (scope in 2.5).
5. WebSocket feed: **only after 20–30 quick trades show positive net expectancy.**

### 4.1 Main signal while a quick position is open on that symbol

**Owner decision (2026-11-14): (a).** The signal is still sent and still tracked in the virtual stats. Its entry button is replaced by "มีไม้ quick อยู่ — ปิดก่อนถึงเข้าได้". Main signal stats are unaffected; only the real entry waits until the quick position is closed. **M1:** that button opens the quick close card.

Rejected: (b) skip the main signal; (c) the bot closes the quick position by itself.

### 4.2 Owner decisions (2026-11-20)

1. **Quick cap reconfirmed:** default 1%, range 0.5–2%, confirm above 1%. Combined gate uses the dynamic portfolio cap (section 1.3).
2. **No Quick daily stop and no 3-loss lock** (owner explicitly confirmed removal of the shipped `day_max_loss_r` and `streak_lock_n` locks, 2026-11-20 01:20) beyond existing rules (week_loss blocks Quick; main day-3R and streak pauses do not). QPR1 already shipped both locks — see §2.8 for the follow-up.
3. **Measurement gate stays 20–30 closed quick trades**, but expectancy is computed from **real fill fees + funding**, not the house model. The append-only journal in section 2.3 is required.
4. **Fee model:** entries 6 bps/side; stop/market exits 8–10 bps (card uses 10 bps until the owner names a single figure); include funding on the sizing-card break-even when a hold would cross a funding time; live `/fapi/v1/commissionRate` cached daily with conservative fallback; log fee source per fill.
5. **Close cancel scope unchanged**, plus: the card lists leftover protective orders itemised before the tap.
6. **Quick menu requirements (owner, 2026-11-20 01:16):** the Quick menu lets the owner pick **Long or Short** explicitly with buttons, and has a **close-position button** the owner can tap whenever they want to close an open quick trade. Verified at `a0cc39c` (`quick_card.py` l.200–235): today the side is **typed as text** (`<symbol> long|short <stop>`, aliases l/s/buy/sell); there are no Long / Short buttons, and no quick close button exists. M1 adds explicit Long / Short buttons (typed input stays as a fallback) (callback_data ≤ 64 bytes, routed through `process_update`). The close button is the M1 / QPR4 close path (section 2.5).
7. **M1 is split** into M1a (close, cancel, buttons, lock removal: the order-touching part) and M1b (journal and fees: no orders) (owner, 2026-11-20 01:21).
8. **Close needs one confirm** after tapping ปิดไม้ (owner, 2026-11-20 01:21).
9. **Main positions menu gets an any-time close button** (owner, 2026-11-20 01:27), as its own small PR (M0.5) before M1a. Reason: the close button today appears only at the 0.5R / 1R / 1.5R progress alerts or exit advice, and a position held 3 days never reached 1R, so there was no way to close from the bot. This is a UI addition to the main flow; main rules (stop, `flat_exit`, signals) are unchanged.
10. **M0.5 hides the close button on quick positions** until M1a lands (temporary exception to 9); the server also refuses `xc:` for quick symbols from old messages (owner, 2026-11-20 02:02).
11. **Owner-placed leftover orders after a main menu close:** M0.5 lists them on the confirm and result as "✋ คุณตั้งเอง — จะยังค้างอยู่ ลบในแอป" and cancels nothing new (only `jv-` as today). M1a-2 then extends the reduceOnly / `closePosition` leftover cancel (new `cancel_order`) to main menu closes as well (owner, 2026-11-20 02:02).
12. **M3 gate pre-registered:** fixed n = 25 closed quick trades (live, after the journal exists, excluding testnet). Pass only if mean net R − 1.65 × SD / √n > 0 on real fees + funding. If not passed at 25, continue to 50 and re-test once; the owner then decides (owner, 2026-11-20 02:02). This replaces "20–30".
13. **M2 has no extra first-trade risk limit** beyond the quick risk settings and caps (owner, 2026-11-20 02:02).
14. **M0.5 is live-mode only** (`assist_execute_mode() == 'live'`, same rule as the watch): in testnet mode the read client is live fapi but the trade client is demo, so a close would wrongly answer "already closed". The kill switch keeps blocking entries only; exits stay allowed.

15. **Owner decision 15 (2026-11-20 02:20), scope of this PR:**

- (a) `🔄 อัปเดตราคา` on the quick card. The card is stored under a short server id and rebuilt from the original % stop when one was typed. Otherwise it requotes, and blocks when the stop is now on the wrong side. This fixes the 2-min card TTL vs the 10-min registration.
- (b) Telegram notices when a registration is tagged to a fill, when it expires, and when a quick trade is closed in Binance. The Quick menu shows the registration countdown and, once the trade is open, the position.
- (c) A mode tag (`🧪 เทสเน็ต` / `💰 เงินจริง`) on every quick card, confirm and result. Leftover orders are listed one by one instead of as a count.
- Also: the Quick menu leads with trade buttons, and settings move below them. Typed `q …` stays as a fallback. The settings Yes/No buttons become Thai. `SL ตาม ATR` stays out (M4).
- Plus decision 6: explicit Long / Short buttons on the Quick menu (this is the core of M1a-1).

## 5. Hard rules (carry into every quick-mode prompt)

- The main system's rules are unchanged: floors 0.55/0.70, analyzer formula, 1.5× ATR stop, `flat_exit`, `same_side_hold`, profit lock, the main universe, the 4h signal path.
- `JARVISE_LIVE_TRADING` stays off; no change to the kill switch or `assist_execute_mode`.
- No VPS `.env` change without explicit owner OK. Any quick order flag ships off and inert.
- The quick feed never uses the main Redis (and must refuse to start if a configured Redis URL matches main).
- Ship flow as in the move-stop prompts: PR, CI green including docker images, owner OK, merge outside the :20–:27 window, Deploy head equals the merge SHA, read-only `/healthz` and VPS `git log -1` from the MacBook.

## 6. What is already on `main` (for implementers)

Verified at `a0cc39c`. Times are Asia/Bangkok.

| Work | PR | Merged | Notes |
| --- | --- | --- | --- |
| QPR1 settings, tagging, lock, report | [#331](https://github.com/chatoe-norm/jarvise/pull/331) | 14 Nov 2026, 19:01 | Migration 27. Bookkeeping only; `quick.py` still says "No orders." |
| QPR1b main-side guards | [#332](https://github.com/chatoe-norm/jarvise/pull/332) | 14 Nov 2026, 19:48 | Watch skip, sizing exclude, `MSG_QUICK_OPEN`. |
| QPR2a sizing card + 10-minute `qr:` registration | [#334](https://github.com/chatoe-norm/jarvise/pull/334) | 14 Nov 2026, 22:19 | REST mark; house 6 bps/side both ways; no orders. Size is never shrunk — leverage over 5× or short margin refuses. |
| QPR2a leftovers | [#335](https://github.com/chatoe-norm/jarvise/pull/335) | 14 Nov 2026, 22:49 | Card edges. |
| Week_loss-only block | [#336](https://github.com/chatoe-norm/jarvise/pull/336) | 14 Nov 2026, 23:37 | Migration 29 `pause_reason`. Day-3R and streak pauses do not block Quick. |
| ⚡ Quick on the reply keyboard | [#346](https://github.com/chatoe-norm/jarvise/pull/346) | 16 Nov 2026, 12:53 | At `a0cc39c` the button is `BTN_QUICK = "⚡ Quick"` on the **third row**, left of the assist toggle — not the top-left key. |
| Dynamic portfolio cap | [#353](https://github.com/chatoe-norm/jarvise/pull/353) | 17 Nov 2026, 03:49 | 2× live `risk_pct`, ceiling 6%; Quick combined gate uses the same cap. |
| Cap tests and docs | [#360](https://github.com/chatoe-norm/jarvise/pull/360) | 17 Nov 2026, 11:16 | |

Not done on `main`: close button, leftover cancel of owner-placed orders, live `commissionRate`, open path, `QUICK_ORDERS_ENABLED`, `quickfeed`, append-only measurement journal (beyond the current `assist_quick_trade` columns).

## 7. Test matrix and read-only post-deploy checks

### 7.1 Test matrix (land with the milestone that introduces the behaviour)

- **Flag off (M2, also as a regression from M1):** `QUICK_ORDERS_ENABLED` unset/false → open path makes **zero** Binance trade-write calls. Card/register GETs may still run.
- **Expiry (M2):** confirm at 29.9 s is valid; at 30.0 s is expired and sends no order.
- **Idempotency (M1–M2):** N concurrent taps on the same close or the same confirm → **one** order (same `newClientOrderId` / replay).
- **Partial fills (M1–M2):** a partial close or entry is never reported as a full success; recovery follows the existing execute UNKNOWN / query path.
- **Binance error codes (M1–M2):** `-1021` (timestamp), `-2019` (margin), `-4164` (order notional below the minimum: refuse with a clear message, no retry), `-1003` (rate limit), `-2022` (reduceOnly; treat as closed only after `positionRisk` is flat, as `run_exit_sequence` already does), `5xx` and timeout → `UNKNOWN`, never a silent success. `-2011` / `-2013` on cancel → success.
- **Cap matrix (card + confirm):** main `risk_pct` 0.5 / 1 / 2 / 3 / 3.01% (ceiling) against quick cap 0.5 / 1 / 2%, including the combined gate `main_open + quick_open + new` and the isolated `quick_open + new` cap. Main sizing still ignores quick tags.
- **Orphan-order audit (M1):** after a close, no reduceOnly / `closePosition` leftovers remain on the symbol; a non-reduce entry order on that symbol is still there; a failed cancel is itemised and queued in `cleanup_pending`.
- **Locks (settings follow-up):** after the 20 Nov policy PR, week_loss still blocks the card; main day-3R and streak pauses do not; Quick-only daily-R and streak locks no longer fire.
- **Router:** every new prefix is covered through `process_update`.
- **Main regression:** with no quick tag, watch, sizing, signals and `ey:` / `xx:` / `ms:` stay behaviour-identical.
- Standard gates: `ruff check`, `ruff format --check`, `mypy src`, full `pytest`.

### 7.2 Read-only post-deploy checks (after each merge, from the MacBook)

- Deploy head SHA equals the merge SHA. `git log -1` on the VPS matches.
- `GET /healthz` is `ok` and `paper_only` / live-trading flags are unchanged (`JARVISE_LIVE_TRADING` still off).
- After M1/M2 with flags off: no unexpected trade-write traffic; a flag-off probe of the open path records zero Binance POSTs.
- Spot-check one Open Orders snapshot for a recently closed quick symbol (orphan-order audit).
- Do not edit VPS `.env` unless the owner has just approved a specific change.

## 8. UX notes

- **Long / Short picker.** The Quick menu shows explicit Long and Short buttons; the chosen side drives the sizing card, the `qr:` registration, and (M2) the open intent.
- **Close button always available.** Every open quick position shows a close button (ปิดไม้) on the Quick menu and its card; tapping it opens the close card (section 2.5).
- Keep existing Thai copy where it already shipped: "⚡ เริ่มไม้ quick", "มีไม้ quick อยู่ — ปิดก่อนถึงเข้าได้", "ไม่มีราคาสด", lock and week_loss refusals, the Quick settings lines.
- **Itemised close result.** List the market close outcome and each leftover order (cancelled / already gone / failed). A partial failure is never shown as success.
- **Retry only cancels leftovers.** After a successful close, a second tap replays the close result and may retry `cleanup_pending`; it must not place a second market close.
- **Blocked main-signal button opens the close card** (section 2.1 / 4.1).
- **Paper vs live label.** The card, the confirm, and the result must say whether this is testnet/paper or live, using the current `assist_execute_mode()` (and, for M2, `QUICK_ORDERS_ENABLED`).
- Quick settings must keep showing worst-case total risk as 2× main % + the quick cap.
- Telegram "retry" / `ลองใหม่` stays forbidden on confirm paths that may already have sent an order (`ey:` / `xx:`), as in `AGENTS.md`.
- M1a-1 (this PR): Long/Short stepper `qk:`, stored card + 🔄 อัปเดตราคา, tag/expire/close notices, mode tag.

## 9. Open points (not blocking M0 review)

- The owner has not named a single exit-fee figure inside the 8–10 bps band. This plan uses **10 bps** on the card until that is named.
- MAE/MFE collection needs a defined mark source (REST during the hold is enough before M4; the journal must not invent values).
- Setup-tag vocabulary is not defined; M1 can store a free-text or enumerated tag and tighten it later.
- The 20 Nov lock-policy follow-up is a behaviour change to already-shipped QPR1 settings and should be called out in its own PR description, even if it rides with M1.
