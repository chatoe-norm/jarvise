# Walkthrough: Humbled Trader — AI on her own P&L

**Research overlay — not trading doctrine.** Do not index this page into Qdrant `jarvise_doctrine`. Do not silent-merge it into SQLite or the paper ledger. Do not retune analyzer floors **0.55 / 0.70**, do not change `same_side_hold`, and do not set `JARVISE_LIVE_TRADING=true`. This video is a US equity day-and-swing review. It does not place an order and it does not feed Jarvise market truth. The five review questions adapted onto Jarvise paper metrics, and the MVP goals, are in [2026-10-14-youtube-notes-vs-jarvise.md](2026-10-14-youtube-notes-vs-jarvise.md).

Compiled **2026-10-14** from English YouTube auto-captions. Speaker is **Humbled Trader**. She says she has traded for more than **10 years**, lost money for the first **3**, and lost about **$20,000** before she became profitable. The description says she is not a financial adviser. Affiliate links in the description (brokers, TradingView, Trade Ideas, BetaPro, her own tools) are not part of the method.

Sits beside [2026-10-14-claude-tradingview-mcp.md](2026-10-14-claude-tradingview-mcp.md). That note is BoomBigNose asking an MCP whether to buy. This note is the opposite use: upload a broker CSV and ask what already worked.

| What | How to read it |
| --- | --- |
| Title “$376K in 2026” and the spoken “around $400,000 in verified profits” | Her claim. She points at a Kinfo P&L link. This note does not audit it. |
| Overnight and swing longs beat same-day longs; same-day shorts beat swing shorts; focus group **72%** vs avoid group **35%** | What she says Claude returned from her year-to-date CSV. Not a Jarvise sample. |
| Ticker “CHOR” and “SMDK” | Caption noise around Sandisk and a third memory or semiconductor name. Kept as spoken. |

