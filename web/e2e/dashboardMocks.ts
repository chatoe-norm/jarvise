export const APPROVAL_ID = "appr-btc-1";

type DashboardOpts = {
  killSwitch?: boolean;
  pending?: boolean;
  openPosition?: boolean;
};

export function dashboardPayload(opts: DashboardOpts = {}) {
  const pending = opts.pending ?? true;
  const killSwitch = opts.killSwitch ?? false;
  const openPosition = opts.openPosition ?? false;
  const approval = {
    id: APPROVAL_ID,
    symbol: "BTCUSDT",
    timeframe: "4h",
    action: "long",
    confidence_score: 0.72,
    size_pct_equity: 2,
    expires_at_ms: Date.now() + 3_600_000,
    regime_state: "trend_up",
    thesis: "paper mock",
    status: "pending",
  };
  return {
    ok: true,
    status: {
      paper_only: true,
      live_trading: false,
      kill_switch: killSwitch,
      ingest: {},
      rag: {},
      paper: {},
      paper_expire: {},
      risk_caps: {},
      qdrant: {},
    },
    pending_count: pending ? 1 : 0,
    approvals: pending ? [approval] : [],
    paper: {
      ok: true,
      paper_only: true,
      equity: openPosition ? 10050 : 10000,
      cash: openPosition ? 5050 : 10000,
      positions: openPosition
        ? [
            {
              symbol: "BTCUSDT",
              side: "long",
              qty: 0.05,
              avg_price: 100000,
            },
          ]
        : [],
      orders: openPosition
        ? [
            {
              id: "ord-1",
              symbol: "BTCUSDT",
              side: "buy",
              status: "filled",
              qty: 0.05,
              price: 100000,
            },
          ]
        : [],
    },
    metrics: {
      ok: true,
      closed_trades: 0,
      wins: 0,
      losses: 0,
      scratches: 0,
      expected_value_ev: null,
    },
  };
}
