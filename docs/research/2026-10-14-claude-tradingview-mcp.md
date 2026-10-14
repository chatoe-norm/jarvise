# Walkthrough: Claude Code + TradingView MCP (BoomBigNose)

**Research overlay — not trading doctrine.** Do not index this page into Qdrant `jarvise_doctrine`. Do not silent-merge it into SQLite or the paper ledger. Do not retune analyzer floors **0.55 / 0.70**, do not change `same_side_hold`, and do not set `JARVISE_LIVE_TRADING=true`. TradingView stays a research overlay. Jarvise numeric truth stays Binance and CoinGecko ingest. This video does not place an order. How this clip may inform the paper review, and what it must not change, is in [2026-10-14-youtube-notes-vs-jarvise.md](2026-10-14-youtube-notes-vs-jarvise.md).

Compiled **2026-10-14** from Thai YouTube auto-captions. Speaker is **BoomBigNose**. Captions garble Claude as “คอร์ส”, OpenClaw as “Open Core / Open Call”, and TradingView as “เทรด view”. The install block is “pause and copy the screen.” The transcript does not contain the MCP JSON or the package name, so this note does not invent that command.

Sits beside [2026-10-14-binance-futures-10-to-50.md](2026-10-14-binance-futures-10-to-50.md), [2026-10-14-rumers-3r-and-sneaky-pivot.md](2026-10-14-rumers-3r-and-sneaky-pivot.md), and [2026-10-14-humbled-trader-ai-pnl.md](2026-10-14-humbled-trader-ai-pnl.md). Those notes are different authors and different methods. This page does not change them.

| What | How to read it |
| --- | --- |
| Free, open source, about 5 minutes, no TradingView API key, six unnamed historical methods, Yahoo plus Reddit plus technicals | His claims. The six methods are never named. |
| NVDA buy at 182.2, later 183 and a live **183.91**; BTC around **70,000**; weekly brief dated **10 April** | What he reads on screen. Not an audited fill. No size and no stop are spoken. |

