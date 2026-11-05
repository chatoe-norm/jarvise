# Jarvise handoff — 2026-11-05 dashboard audit

`AGENTS.md`, `CLAUDE.md` and `PROJECT_CONTEXT.md` remain the source of truth for rules. This page records the 2026-11-05 dashboard audit (read-only, against `main` at `35d7553`), what branch `fix/dashboard-audit-2026-11-05` changed, and what is still open.

## 1. Status by item

| # | Item | Severity | Status on the branch |
| --- | --- | --- | --- |
| 3.1 | FLAT size-0 cards enqueued and still approvable | Medium | **Fixed** (server + UI) |
| 3.2 | ETH `trend_down` read as a contradiction | Medium | **Fixed** (copy only; formula untouched) |
| 3.3 | Two EV numbers (all-source/all-time vs auto/30d) | Medium | **Fixed** (labels + latch sample exposed) |
| 3.4 | Exchange page is Spot-only, live path is futures | High | **Partly fixed** (labels, assist flag, checklist). Futures wallet card open (J) |
| 3.5 | AVAX "28395 น.", inactive coins on load | Low | **Fixed** |
| 3.6 | ADA short at 04:25 Bangkok left no `assist_signal` row | Medium | **Log added**; root cause needs the VPS (§3) |
| 3.7 | Gmail + notebook UUID on the dashboard | Medium | **Fixed on the dashboard**; repo is **public**, so the wider leak is open (§3) |

## 2. What changed

### 3.1 FLAT no-op

- `src/jarvise_paper/approval.py` `enqueue_approval()`: `action == "flat"`, no paper position, and no pending card for that symbol/timeframe → returns `{"ok": True, "skipped": True, "reason": "flat_no_position"}` and writes nothing. Runs **after** market_safety / `_risk_breach` / `_portfolio_breach`, so their kill-switch behaviour is unchanged.
- If a pending card already exists for the pair, FLAT still overwrites it (old behaviour), so a stale long/short card is never left pending.
- `expire_approvals()`: `resolve_reason = "timeout_noop"` when there is no position. `timeout_flat` now means a position was actually flattened; `timeout_hold` unchanged.
- `src/jarvise_paper/cli.py`: `flat_no_position` is not an error. The audit's suggested fix alone would have put every FLAT pair into `errors` and made `paper run` return `ok: false`.
- `web/src/components/ApprovalQueue.tsx`: the no-op `<details>` table offers "ดูคำแนะนำ" only.
- Outcome counting: a FLAT analysis with nothing queued is `not_queued`, which the UI already labels "ไม่มีไม้ให้ปิด". No new no-op approvals reach "ถือไม้เดิม".
- Not changed: `apply_signal()` still writes cash/equity and a `performance_risk_metrics` row on a no-op FLAT approve. After this branch that is reachable only through the API for cards created before deploy (they expire within 60 min as `timeout_noop`).

### 3.2 Regime copy

- `src/jarvise_analyze/engine.py` thesis now reads `Trend down: EMA20 <v> < EMA200 <v>` (strings only; `analysis_id` does not hash the thesis, condition and floors untouched).
- `src/jarvise_paper/recommendation.py` `_what_happened()`: the regime line adds `กรอบใหญ่ (HTF) <regime>`. `htf_regime` is **not a DB column**; it is parsed from the thesis (`HTF=…` / `HTF … confirms|conflicts`).
- Same function now takes RSI/ATR/volume from `signal_candle` when present, so a bar closing inside a card's life cannot mix bars.

### 3.3 EV labels

- `feedback.py` `read_ev_gate_latch()` returns `sample_n` / `sample_ev`; `auto_ev_gate_status()` exposes them as `latched_n` / `latched_ev`. `blocked` is computed exactly as before.
- Gate copy (`web/src/lib/copy.ts`): "EV ของไม้ auto 30 วัน ตอนนี้ N=…, EV … · ค่า ณ ตอนล็อก N=…, EV …".
- Home and Paper KPI: "EV ต่อไม้ (ทุกแหล่ง, ตลอดอายุ)", hint `n=…`.
- Card evidence line: "EV ของไม้ auto 30 วันยังติดลบ".

### 3.4 Exchange page and checklist

- `exchange_payload()` adds `assist_execute` (`off`/`testnet`/`live`/`unknown`). Spot balance code unchanged.
- `ExchangePage.tsx`: a notice shows both flags side by side and states that `JARVISE_LIVE_TRADING=false` does not stop assist futures; Spot cards and the guide are titled as Spot-only.
- `docs/ops/live-enable-checklist.md`: two-path header; §1 step 5 says futures never goes on the spot key; assist section adds One-way, Single-Asset, Isolated + leverage (verified against `execute.py` `assert_account_modes`, `set_isolated`, `set_leverage`); "Current VPS status" refreshed from the repo with the dated history kept.

### 3.5 Data age

- `formatAgeMin()` in `web/src/lib/utils.ts` (minutes < 60, hours < 48 h, else days). Inactive coins show "ไม่ได้ sync (ปิดใช้งาน)". The universe filter defaults to "active".

### 3.6 Assist scan log

- `scan_signal_candidates()` logs one INFO line: `assist scan: candidates=N skipped=[SYMBOL:reason,...]`. `jarvise_assist` INFO already goes to `docker compose logs jobs` (`jobs.py` `configure_process_logging`). Floor, sizing and `same_side_hold` untouched.

### 3.7 PII on the dashboard

- `/api/rag/sync` returns `notebook_alias: "jarvise"` instead of the notebook UUID and URL.
- `CryptoUniverseCard.tsx`, `QdrantTradingRulesCard.tsx`: masked account, alias instead of UUID, link to `https://notebooklm.google.com/`.

## 3. Open for the owner

1. **Repo is PUBLIC** (`gh repo view` → `visibility: PUBLIC`). The Gmail address and notebook UUID are also in `README.md`, `AGENTS.md`, `PROJECT_CONTEXT.md`, `config/notebook.json`, `data/analytics/*`, `docs/research/*`, `docs/superpowers/*`, `.cursor/skills/jarvise-notebook/SKILL.md`, and in git history. The VPS public IP is hardcoded in `web/src/pages/ExchangePage.tsx` (Host VPS IP card). Editing files does not remove history. Decide: make the repo private (simplest), or keep it public and rewrite history. Check the notebook's own sharing setting is "restricted" either way.
2. **ADA short, 04:25 Bangkok run.** Still unexplained. Read-only on the VPS:

   ```bash
   cd /opt/jarvise
   docker compose logs jobs --since 20h 2>&1 | grep -E "assist_signal|record_blocked_signal failed|below_min_notional|size_failed|no_binance_auth|futures_down"
   docker compose exec redis redis-cli GET jarvise:assist_signal:last
   ```

   After deploy, every run prints the `assist scan:` line, so the next miss answers itself.
3. **J — read-only futures wallet card** on the Exchange page. Not started; changes `exchange_payload()` and adds a futures read call. Hold until item 2 is answered.
4. ~~AGENTS.md after merge.~~ Done: shipped as [PR #284](https://github.com/chatoe-norm/jarvise/pull/284) (merge `040ade8`, Deploy 2026-11-05T03:04:37Z, `live_trading` false, `assist_execute` off); the Learned Workspace Facts bullet was added in the follow-up docs PR.

## 4. Checks run on the branch (Windows, local)

- `ruff check src tests`, `ruff format --check src tests`, `mypy src`: clean.
- `pytest`: 917 passed, 1 skipped.
- `web`: `npm run lint` clean, `npm test` 60/60, `npm run build` ok.
