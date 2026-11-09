# Jarvise Mac handoff — 2026-11-09

This continues the Windows session of 2026-11-09 on the MacBook. It covers the trade-assistant safety work (PR #306, PR #307, docs PR #308) and what comes next. `AGENTS.md` and `PROJECT_CONTEXT.md` are still the source of truth for rules. For Mac setup (remote, SSH alias, venv, check commands), use §1 of [`2026-10-28-mac-handoff.md`](2026-10-28-mac-handoff.md). Nothing about the setup has changed.

Claude memory does not sync between machines. The facts the next session needs are on this page.

## 1. State at handoff (2026-11-09 ~12:00 Bangkok)

| Item | State |
| --- | --- |
| PR #306 re-quote cap fails closed | **Merged** `891ceb1`. Deploy run head SHA matched, finished 2026-11-09 10:40:41 Bangkok |
| PR #307 reconcile unknown order outcomes | **Open, not merged.** Head `28914f2`, all CI jobs green. **Next: Claude reviews commits `0b2cd53`, `9564ac8`, `5e1298e`, `28914f2`** |
| PR #308 docs: emergency close waits for a proven stop | **Open, not merged.** Merge right after #307 in the same fdpy, never before or alone |
| PR-C (report truth) and PR-D (card sizing, held list) | Scoped (§5), prompts not written yet |

## 2. Rules agreed with the owner on 2026-11-09

- **Unknown is not failed.** An order whose outcome is unknown is never stored or reported as failed. While any intent for a symbol is `confirmed`, `unknown` or `recovering`, no new entry preview or confirm is allowed for that symbol.
- **Outcome classes** (`src/jarvise_assist/exec_outcome.py`):
  - FILLED
  - NOT_FILLED: an allowlisted Binance code, or order status CANCELED/EXPIRED/REJECTED with executedQty 0
  - UNKNOWN: everything else, including -1001/-1006/-1007, HTTP ≥ 500, and a first-burst -2013
  - The allowlist table is in the PR #307 body; #307 adds -4130.
- **Emergency close after a fill:** close unless at least one stop POST returned a success response or a read-back returned VERIFIED. A success response whose read-back is unreadable stays `unknown` for recovery. This changes the AGENTS.md line-4 rule; PR #308 carries the new text.
- **Recovery** runs every watch pass, independent of watch errors, plus once at listener start. It continues the owner's tapped intent only and never sends a second `-mkt`.
  - **Age ≤ 2 h:** a late fill with no verified stop gets `-slr` at the preview stop. If that fails, run the existing emergency close. *(Owner decision.)*
  - **Age > 2 h:** read-only, no orders. If readable: one true-state message, then `stale`, which releases the block. If unreadable: keep `unknown` (block stays), send one ⚠️, and keep reading.
  - Not-found must be consecutive: ≥ 3 passes spanning ≥ 15 min, reset by any pass that finds the order or a non-flat position.
  - Partial fills stay `unknown` until the order is final. Stops grow place-then-cancel, rotating among the non-open tags `sl0`/`sl1`/`slr`.
  - VERIFIED means the stop qty covers the **current** position qty.
- **Stop read-back** (ours, by client id) checks: symbol, closing side, STOP_MARKET, NEW status, trigger equal to the tick-rounded stop, MARK_PRICE, reduceOnly, positionSide BOTH, quantity ≥ position. Production facts from a GET-only probe: a missing `clientAlgoId` gives HTTP 400 -2013; open = NEW, triggered = FINISHED, cancelled = CANCELED.
- **Exit (`xx:`):** cancel stops/TP only once the re-read position is flat. Confirm each cancel and warn with the client ids when one fails. Retry the cancels only while the position stays flat. If the owner has reopened a position, stop retrying and send one ⚠️ that lists the leftover `jv-` ids.
- **Lease:** each claim carries a token. Renew only while this pass still owns the claim. If renewal fails, stop that intent and send no order.

## 3. How to review PR #307 next

The spec is the four-commit prompt plus its addendum, both sent to Cursor on 2026-11-09. The commit titles match:

1. `0b2cd53` entry closes when no stop is proven:
   - `mark_price` moved out of the placement try; a ConnectError before sending counts as not sent
   - sl1 is always attempted
   - position re-read before sl1, TP and emergency close
   - fixed `test_both_stops_fail_lookups_network_still_emergency_closes` (two real POSTs, expects `-cls`)
2. `9564ac8` recovery reads before it gives up:
   - the > 2 h read-only rule
   - consecutive not-found
   - partial-fill growth with tag rotation
   - stale tests rewritten
   - tests for the entry guard after `stale` and for the watch alert
3. `5e1298e` exit and recovery wiring:
   - unknown exit + NOT_FOUND + open position → `unknown`
   - cancel confirmation and retry
   - recovery in its own try in `src/jarvise/jobs.py`, with a fresh `now_ms`
   - token lease
   - conditional notify
4. `28914f2` truthful move-stop copy:
   - -4130 allowlisted
   - "stop เดิมยังอยู่" only when verified
   - duplicate-cancel only for logged tags
   - `MSG_MODE_UNREADABLE`
   - path-specific copy on `ms:` and `xx:`
   - live-mode stale-card tests

Read the actual diff per commit, not only the stat. Check every changed existing assertion against its stated reason. Watch the watch-alert behaviour: the watch alerts **once per coverage state change**, not every 5 minutes. If no alert would fire, the stale message itself must say the position has no stop.

When #307 passes review, the fdpy order is: #307 → wait for the Deploy run whose head SHA equals the merge SHA → #308.

## 4. Evidence from the VPS ledger (read-only, 2026-11-09)

Claude may read the VPS ledger read-only (owner approval 2026-11-09): SQLite opened with `mode=ro` through `docker exec -i jarvise-jobs-1 python -`. Never write it and never copy it into the repo.

- **Journal 3 of 4 closed trades:** the missing one is the DOGE short closed 2026-11-05 15:11Z. It closed before the journal shipped (fc1cd48, 11-06), and there is no backfill.
- **"Unmatched 2"** are the owner's manual close orders on DOGE and ADA, 2026-11-04.
- **"No track 2"** is a linking bug, not missing signals. The entries DOGE 11-05 12:16Z and ADA 11-06 11:55Z have `signal_id = NULL`, because an `ec:SYMBOL:s` tap never attaches a signal id (`handle_exec_callback`), even though the preview already holds the seen idea's levels.
- **ADA and DOGE ideas of 11-06 were smaller than the same batch's budget.**
  - Both were night-held rows re-quoted by the 08:20 morning batch under the old qty cap that #294 replaced. The data is consistent with this, but it is not fully proven: the held qty was overwritten.
  - This was under-sizing, not a cap leak.
  - The re-quote "first loss" anchor then carried the reduced value for the rest of the 24 h.

## 5. Next work after #307/#308

- **Post-deploy check for #306:**
  - Find the first signal written after 10:40:41 Bangkok. Measurement split key: `COALESCE(sent_at_ms, created_at_ms)` on `assist_signal`, `sent_at_ms` on `assist_track`. A NULL track `sent_at_ms` means unknown version.
  - Confirm its stored stop is tick-rounded the safe way (long down, short up) and equals the card stop.
- **PR-C, report truth:**
  - Link a trade to its signal through the preview's seen idea id. A second trade on the same signal is labelled as a re-entry and excluded from the real-vs-virtual diff.
  - Labels state the unit, the window (Bangkok time) and n for every metric.
  - The diff uses pairs only.
  - Virtual rows with risk ≤ 0 become None, and the report counts how many were dropped.
  - Income that has not arrived yet is pending, not 0.
  - Reconcile by category and window, and list the items that cannot be attributed.
  - Journal: backfill, or label rows as before the journal existed.
  - **Owner decision needed first:** the real-trade R base, planned (preview `loss_at_stop`) or actual (fill vs initial stop).
- **PR-D, card and list:**
  - The card shows the Futures balance used, risk %, the risk budget, the risk after rounding, and the reason for any size reduction, including which idea and which time the re-quote cap came from.
  - Migration 23 stores the sizing inputs on the signal and the preview.
  - Held-positions list: numbered coins, bullets per detail, a blank line between coins.

## 6. Windows-only leftovers (do not transfer)

- `git stash` holds three old entries from earlier assist branches (#238 era). They are not part of this work.
- The worktree `jarvise-requote-tick` has a format-only diff in `tests/test_assist.py`: line wrapping from a local ruff version, on an already-merged branch. Discard it.
- The worktree `jarvise-wt-pr117` has an untracked `tmp/`.

## 7. Working pattern

- The owner pastes opinions from Grok. Claude checks each point against the code before accepting it, and states plainly which points it accepts, which it rejects, and why.
- Prompts to Cursor are in English and follow `CLAUDE.md` (Read first, Context, Task, Checks, Ship, Hard rules, Report).
- Big changes go push-only first. Claude reviews the actual diff, then a separate fdpy prompt.
- Replies to the owner are in Thai and short.
