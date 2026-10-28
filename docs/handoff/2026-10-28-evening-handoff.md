# Jarvise evening handoff — 2026-10-28

Follows [`2026-10-28-mac-handoff.md`](2026-10-28-mac-handoff.md), which covers everything up to PR #238 and the Mac setup. `AGENTS.md` and `PROJECT_CONTEXT.md` are still the source of truth for rules. This page lists what changed on 2026-10-28 afternoon and evening, and what is open.

## 1. Shipped today (all on `main`, deployed)

| PR | Change |
| --- | --- |
| #239 | The "🔎 ตอนนี้มีเหรียญไหนน่าเข้า" button shows ideas that push-dedupe had hidden, with "ส่งแล้วเมื่อ HH:MM" or "ไม่แนะนำไล่". Confirmed in Telegram: ADA short showed as sent at 20:59 the day before. |
| #242 | Assist explore list D1: owner-managed extra coins, data and analysis only. Spec [`docs/superpowers/specs/2026-10-28-assist-explore-list.md`](../superpowers/specs/2026-10-28-assist-explore-list.md). |
| #243 | Fix: the explore add probe asked Binance for 1 kline, which is always the still-open bar, so every add was rejected. It now asks for 2. |

### Explore list (D1) state on the VPS

- Coins added by the owner: AAVEUSDT, AVAXUSDT, BCHUSDT, LINKUSDT, NEARUSDT, SUIUSDT.
- `assist-explore.db` sits beside the main DB (`/app/data/analytics/` in the container) with 999 closed 4h and 1d bars per coin. The membership table `assist_explore_symbols` is in the main DB.
- n8n `jrvsAssistSignal4h` was re-imported: schedule → `POST /jobs/assist-explore` (90 s, continue on error) → `POST /jobs/assist-signal`. The 16:25 and 20:25 runs wrote analysis.
- All six coins were `flat` at the 2026-10-28T00:00Z bar (SUI 4h short vetoed by 1d trend_up; LINK confidence 0.50; NEAR range).
- The main DB has old research rows for AVAXUSDT (up to 2026-10-16). Explore did not write them.
- Coin choice: a coin needs about 250 daily spot bars, or the 1d EMA200 is missing and MTF forces flat. HYPEUSDT has 35 and is not useful. `1000…` futures symbols have no spot pair and are rejected.

### D2 (not started)

Signals, position watch, virtual track, and the Monday summary for explore coins, per spec decisions b–d. D2 must not send explore signals while the kill switch is engaged or core signals are held for a market-safety reason, because explore analysis skips market safety. Claude writes the D2 prompt when the owner asks.

## 2. First real trade (open at the time of writing)

| Item | Value |
| --- | --- |
| Position | ADAUSDT short, 145 ADA, entry 0.2065, Isolated 1x, Single-Asset mode |
| Orders | Stop Market close-position, Mark ≥ 0.2129; Take Profit Market close-position, Last ≤ 0.1937 (2R from the real entry) |
| Risk | about 0.93 USDT at stop; about +1.86 USDT at TP |
| Watch | `assist_watch_advice` sent `move_stop` / `breakeven` at 0.206087 around 20:05 Bangkok (price passed 0.5R) |
| 21:18 | mark 0.20161 (+0.76R); low since entry 0.20141 (+0.79R) |

Claude told the owner to move the stop to 0.2061 (Mark). Whether the owner did is not confirmed. At +1R (mark ≤ 0.2001) the watch trails the stop to best low + 1R. Record the result when the trade closes; it is the first real trade for the 2026-11-02 weekly summary.

How the account was set up, for the next trade:

- The account was in **Multi-Assets** mode, which forbids Isolated. The owner switched to Single-Asset (`S` beside leverage).
- Binance's order-form TP/SL defaults to **Last** price, and an order-attached stop stays as a separate order when a Position TP/SL is added later. The owner cancelled the duplicate Last-price stop by hand.
- No watch message while the position is quiet is normal. Watch only sends move-stop, exit, or no-stop advice. `futures.py` reads both `openOrders` and `openAlgoOrders`, including `closePosition` stops.

## 3. Open now

1. **PR #241** (dependabot batch) is open and waits for Claude's review. After it ships, close #217–#222 and #237.
2. **Assist message follow-ups** (one small PR when the assistant is quiet):
   - The signal says "Leverage 1x (isolated)". It should say to check the margin mode, because Multi-Assets accounts cannot use Isolated.
   - Tell the owner to set the stop trigger to Mark, and warn that an order-attached TP/SL plus a Position TP/SL gives two stops.
3. The small follow-ups from the Mac handoff §3.4 are still open (assist-signal payload lacks `available_usdt`; unmapped on-demand reasons in English; Approve button on `expired` rows; long-entry rule quoted on a short idea).

## 4. Mac and tools

- **SSH.** `ssh Jarvise-VPS` works from this Mac (root@76.13.218.48, `~/.ssh/id_ed25519_jarvise`, host `srv1811101`). **Do not use lowercase `jarvise-vps`**: in this Mac's `~/.ssh/config` it points to 72.62.244.54 (`srv1269762`, the T4Trip server, no `/opt/jarvise`). `jarvise-vps-ts` (100.93.110.48) is also not the Jarvise VPS.
- **Claude in Chrome.** The native host `com.anthropic.claude_code_browser_extension.json` is installed. The tools load only in a session started with `claude --chrome`; a running session does not pick them up. Not yet verified end to end.
- **n8n import** is manual: `infra/docker/render-n8n-jobs-workflows.sh`, then import and publish only the workflow that changed, then delete the rendered files.

## 5. Checkpoints (unchanged)

See the Mac handoff §5: first weekly assist summary 2026-11-02 08:30, shadow-veto day 30 on 2026-11-17, Forward H3 first look 2026-11-25. H3 frozen files are unchanged.
