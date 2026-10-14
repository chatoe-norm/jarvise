# Walkthrough: The Rumers — 3R morning routine and sneaky pivot

**Research overlay — not trading doctrine.** Do not index this page into Qdrant `jarvise_doctrine`. Do not silent-merge it into SQLite or the paper ledger. Do not retune analyzer floors **0.55 / 0.70**, do not change `same_side_hold`, and do not set `JARVISE_LIVE_TRADING=true`. These videos are US equity and index-futures day trading. They are not a Jarvise spot path and not a futures execution path. How this series may inform the paper review, and what it must not change, is in [2026-10-14-youtube-notes-vs-jarvise.md](2026-10-14-youtube-notes-vs-jarvise.md).

Compiled **2026-10-14** from English YouTube auto-captions. Speaker is **Doug** on **The Rumers**. He says he has day-traded for **26 years**. The description on both videos says his results and risk tolerance are not typical, that the videos are educational and entertainment, and that he is not a financial advisor.

Sits beside [2026-10-14-binance-futures-10-to-50.md](2026-10-14-binance-futures-10-to-50.md). That note is a different author and a different market. This page does not change it.

| What | How to read it |
| --- | --- |
| Title lines ($10,000/month, 15 minutes a day, futures up 80% of the time, 15-year SPY clock test) | His claims. No statements or independent backtest are in the videos. |
| Chart examples (TSLL, YM) and the two narrated tickets (AAOI, GGLL) | What he says while the chart is on screen. A few spoken prices conflict. They are kept as spoken, not rebuilt into a P&L. |

Sources:

