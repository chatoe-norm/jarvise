# Jarvise Mac handoff — 2026-10-28

Continue on the MacBook from the Windows session (2026-10-24 to 2026-10-28). `AGENTS.md` and `PROJECT_CONTEXT.md` are still the source of truth for rules. Read [`2026-10-27-claude-handoff.md`](2026-10-27-claude-handoff.md) first: it covers PRs #223–#233, the stop-fill research, and the checkpoints. This page adds what changed after it and what the Mac needs.

## 1. Mac setup

| Item | Value |
| --- | --- |
| Remote | `chatoe-norm/jarvise`, cloned over SSH host `github.com-chatoe-norm` |
| `gh` account | **`chatoe-norm`** only |
| VPS | Hostinger `srv1811101`, public `76.13.218.48`, Tailscale `100.89.171.36` |
| Command Dashboard | `http://100.89.171.36:8080` (Tailscale only) |
| Deploy | Push or merge to `main`, then the self-hosted runner runs `infra/deploy/vps-deploy.sh` |

```bash
git clone git@github.com-chatoe-norm:chatoe-norm/jarvise.git
cd jarvise && git checkout main && git pull
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# checks on macOS (the Windows paths in CLAUDE.md become):
ruff check src tests && ruff format --check src tests && python -m mypy src && python -m pytest
```

SSH alias, copied from the Windows `~/.ssh/config`. Copy the private key `id_ed25519_jarvise` to the Mac by your own secure means. It is never committed.

```text
Host Jarvise-VPS jarvise-vps
    HostName 76.13.218.48
    User root
    IdentityFile ~/.ssh/id_ed25519_jarvise
    IdentitiesOnly yes
```

Claude memory does **not** sync between machines. On Windows it lives in `~/.claude/projects/d--Projects-jarvise/memory/`. The two facts it held are in §4 below.

## 2. What shipped after the 2026-10-27 handoff (all on `main`, deployed)

| PR | Change |
| --- | --- |
| #234 | Morning batch moved to **08:20** Bangkok, after the 08:00 paper-run, so the analysis is fresh. `same_idea` counts only `sent`, `taken`, or `skipped` rows. Before this fix, no overnight signal reached the owner. |
| #235 | Trade assistant PR C. See the list below. |
| #236 | `AGENTS.md` memory (docs). |
| #238 | One shared leverage for a batch: `L = ceil(Σ notional / (availableBalance × 0.95))`, at most 5x, and the lowest-confidence idea is dropped as `margin_cap` above that. Messages show `margin ≈`. The default `risk_pct` is now **1%** for new databases only; the live row is changed by the owner from the menu. `dnspython` 2.9.0. |

PR C (#235) added:

- a Telegram long-poll listener as a daemon thread in `jobs`. It fails closed if `TELEGRAM_CHAT_ID` is empty.
- a skip button, a 5-button menu, the risk % setting, and an on/off switch;
- a virtual track that uses as-of analysis (`timestamp + 5h`);
- the Monday 08:30 weekly summary;
- the losing-streak pause: notify and pause new signals until 08:20 after 3 losing trades (fills within 120 s are grouped) or a week loss above 6%. The position watch keeps running.

n8n workflows (Asia/Bangkok):

| Workflow | Schedule |
| --- | --- |
| `jrvsAssistSignal4h` | `25 0,4,8,12,16,20 * * *` |
| `jrvsAssistMorning1` | `20 8 * * *` |
| `jrvsAssistWatch5m` | `*/5 * * * *` |
| `jrvsAssistWeekly1` | `30 8 * * 1` |

## 3. Open now

1. **PR #239** (`fix/assist-on-demand-reasons`) is reviewed and approved by Claude and waiting to ship.
   - The "🔎 ตอนนี้มีเหรียญไหนน่าเข้า" (anything to enter now) button no longer applies the push-dedupe rules (`same_idea`, sent/held risk slots).
   - A still-valid idea shows "ส่งแล้วเมื่อ HH:MM" (sent at HH:MM), or "ไม่แนะนำไล่" (do not chase) if price moved more than 0.3R past the earlier entry.
   - When there is no idea, it lists a Thai reason per symbol.
   - Root cause, checked on the VPS at 2026-10-28 10:02: ADA short 0.75 was hidden as `same_idea`, and the other six symbols were flat.
   - Ship it with fdpy, then press the button in Telegram.
2. **The owner sets risk to 1%** in Telegram (⚙️ menu). The live row is still 2.0.
3. **Dependabot PRs #217–#222 and #237** are pending. Batch them into one PR when the assistant is quiet.
4. Small follow-ups that do not block anything:
   - The assist-signal HTTP payload lacks `available_usdt`.
   - On-demand reasons that are not mapped (for example `size_failed`) show in English.
   - The Dashboard approval card still shows an Approve button on `expired` rows.
   - The "กฎการเทรด" (trading rules) panel can quote a long-entry rule on a short idea.

## 4. Facts that were only in Claude's Windows memory

- **CoinGlass.** Never put `COINGLASS_API_KEY` on the VPS.
  - The owner's key plan answers "Upgrade plan" on every endpoint, and the OI and funding paths are stale v3 paths that return 404.
  - `providers/derivatives.py` switches to CoinGlass whenever the key is set, with no Binance fallback.
  - Derivatives is a required stream, so a failed fetch is critical and engages the kill-switch with force flat. The kill-switch never clears itself.
- **Binance.**
  - Master account. USDⓈ-M futures was activated 2026-10-26, one-way mode, with a 100 USDT futures wallet.
  - Key **"Jarvise-2"**: Ed25519, Enable Reading only, IP-locked to `76.13.218.48`. Its PEM is at `/opt/jarvise/secrets/binance-ed25519-prv.pem` on the VPS, read through `BINANCE_API_KEY` + `BINANCE_API_PRIVATE_KEY_PATH`.
  - The old key "Jarvise" lost its PEM (cause unknown) and was deleted.
  - Spot balance sync works again from 2026-10-26.
- **First real signals.**
  - 2026-10-27 about 21:00: ADA short and DOGE short, about 2 USDT at stop each. XRP was skipped by `risk_cap`.
  - Following them literally needed 119.55 USDT of margin, more than the wallet held. That led to #238.
  - The owner had not entered any trade by 2026-10-28.

## 5. Checkpoints (unchanged)

| Date (about) | What | Command (jobs container) |
| --- | --- | --- |
| 2026-11-02 08:30 | First weekly assist summary | arrives on Telegram |
| 2026-11-17 | Shadow-veto window, day 30 | `jarvise paper shadow-veto score --json` |
| 2026-11-25 | Forward H3, first look | `jarvise paper forward-h3 score --json` |
| 2026-12-17 | Shadow-veto window, day 60 (an extension needs the owner) | same score command |

Frozen while H3 runs: `strategies.py`, `replay.py`, `stops.py`, `profit_lock.py`, `jarvise_paper/engine.py`.

## 6. Working agreements

- Claude reviews, audits, does research, and makes small fixes when finding the bug was the hard part (for example #234). Cursor implements features and ships with `fdpy`.
- Claude reviews every feature PR before merge. Read-only VPS checks go through `ssh Jarvise-VPS`.
- Claude never handles API key values. The owner pastes them with `read -s`.
- Claude never places trades or moves funds. It can open Binance pages and fill non-secret fields. The owner does 2FA and confirms.
- Keep `AGENTS.md` memory edits out of feature PRs. Ship them as separate docs PRs.
- Replies to the owner are in Thai and short.
