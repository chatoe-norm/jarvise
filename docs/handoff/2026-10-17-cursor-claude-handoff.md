# Jarvise handoff — 2026-10-17 (Cursor IDE + Claude Code in VS Code)

For the next agent picking up in **Cursor** (Grok/Composer for implementation) or **Claude Code in VS Code** (review, synthesis, hardest tasks). Read `AGENTS.md` and `PROJECT_CONTEXT.md` first; this note covers what changed on 2026-10-17 and what is still open. It supersedes the "next steps" in [2026-10-17-claude-handoff.md](2026-10-17-claude-handoff.md) (keep that one for the #162–#165 background).

## State right now

- `main` = `99fa702` (PR #178). VPS Deploy green, `healthz` → `{"ok":true,"paper_only":true}`.
- `JARVISE_LIVE_TRADING=false`. Kill switch off. No live keys in use.
- Paper: closed `auto_claude` n=10, EV −0.65/trade, win rate 20% → soft EV gate **blocks** auto-decide, as designed. Floors 0.55/0.70, 1.5× ATR stop, `same_side_hold`, analyzer formula: **unchanged and must stay unchanged**.
- VPS universe `paper_liquid` (7 active: BTC/ETH/SOL/BNB/ADA/DOGE/XRP), timeframe 4h, open-position cap 4 (env only).

## Shipped today (all merged with green CI and deployed)

| PR | What |
|----|------|
| [#166](https://github.com/chatoe-norm/jarvise/pull/166) | Safety: approve/reject return the real result (409 on refusal); risk caps reject ≤ 0; jobs proxy sends `X-Jarvise-Token`; `useConfirm` dialogs on state-changing Ops actions; top-level ErrorBoundary; service worker v2 (network-first shell, cache-first only hashed `/assets`, `sw.js` no-cache). |
| [#167](https://github.com/chatoe-norm/jarvise/pull/167) | Honest data: no fake Market Context fallbacks; F&G/Altseason trade advice removed (`ContextOnlyNote`); 30 s dashboard polling; FLAT no-op cards grouped; exit labels from stored reasons (`exitKind`), `loss_lesson.by_exit.manual`; owner copy remaps "doctrine" → กฎการเทรด; Ops RiskMonitorCard; model-routing card labelled as an unwired plan; Exchange page shows real errors. |
| [#168](https://github.com/chatoe-norm/jarvise/pull/168) | Mobile: 44 px tap targets (`Button sm` = `h-11 sm:h-8`), stacked approval cards, pinch zoom allowed; dead alias files removed; `tsx` dev dep; clipboard fallback. |
| [#176](https://github.com/chatoe-norm/jarvise/pull/176) | Universe remove/reset are point-in-time (`delisted_at = now`, re-add opens a new interval; `reset_universe_members`); sync/backfill report per-symbol `synced/warnings/failed` (exit 1 = warning, exit 2/exception/no candles = failed); lazy routes for Paper/Decisions/Exchange/Ops (first-load JS 1,007 KB → 437 KB). |
| [#177](https://github.com/chatoe-norm/jarvise/pull/177) | Hotfix: `web` container gets the same `JARVISE_PAPER_UNIVERSE` / `JARVISE_PAPER_TIMEFRAME` as `jobs` (Ops Universe edited `paper_core` before). Guarded by `tests/test_compose_env.py`. |
| [#178](https://github.com/chatoe-norm/jarvise/pull/178) | News digest "4 of 5 categories fail": GDELT queries now parenthesize OR terms (unparenthesized OR returns plain text → JSONDecodeError), requests paced 8 s with 429 retry; Google News RSS fallback per theme once GDELT throttles; new job `POST /jobs/news-reaction` + n8n workflow `jrvsNewsReact0749` at 07:30/19:30 ICT. Home shows a small note when a theme came from Google News. |

VPS steps done after #178: `n8n import:workflow --input=/workflows/jarvise-news-reaction.json`, `n8n publish:workflow --id=jrvsNewsReact0749`, `docker compose restart n8n`; one manual run → exit 0, `errors: []`. GDELT is currently throttling the VPS IP, so all four themes come from Google News; GDELT is tried first each run.

## Open items (owner decisions or ops; do not act without the owner)

1. **Exchange page `error: auth`.** Keys are present but Binance rejects them. Most likely the read key's IP whitelist still has the old host; it needs `76.13.218.48`. Read key = `BINANCE_API_KEY` + `BINANCE_API_SECRET` (or `BINANCE_API_PRIVATE_KEY_PATH` [+ `_PASSPHRASE`]), "Enable Reading" only. Trade keys `BINANCE_TRADE_*` stay unset while live is off. Check (read-only): `docker compose exec jobs jarvise exchange check-key --json`. Never SSH-edit `/opt/jarvise/.env` for the owner.
2. **OpenClaw news digest stale since 2026-10-14.** `data/openclaw/workspace-ingest/digests/` only has `2026-10-14-13.md`. n8n `jrvsNewsIngest0719` is active and OpenClaw receives the hook, but the `ingest` agent writes nothing. Logs show memory pressure and an invalid `trade-bot` SKILL.md frontmatter. Not investigated yet. The Thai lines on Home come from that old digest; the headline/price-reaction stats are fresh.
3. **Resolved (owner, 2026-10-17).** The VPS universe is the source of truth. The owner manages it on Ops → Universe (`universe_membership` + `JARVISE_PAPER_UNIVERSE`). Presets are starting members; Ops adds and removes, so 7 active symbols with a 4-symbol `paper_liquid` preset is expected. Read the active list with `jarvise universe show`. Do not swap pairs to chase win rate.
4. **Dependabot PRs #169–#175** unmerged. #170 is zod 3 → 4 (major, needs code changes in `web/src` schemas); the rest are minor Python bumps. Not covered by `fdpy`; merge only on owner request.
5. **Host reboot** still pending owner OK (`REBOOT_REQUIRED` since 2026-10-07 apt).

## How to work here

- **Ship:** `fdpy` = commit → PR → green CI → `gh pr merge --merge` → wait for VPS Deploy (self-hosted runner `srv1811101`). `wdpy` = wait for the in-flight queue first. See `.cursor/rules/fdpy-wdpy.mdc`. `fdpy` never enables live, edits VPS `.env`, or merges unrelated PRs.
- **Git/GitHub:** remote `chatoe-norm/jarvise` via SSH host `github.com-chatoe-norm`; `gh` account must be `chatoe-norm`.
- **Commit trailer (Claude):** `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; PR body ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **Checks before a PR:**
  ```powershell
  cd web; npm run lint; npm test; npm run build; cd ..
  .venv\Scripts\ruff.exe check src tests
  .venv\Scripts\ruff.exe format --check src tests
  .venv\Scripts\python.exe -m mypy src
  .venv\Scripts\python.exe -m pytest
  ```
- **VPS (read-only checks):** `ssh Jarvise-VPS`, stack at `/opt/jarvise`. Dashboard `http://100.89.171.36:8080`. Jobs service is Docker-network only (`jobs:8090`, header `X-Jarvise-Token`).
- **New n8n workflow files are not auto-imported:** after deploy run `import:workflow` → `publish:workflow --id=…` → restart n8n. Never import live-reconcile while live is off.
- **Owner copy:** Thai, says **กฎการเทรด** (never "หลักคำสอน" or visible "doctrine"). F&G / Altseason / news are context only, never a trade signal.

## Lessons from this session

- Windows: write regex escapes with the editor, not a Python heredoc (`\b` became a backspace byte once). Redirected stdout on Windows fails on Thai text (cp1252); run such CLI checks in the container.
- `npm install -D …` in `web/` prunes extraneous packages (Playwright vanished). Keep ad-hoc Playwright outside the repo.
- GDELT DOC API: max one request per 5 s per IP; hitting it repeatedly while probing keeps the IP throttled for a long time. Test against Google News or with mocks.
- Compose services that share a setting (universe, timeframe) must get it in both `web` and `jobs`; `tests/test_compose_env.py` checks this now.