- [If You Only Have $50, Do This Every Morning (The 3R Rule)](https://www.youtube.com/watch?v=XBOGd67BJks) (~24:44)
- [How Trading Like an Idiot Makes Me $10,000/Month (15 Minutes a Day)](https://www.youtube.com/watch?v=zspMXJVbfAY) (~26:13)

## Video 1 — the 3R / probability factor

He says that before the larger opening-bell wins, he traded one setup every day and risked no more than **$20–$50** a trade. The line he repeats: if you cannot make **$50** consistently, you cannot make **$50,000**. The process from a one-figure account to a six-figure account stays the same. Only the numbers change. Chasing money ahead of method is what he says smoked him.

The video’s four lessons, in his order:

1. Trade the smallest risk you can, at any account size.
2. Aim at larger gains with the **probability factor** (this is what the title calls the **3R rule**).
3. Scale with the **Wall Street raise** (auto-captions render it “Raze”).
4. Pick the asset class he would use if he started again with about **$50** of risk.

He treats a standing **2:1 or 3:1** rule as incomplete. The stop is the part a trader can set. The market’s willingness to pay the target is the part a trader cannot set, and that is the part he says decides whether you stay in the business. A dollar stop with no structure gets run, then the name trends the way you thought without you. A target the day’s range cannot reach is why people say “I wish I had held” and also why they should not have held.

### Data he states

- Risk cap while learning the setup: **$20–$50** per trade.
- With-trend share of the day’s ideas: **80–90%**.
- Contrarian trades: smaller size, scalps of about **30 seconds to 2 minutes**.
- With-trend targets he names: **3:1 through 6:1**.
- **TSLL** (Tesla bull ETF), daily ATR with no extra filters: about **$0.70** per share (the caption’s “6970” is that 70 cents). Already up **$0.33** on the day. A chase long with the stop under the day’s low needed about a **$1.10** move and about **$400** of profit to make 2:1. He rejects that because it asks for more than a full ATR.
- The short he prefers, into the swing high, uses the same **$45–$50** of risk, stop just above the sellers, target back at the morning liquidity. He measures that near **4:1**. It eventually tagged the base of the range.
- **YM** (Dow futures), from his squad room: a breakout look already about **981 points** up versus a daily ATR of about **600** (about two times range), pressed into the prior day’s liquidity, and trading under the 50-day. He treats the value as downside.
- Scaling mock-up: **$5,000** account, **50 shares**. Each **10%** gain (**+$500**) adds **10%** to share size (**50 to 55**). Conservative until about **30%** growth, then a bit more aggressive. On a **$300,000** account, waiting for a 10% step is too coarse.
- Consistency gate before any size increase: small enough that the heart rate is calm, green **3 of 5** days, and a couple of weeks of that. Do not quadruple size after two green days. He says the first size-up trade is often a loser.
- If he started over: **index futures** first — **ES** (E-mini S&P), Nasdaq futures, Dow futures. He says futures “go up about **80%** of the time.” He then gives a different statistic: over **15 years**, buying **SPY** (or the ES) at the open with no chart and selling **1 hour** later won **51%**, **2 hours** **53%**, and a full session **54%**. Those two figures are not the same measurement. Both stay as spoken claims.

### Knowledge

- Consistency at a tiny risk is the skill. Scaling is the same process with larger numbers.
- Reward is not a number you type in. It exists only if trend, location, and the day’s range can deliver it.
- A stop that is only a dollar amount sits in the wrong place and gets tagged.
- Being early and aggressive belongs on the side of the daily trend. The other side is a scalp.
- Index futures are his preferred starter market because, on his telling, a blind long already wins about half the time and chart work is supposed to improve that. Stocks, FX, crypto, options, and commodities can pay, and he says they come with more problems for a new trader.

### Technical steps

1. Daily chart. Add a **50-period simple moving average**. No other filters.
2. Price above the 50-day: plan longs. Price below: plan shorts.
3. Drop to the **5-minute** chart. Mark the previous day’s high and low (range high, range low).
4. Walk left until the next high above the range high (swing high) and the next low below the range low (swing low). Distance in days does not matter.
5. Upper pair is the sell zone. Lower pair is the buy zone.
6. Daily **average true range**, no extra filters. That number is the baseline expected move, up or down, in price units (cents on TSLL, points on YM).
7. Compare the open’s distance already traveled with that ATR, and compare a structural 2:1 target with the ATR. If the target needs more than the day’s range, skip the trade.
8. Place the stop beyond the structure you are leaning on (above the sellers for a short at the high, under the buyers for a long at the low), then check that the dollar risk is still the small number you meant to risk.

### Tactics

- Do not buy a strong intraday breakout just because the candles are blue. Check the 50-day, how close price is to the upper lines, and how much of the ATR is already used.
- On TSLL below the 50-day and pressed into the range high and swing high, he wants the short with sellers, not the chase long.
- With-trend longs: breakouts and dip reversals, full risk, aggressive targets. With-trend shorts: breakdowns and top reversals, full risk.
- Contrarian (short above the 50-day, or long below it): smaller size, in and out in a couple of minutes.
- Scale only after the weekly hit rate is already there. Two green days are not a license to lean in.
- The $50 is the risk, not the account. He says anyone can define a $50 stop. The work is putting that $50 where the structure is, on a name whose range can pay.

## Video 2 — the sneaky pivot

The title says this simple strategy makes him about **$10,000 a month** in about **15 minutes a day**. The video does not show a month of statements. What it shows is one 15-minute method, a YM playback, and two narrated stock/ETF tickets. Record the monthly figure as a title claim.

He says getting smarter, cuter, or more clever made him look stupid, and that the fix was fewer moving parts: one time frame, one candlestick sequence, about 15 minutes of attention. He says he will trade it live with real money rather than only show hindsight charts.

### Data he states

- Time frame: **15-minute** only. No indicators required for the method.
- Four lines, same definitions as video 1. On TradingView he points at **Rumers magic lines**, which also layers one more level above and one more below. He still teaches the four hand-drawn lines so the method is not locked to that indicator.
- First **15 minutes** of the day often ping-pong between range high and range low. After that, a break of the range low often visits the swing low, and a break of the range high often visits the swing high.
- Three candles: opening-range candle, sneaky candle, entry candle. He says the entry is often the bar at the **45-minute** mark.
- YM playback: the first bar plows to the swing low, the next bar is green (the sneaky candle), and the buy is the push through that candle at about **49,521**. Stop goes under the buyer that has already been tested for about half an hour. The line is a zone, not a literal tick. The example then chopped about **45 minutes** with repeated wicks and still held the low. Target is the upper seller.
- **AAOI** long, **500 shares**. Entry he states as about **$170.32**, and he also says **$172.32**. Keep both. Swing high near **184**. Three lower wicks near **166**. He wants a push through **176** into the upper seller. Into the close he says it tagged that seller and was **almost $3,000**.
- **GGLL** (he also says GGL), a Google ETF, **1,000 shares** near **$136.64**. Next target he names is **$139.54**. It made a V toward the open. He says it was up almost **$2,000**, then a fast 15-minute bar rejected and he cut it on the way back toward the buyer. He still calls the day “two for two” and says the ROI was good, and he calls the Google cut disappointing.
- Mid-session he also says “about 18” on one name and “almost $3,000 on these two.” Those phrases do not reconcile cleanly with the share counts. They stay as spoken. This note does not rebuild a P&L from them.

### Knowledge

- Most of the chart is a waiting room. The only places he acts are the four lines.
- The upper pair is where sellers have shown up before. The lower pair is where buyers have shown up before. Until those participants leave, he expects price to stall there.
- The first candle chooses the side. He does not start the day already bullish or bearish.
- A level is a zone. A wick through the line and a snap back still counts. “Close enough,” plus several tests, is his discretion when a wide name never quite tags the daily line.
- Textbook drawings do not match the live chop. The trade stays valid while the defended low (or high) holds, even when it has not rallied yet.
- A live session includes a winner and a name that gives the open profit back. He treats that as part of participating, not as a reason to rewrite the rules mid-bar.

### Technical steps

1. One 15-minute chart. Leave indicators off unless you are only using the magic-lines helper to draw the same levels.
2. Mark the previous day’s high and low.
3. Mark the next high above that and the next low below that.
4. Do nothing until price is at one of those four lines.
5. Let the first 15-minute candle show which line it chose.
6. Wait for the next 15-minute candle (the sneaky candle) to show the turn: green after a test of the lower lines for a long, the mirror for a short at the upper lines.
7. Enter when the following candle pushes through that sneaky candle. On a long, that is a cross above it. Stop goes under the tested buyer. On a short, stop goes above the tested seller.
8. Target the opposite side of the range: from the lower lines back to the range high or swing high, or from the upper lines back toward the swing low.
9. If the daily lines are very far apart and the first candle already used most of the day’s range, he allows a smaller map: the high and low of that opening 15-minute candle.

### Tactics

- Sell only the upper two lines. Buy only the lower two. Leave the middle alone.
- Do not buy the first touch of the low. Wait for the sneaky candle, because that low might be the day the level fails.
- The stop sits beyond the big buyer or the big seller, which is why he draws the lines before he thinks about the order. He calls that placement the reason people stop getting tapped and then watching the move leave.
- Hold through the ugly middle as long as the defended extreme holds. In the YM playback that middle lasted on the order of 45 minutes.
- When a name is extremely wide, do not force a tag of a distant daily swing. Use the opening candle’s own high and low.
- On the day he narrates, AAOI ran from the lower wicks to the upper seller. GGLL looked the same, reached toward the open, then a single 15-minute bar plowed back to the buyer and he cut it. He did not add a new rule after the cut.

## Combined summary

Both videos use the same four lines: previous-day high and low, plus the next swing beyond each. The upper pair is where he wants to sell. The lower pair is where he wants to buy.

Video 1 adds two filters so a **$50** stop is only placed when the math can pay. The **50-day average** picks the side that gets full risk. Daily **ATR** checks that the structural target fits inside the day’s range. A pretty intraday breakout that is already extended into the upper lines, under the 50-day, is a short in his examples (TSLL, YM), not a buy. Size goes up only after **3 of 5** days are green for a couple of weeks, about **10%** more shares per **10%** of account growth on his $5,000 mock-up.

Video 2 drops the moving average and the ATR. It waits on one **15-minute** chart for a three-candle turn at those same lines, enters on the push through the middle candle, and aims back at the opposite line. The on-screen session is two tickets, not a verified $10,000 month: AAOI narrated as almost $3,000 into the upper seller, GGLL narrated as almost $2,000 given back.

## Boundary

Nothing here is a Jarvise order, a paper rule, or a doctrine extract. Do not import the 50-day bias, the four-line day-trade, the sneaky-pivot entry, the share-scaling ladder, or the futures-first preference into the analyzer, the approval queue, or live trading.