Source: [I made $376K in 2026... Here’s how I use AI for Trading](https://www.youtube.com/watch?v=zU0i9rBbEkI) (~12:32). Chapters in the description: intro, use case 1 at 1:22, what the model found at 2:47, use case 2 at 4:41, sponsor at 5:32, the 72% group at 7:39, use case 3 at 10:19.

## Walkthrough

**0:00–1:22.** She is up around **$400,000** in verified profits this year. She says she did not get there by asking ChatGPT whether to buy AMD tomorrow. She used AI on her own fills: which strategies worked, which stocks to focus on, when to size up, and which trades wasted time and capital. AI did not create the edge.

**1:22–4:41. Use case 1 — P&L dashboard.** Export the broker statement as **CSV** (year-to-date trading activity from one brokerage). Drop it into **Claude** or **Codex**. She uses Claude. The prompt, which she says is copied on her blog, asks for five cuts:

1. Holding period: day trades, overnight, longer swing.
2. Stock selection: best and worst names.
3. Biggest winners versus biggest losers.
4. Entry and exit patterns.
5. Any other hidden link.

What she says came back:

- Overnight and swing trades beat her same-day longs. Her favorite trend-following long often keeps going into day two, so the day trade becomes the better overnight swing.
- Same-day shorts beat swing shorts. She treats that as a reason to stop swinging shorts.
- Strongest results are high **ADR** names with strong liquidity. ADR is average daily range as a percent. She wants **above 4%**. Under **4%**, and especially under **3%**, the range is too small no matter the price.
- The high-ADR, high-liquidity cluster on her dashboard is semiconductors, memory, and storage. Winners she names: **SanDisk**, **MU**, and a third name the caption renders as **CHOR** (earlier “SMDK” for SanDisk).
- Outside those momentum sectors this year, Claude told her she loses money or at best breaks even.
- More strategies and more trades are not the path. Keep the few things that already pay and stop funding the rest.

**4:41–7:39. Use case 2 — size the winners, plus a sponsor.** The uncomfortable finding: she put nearly the **same capital** on her best trades and her worst trades. Edge only matters if risk changes with it. She would put more risk on the biggest winning type: semiconductors and memory (**MU**, SanDisk, CHOR) when volatility, liquidity, and sector momentum are all there. The middle of this section is a **BetaPro** sponsorship. If bullish on semiconductors, their **3x semiconductor daily leveraged bull** ETF (holdings she names include Nvidia, AVGO, AMD, MU). If bearish, the **−3x** daily inverse. She says leverage is not conviction. These products **reset daily**, losses compound as fast as gains, and they are short-term trading tools, not investments to hold.

**7:39–10:19. The 72% group.** Same size on winners and losers meant strongest and weakest ideas were treated as equal. Grouping the trades that had higher volatility, stronger liquidity, larger market cap, and sector momentum produced a **72%** historical win rate. Trades outside that set, the “avoid” group, were **35%**. Multi-day breakouts of high-ADR names in the hot sector are her best trend-following longs and overnight swings, so those are where she says she should size up. Every weekend she scans high range, high volume, daily-chart setups and emails a free game plan. She then warns that a win rate is not a forecast. The dashboard only decides more size, less size, or no trade. She calls the sizing rule **exponential bet sizing**: size the A+ setup, trade much smaller on average setups. If every trade looks like an A+, the grade is broken. A+ names should not show up every day. She does not have to trade every day, week, or month. More risk waits until the setup is actually there.

**10:19–end. Use case 3 — faster research, not a buy button.** Do not ask the model what to buy today. Use tools to shorten the morning game plan and surface a missed catalyst. Two tools:

- **TradingView Co-Pilot**, a browser extension on paid TradingView plans. It screens for her conditions and indicators, shows sector momentum, and can read charts already on the watchlist.
- **tenji.io**, a swing-research tool she says she is building. It gathers news, catalysts, trending sectors, and market signals, and looks for links between articles. She says a beta is free for a limited time.

AI supports the decision. She still validates the information, understands the setup, and manages risk. AI will not make someone profitable. She points at other videos for Claude, Codex, and broker APIs. Those connections are not shown here.

## Data

- Claimed 2026 result: title **$376K**, speech **about $400,000** verified. Career: **10+ years**, unprofitable for **3**, about **$20,000** lost before the turn.
- Input: one brokerage **CSV** of year-to-date activity, uploaded to **Claude** (Codex is the alternative she names).
- Five requested cuts: hold time, stock selection, winners versus losers, entries and exits, hidden links.
- ADR filter she states: prefer **above 4%**. Treat **under 3–4%** as too tight.
- Named winners: **MU**, **SanDisk**. Third ticker is unclear in the caption.
- Sectors that paid her this year: semiconductors, memory, storage. Outside that, lose or break even.
- Win rates from her own file, as spoken: focus group **72%**, avoid group **35%**.
- Position-size finding: best and worst trades received about the **same capital**.
- Leveraged-ETF sponsor: BetaPro **3x** semiconductor bull and **−3x** bear, daily reset. Holdings she lists: Nvidia, AVGO, AMD, MU.

## Knowledge

- The model is a reader of her ledger, not a source of the next ticker.
- A day-trade long that is really a day-two continuation should be judged as an overnight swing, because that is where her P&L was better.
- Shorts in her file paid as same-day trades and not as swings.
- Range without liquidity is not the cluster. She wants high ADR and strong liquidity and, in the 72% group, larger market cap plus sector momentum.
- Equal size across unequal ideas is the leak. Win rate still does not pick the next trade. She executes. The dashboard only sets size.
- An A+ that appears every day is a grading problem.

## Technical skills

1. Download the broker activity file as CSV.
2. Upload it to Claude or Codex and ask for the five cuts above. She says the wording is on her blog. This clip does not read the prompt aloud, so it is not reconstructed here.
3. Split results by hold time before splitting by ticker.
4. Measure ADR as a percent of daily range. Keep names above about **4%** when the goal is her momentum long.
5. Tag sector (semiconductor, memory, storage versus everything else) and liquidity.
6. Build three buckets from that tag: more size, less size, no trade.
7. For the morning scan, TradingView Co-Pilot on a paid plan, or her tenji.io board for swing catalysts. Both still get a human check.

## Tactics

- Do not ask the model “what should I buy today,” and do not ask it whether to buy AMD tomorrow. That is the use she says did not make the year.
- Trade fewer setups. Drop swing shorts if the file says they do not pay. Keep same-day shorts if they do.
- Let a strong trend-following long become an overnight swing when day-two continuation is the historical edge.
- Size up only when volatility, liquidity, market cap, and sector momentum line up. Size down or skip the rest.
- Wait. The best setup is rare. Flat days, weeks, and months are part of the rule.
- Leveraged sector ETFs, if used at all, are a one-day expression of a view she already has. They are not a substitute for that view, and she says not to hold them as investments.

## Boundary

Do not import the 72% bucket, the 4% ADR cut, the semiconductor-only focus, or the 3x ETF into the analyzer or the paper book. Jarvise does not read a broker CSV through Claude, and it does not take TradingView Co-Pilot or tenji.io as numeric truth. Live trading stays off.