Source: [เทรดด้วย AI แบบฟรี! สอนใช้ Claude Code + TradingView MCP ครบจบในคลิป (แถม OpenClaw)](https://www.youtube.com/watch?v=TilUhQ1t6Ak) (~18:37).

Description links he actually posts:

- [Claude Code quickstart](https://code.claude.com/docs/en/quickstart)
- [tradingview.com](https://www.tradingview.com/)

Chapters in the description: intro, overview at 0:55, Claude Code at 4:10, Skool promo at 6:51, MCP try at 8:05, Hostinger at 9:56, OpenClaw at 10:15, outro at 18:06. The Skool and Hostinger segments are course and affiliate pitches (`BOOMBIGNOSE`). They are not part of the chart method.

## Walkthrough

**0:00–0:55.** He opens on a free stack: Claude connected to a TradingView MCP that he says can pull investing data in real time, news from Yahoo and Reddit, and technical chart work. He says yesterday’s NVIDIA read was a good buy and that the same setup also runs on OpenClaw, including chart images sent into a chat. He points people who have not installed Claude or OpenClaw at this clip.

**0:55–4:10.** The MCP is the add-on. Claude writes the summary, and OpenClaw can forward that summary to a Telegram bot. He contrasts it with older workflows: about a **5 minute** install, free or open source, **6** ways to look at historical strategy data, live news from foreign sites, real-time quotes, and **no TradingView API key**. Install is one copy-paste that Claude applies. The right-hand screenshot is a Telegram thread, via OpenClaw, with a real-time chart pull and, he says, no API charge. The only result he offers is NVIDIA: yesterday the assistant said buy at **182.2** (an early caption “82.2” is the same line, garbled). He says the stock is higher today.

**4:10–6:51.** Two places to run it. First is Claude Code on his machine. He shows the free app, asks for NVIDIA, and notes the cash session is still closed so that price is not moving. He switches the question to **BTC**. He tells viewers to ask in **English** so the ticker parses. Claude Code on his Windows box is the `claude` command; on a Mac it is Terminal. He wants a project folder first (he suggests something like a Desktop folder named for this TradingView chat), then Claude inside that folder. `/mcp` shows whether the TradingView server is already connected. The config itself is on screen. He tells people to pause and copy it. He does not read the JSON out loud.

**6:51–8:05.** Skool “BoomBigNose Plus” promo: Claude Cowork, office projects, n8n templates, and remote install help. Skip for the method.

**8:05–9:56.** Back in Claude he asks for **BTCUSD** and whether to invest. He reads about **70,000**. He says yes when Claude asks permission to call the MCP. The tool returns a live quote, then a last-step summary of buy or sell. He says the coin is moving, so the number can change, and that the same prompt can be left to run on a schedule.

**9:56–10:15.** Hostinger one-click OpenClaw, with his discount code, so the PC does not have to stay on. That path is his affiliate install. It is not a change to the Jarvise image pin.

**10:15–18:06.** OpenClaw install is another pause-and-paste. The only spoken edit is to replace the placeholder with a Telegram bot token. In Telegram he searches **BotFather**, sends `/newbot`, names the bot, copies the token back into OpenClaw, and presses Enter. He says the TradingView MCP skill then appears, still free, still real time. He also says OpenClaw can cron the same pull. He returns to the NVIDIA anecdote: yesterday’s buy, a figure of **182**, and today **183**, and he waits for the open. He then uses a more advanced prompt: a **weekly brief** focused on NVIDIA. The brief on screen is dated **10 April** (year not spoken) and he says it is real time. He says the MCP exposes many algorithms the model can be told to use. He does not name them. A simple phone prompt follows: current NVIDIA price, and should he buy. The reply he reads is **183.91** dollars, which he says matches the chart. A second use case asks Reddit and foreign social media for the mood. He reads that back as a **bear market / still consolidating**, not a bull market. He then asks for a **complete trading plan** and to send it on Telegram **every morning at 09:00**, which he says can be the cash open. He refreshes the OpenClaw chat, says the cron is enabled, and waits for the NVIDIA plan to land in Telegram. Last requests: an **NVDA snapshot image**, then the same snapshot with the symbol changed to **BTC**. He says those images come back close to real time. Outro is a subscribe ask.

## Data

- Install time he states: about **5 minutes**. No TradingView API key.
- Sources he names: **Yahoo**, **Reddit**, other foreign news, technical analysis, and **6** historical-strategy methods. The six are not named.
- NVIDIA anecdote: buy at **182.2**, later spoken as **182**, then **183**, then a live quote of **183.91** that he says matches the screen.
- **BTCUSD** around **70,000** while US stocks are still closed, so NVDA is not ticking in that first Claude Code pass.
- Weekly brief date he reads: **10 April**. Social tone he reads: bear or consolidation, not a bull market.
- Schedule he asks for: a complete trading plan on Telegram at **09:00**. He says the job is enabled.
- Outputs he shows: text summary, weekly brief, and a separate **snapshot image** for NVDA and for BTC.
- No share count, no dollar risk, no stop, and no broker ticket.

## Knowledge

- Two surfaces share one MCP. Claude Code is the desktop terminal. OpenClaw is how the same questions run from Telegram, including from a phone.
- Claude does not replace the data source in his demo. The MCP returns the quote, the news, and the chart image. Claude writes the buy/sell sentence afterward.
- A snapshot is its own request. It is not the same call as the text brief.
- The only track record on camera is one NVIDIA day plus a live price check. He presents that as a beginner’s reason to try the tool, not as a tested system.
- A hosted OpenClaw is optional so the machine can sleep. Jarvise already runs its own OpenClaw pin. This clip’s Hostinger click is a different install.

## Technical skills

1. Install Claude Code from the quickstart, Mac Terminal or Windows Command Prompt.
2. Create a project folder and start `claude` inside it.
3. Run `/mcp` and confirm the TradingView server is listed.
4. Paste the on-screen MCP config. Say yes when Claude asks to use the tool.
5. Prompt in English. His pattern is the symbol, the realtime price, then whether to buy.
6. In Telegram, BotFather and `/newbot`. Paste that token over the placeholder in the OpenClaw prompt and press Enter. The TradingView MCP skill should show up.
7. Ask OpenClaw for a cron. His example is 09:00 and a complete trading plan. Refresh the OpenClaw chat to see the saved job.
8. Ask for `snapshot image` when the deliverable is a chart picture. Change the symbol in that same request (he swaps NVDA for BTC).

## Tactics

- Price first, then “should I buy”, then a Reddit or social pass, then a weekly brief or a complete trading plan.
- Put the plan on a morning cron instead of watching the terminal.
- Leave the chart work inside the MCP. He never types a broker order.
- Use the image when he wants to see the chart, and the text when he wants the buy/sell line.

## Boundary

Do not add this MCP as market truth. Do not reinstall Jarvise OpenClaw from the Hostinger affiliate, and do not treat `BOOMBIGNOSE` as a Jarvise deploy step. The VPS image stays `ghcr.io/openclaw/openclaw:2026.9.8`. Do not enable live trading, and do not turn the NVIDIA anecdote or the 09:00 Telegram plan into an analyzer rule.
