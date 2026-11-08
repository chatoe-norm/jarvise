# P5 go/no-go gate for the trade assistant

Date: 2026-11-04. Status: approved by the owner on 2026-11-04 (answers 1–4 below).
Builds on `2026-10-25-trade-assistant-design.md` and `2026-11-02-assist-execute-design.md`.
Research overlay: this gate does not change analyzer floors (0.55 / 0.70), the
analyzer formula, the 1.5× ATR stop, `flat_exit`, `same_side_hold`, or the
universe. It does not set `JARVISE_LIVE_TRADING` and does not enable auto-decide.

## 1. Intent

P5 means Jarvise places live orders without a per-trade owner tap. This spec
decides only **whether** a P5 plan may be written. It does not design P5.
`PROJECT_CONTEXT.md` still applies: P5 starts only after the gated live path is
stable.

The gate is written before the data exists. The decision rule below is the only
decision cell. Every other cut in §5 is sensitivity only and cannot turn a fail
into a pass.

## 2. Sample

| Item | Value |
|---|---|
| Virtual sample | `assist_track` rows with `cohort='core7_4h_v2'`, `kind='sent'`, `exit_at_ms IS NOT NULL` |
| Start | 2026-11-04T13:51:27Z (`sent_at_ms >= 1793800287000`), deploy of PR #280 |
| Symbols / timeframe | the seven universe symbols, 4h analysis, 1h track bars |
| Cost model | fee 5 bps/side, slippage 1 bp/side, funding at each 00/08/16 UTC settlement |
| Real sample | one-tap live entries (`JARVISE_ASSIST_EXECUTE=live`) linked to a `signal_id` and fully closed |
| Excluded | `core7_4h_v1` (4 bps fee model), `kind` `held` and `blocked`, testnet fills, manual trades without a signal |

Rows closed before the exact-settlement funding lookup ships may use the previous
period's rate for a settlement. That error is at most one funding period per
settlement and does not restart the count.

## 3. Decision rule (all must pass)

| Gate | Rule |
|---|---|
| G1 sample | ≥ 100 closed virtual trades in §2 |
| G2 real | ≥ 20 closed one-tap live trades in §2 |
| G3 expectancy | mean net R ≥ +0.10R **and** bootstrap 80% two-sided CI lower bound > 0 (10,000 resamples of trades, fixed seed 20261104) |
| G4 drawdown | max drawdown of cumulative net R, ordered by `exit_at_ms`, ≤ 10R |
| G5 entry slippage | mean adverse entry slippage ≤ 5 bps (§4) |
| G6 stop slippage | mean adverse stop-exit slippage ≤ 5 bps (§4) |

With trade R spread near 1.2R, G3 needs a mean of about +0.15R at n = 100,
because the lower bound is roughly mean − 1.28 × 1.2 / √n.

## 4. Slippage measurement

Measured per real trade and reported per trade, never only as one pooled mean.

- **Entry:** adverse slippage in bps = (fill avg − reference) / reference × 10,000,
  with the sign flipped for shorts. The **gate reference is the price at order
  time** (the preview price the owner confirmed). P5 enters at signal time, so the
  owner's tap delay is not execution cost.
- **Entry drift (reported, not gated):** the same formula against the signal's
  entry price. It shows what tap delay costs today.
- **Stop exit:** adverse slippage in bps of the exit fill against the stop trigger
  price. Stops are reported apart from take-profit and manual exits, because stop
  fills are usually worse.
- Report mean, median and p90 for each leg, plus real minus virtual net R per
  matched trade.

Data source: `/fapi/v1/userTrades` matched on Jarvise `jv-` client order ids from
`assist_exec_log`, plus `/fapi/v1/income` for commission and funding. The order
path is not changed for this. **This report does not exist yet.** G2, G5 and G6
cannot be evaluated until it ships.

## 5. Sensitivity only (cannot flip the decision)

- G3 recomputed without rows that have `funding_missing > 0`.
- Per-symbol and long/short split of G3.
- `held` and `blocked` virtual results by `block_reason`: did `risk_cap`,
  `margin_cap` and the losing-streak pause help or hurt?
- Real trades only: mean net R from `REALIZED_PNL + COMMISSION + FUNDING_FEE`.

## 6. Looks and outcomes

Check only at n = 100, 150 and 200 closed virtual trades, so repeated looks do not
inflate a false pass.

- **All gates pass:** the owner may approve writing a separate P5 plan. A pass
  does not enable anything by itself.
- **G3 mean ≥ 0 but CI lower bound ≤ 0, or G2 short:** extend to the next look.
  This is not a loss verdict.
- **Mean net R < 0 at any look, G4 breached, or no pass by n = 200:** no-go. Stop
  the P5 path. Any strategy change needs its own pre-registered study.
- **G5 or G6 fail with G3 passing:** no-go for P5 until execution is fixed and
  re-measured on new trades.
- Never lower the 0.55 / 0.70 floors or change the analyzer, stop or exit rules
  to pass this gate.

## 7. Risk limits for the live account

The futures account holds about 100 USDT, separate from the 10k paper ledger.

- Risk per trade stays **0.5%** for live. The 3–5% proposal is rejected: a short
  losing streak would do too much damage on 100 USDT.
- Existing caps stay as they are: at most 2 open ideas, 4% total open risk,
  leverage ≤ 5×, pause after 3 losing trades, pause on a week loss above 6%.
- No separate daily 3R cap: at 0.5% risk the 3-loss pause already stops the day
  at about 1.5% of equity.

## 8. New symbols or timeframes

Any new symbol list (for example D2) or timeframe (for example 1h) is a separate
test with its own `cohort` string, its own registration note in `docs/research/`
written before the first trade, and its own 100-trade count. Its trades never
pool with `core7_4h_v2`.

## 9. Owner answers (2026-11-04)

1. Slippage gate 5 bps, measured per trade and split by entry and stop exit.
2. Bootstrap 80% lower bound > 0 added; a miss means collect more, not a loss.
3. 0.5% risk per trade only.
4. Count starts at 2026-11-04T13:51:27Z. Funding uses only the 00/08/16 UTC
   settlement rates, never the hourly rows.
