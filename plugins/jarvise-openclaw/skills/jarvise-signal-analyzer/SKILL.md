---
name: jarvise-signal-analyzer
description: Stage 1 Signal Generation — Multi-timeframe market structure, VWAP fair value, order book spoof check, and deterministic signal output.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"] },
      },
  }
---

# Jarvise Signal Analyzer (Stage 1)

Evaluates market structure and generates structured trade candidates across `1D`, `4H`, `1H`, and `15m` timeframes before passing them to Stage 2 (`jarvise-risk-guard`).

## Analysis Rules
1. **Market Structure First:** Identify Trend vs. Range, Swing High/Low, Support/Resistance zones, and VWAP fair value.
2. **Multi-Timeframe Alignment:** Require HTF alignment (e.g. 1D trend_up must not clash with LTF long).
3. **Indicators as Context:** EMA20 pullback, RSI band [45, 70] for pullback longs, volume confirmation.
4. **Order Book Microstructure:** Check order book depth for spoofing walls or thin liquidity before confirming setup.

## Output Specification
Outputs structured JSON:
```json
{
  "symbol": "BTCUSDT",
  "direction": "long",
  "confidence_score": 0.75,
  "entry_price": 68500.0,
  "invalidation_stop_price": 66800.0,
  "target_price": 72500.0,
  "timeframe": "4h",
  "thesis_summary": "Pullback to EMA20 with HTF 1D trend_up alignment, RSI 54, book depth stable."
}
```
If confidence < 0.65 or structure is chaotic, outputs `direction: "flat"`.
