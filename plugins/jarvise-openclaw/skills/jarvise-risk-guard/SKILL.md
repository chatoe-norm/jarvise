---
name: jarvise-risk-guard
description: Stage 2 Risk Guardrail Gatekeeper — Enforces 1% risk ceiling, >=1.5x ATR stop-loss distance, daily drawdown halt, and asset whitelisting.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"] },
      },
  }
---

# Jarvise Risk Guardrail Gatekeeper (Stage 2)

Evaluates incoming signals from Stage 1 against strict declarative guardrails in `config/guardrails.policy.json`.

## Mandatory Non-Negotiable Checks
1. **Allowed Asset Check:** Asset must be in `allowed_assets` (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `BNBUSDT`).
2. **The 1% Position Sizing Rule:** Maximum risk at stop must stay $\le 1.00\%$ of account equity:
   $$\text{Risk at Stop} = \text{Size \%} \times \frac{|\text{Entry} - \text{Stop}|}{\text{Entry}} \le 1.0\%$$
3. **Volatility-Adjusted Invalidation:** Hard Stop-Loss distance must satisfy:
   $$|\text{Entry} - \text{Stop}| \ge 1.5 \times \text{ATR(14)}$$
   Signals with stops tighter than $1.5\times\text{ATR}$ are aborted as noise (`HELD: Stop distance below 1.5x ATR noise floor`).
4. **Daily Cumulative Drawdown Halt:** If UTC calendar day realized + MTM loss reaches the limit ($\$100$ or $2.5\%$), engage kill-switch and set status to `HALT`.
5. **Confidence Gate:** Reject any signal with confidence $< 0.65$ into `FLAT`.

## Output Verdict
- `PASSED`: Forwards to Stage 3 (`jarvise-order-router`).
- `HELD`: Rejection reason recorded into audit log.
