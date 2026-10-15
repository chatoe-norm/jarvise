---
name: jarvise-order-router
description: Stage 3 Order Execution Router — Routes approved signals into Jarvise paper ledger or manual approval queue. Enforces paper-first mode.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"] },
      },
  }
---

# Jarvise Order Router (Stage 3)

Receives passed trade setups from Stage 2 (`jarvise-risk-guard`) and routes them through the Jarvise Core OMS.

## Operating Modes
1. **`PAPER_SIMULATED` (Default & Active):**
   - Simulates `LIMIT` bracket orders against live bid/ask book depth.
   - Applies paper fee ($10\text{ bps}$) and slippage ($\max(5\text{ bps}, \frac{1}{2}\text{spread})$).
   - Writes order fill to `paper_orders` and open position to `paper_positions` in `data/analytics/jarvise.db`.
   - Attaches two-stage profit lock:
     - Stage 1: Breakeven ($+20\text{ bps}$ fee buffer) at $0.5R$
     - Stage 2: Trailing $1.0R$ from best favorable excursion
2. **`MANUAL_APPROVAL_POLICY`:**
   - Enqueues candidate into `approval_queue` for operator manual sign-off on the Home dashboard or Telegram alert.
3. **`AUTONOMOUS_LIVE` (Strictly Gated):**
   - Dormant while `JARVISE_LIVE_TRADING=false`. Never routes live orders without passing the P4-C checklist and explicit operator key activation.
