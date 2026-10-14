export const STARTING_EQUITY = 10_000;

export type ClosedTrade = {
  close_ts?: unknown;
  pnl_usd?: unknown;
};

export function buildEquityPoints(
  trades: ClosedTrade[],
  startingEquity: unknown = STARTING_EQUITY,
): Array<{ t: string; equity: number }> {
  const start = Number(startingEquity);
  const base = Number.isFinite(start) ? start : STARTING_EQUITY;
  const rows = trades
    .map((trade) => ({
      ts: Number(trade.close_ts ?? 0),
      pnl: Number(trade.pnl_usd ?? 0),
    }))
    .filter((row) => Number.isFinite(row.ts) && row.ts > 0 && Number.isFinite(row.pnl))
    .sort((a, b) => a.ts - b.ts);

  let equity = base;
  const points = [{ t: "start", equity: round2(equity) }];
  for (const row of rows) {
    equity += row.pnl;
    points.push({
      t: new Date(row.ts).toLocaleDateString(undefined, { month: "short", day: "numeric" }),
      equity: round2(equity),
    });
  }
  return points;
}

export function equityDomain(points: Array<{ equity: number }>): [number, number] {
  const values = points.map((point) => point.equity).filter((value) => Number.isFinite(value));
  if (values.length === 0) return [STARTING_EQUITY - 10, STARTING_EQUITY + 10];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const pad = span > 0 ? span * 0.2 : 10;
  return [round2(min - pad), round2(max + pad)];
}

function round2(value: number): number {
  return Number(value.toFixed(2));
}
