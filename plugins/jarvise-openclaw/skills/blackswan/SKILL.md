---
name: blackswan
description: Precursor Risk Intelligence — 15-minute Flare alarms and 60-minute Core risk synthesis for sudden macro liquidity dislocations.
metadata:
  {
    "openclaw":
      {
        "requires": { "env": ["OPENCLAW_PAPER_ONLY"] },
      },
  }
---

# BlackSwan Precursor Risk Intelligence

Monitors real-time anomalies and extreme macro dislocations:
- Sudden funding rate spikes or negative inversions.
- Cascade liquidation clusters across derivatives venues.
- Massive order book bid/ask wall pullouts (spoofing collapse).

## Actions on Anomaly
- Triggers high-priority alert to Jarvise Risk Monitor.
- Pre-emptively engages fail-closed market safety: forces incoming signals to `FLAT` until market depth stabilizes.
