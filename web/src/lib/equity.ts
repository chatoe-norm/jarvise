export const STARTING_EQUITY = 10_000;

export type ClosedTrade = {
  close_ts?: unknown;
  pnl_usd?: unknown;
  symbol?: unknown;
};

export type EquityPoint = {
  t: string;
  equity: number;
  symbol: string | null;
  pnl: number | null;
};

export function buildEquityPoints(
  trades: ClosedTrade[],
  startingEquity: unknown = STARTING_EQUITY,
): EquityPoint[] {
  const start = Number(startingEquity);
  const base = Number.isFinite(start) ? start : STARTING_EQUITY;
  const rows = trades
    .map((trade) => ({
      ts: Number(trade.close_ts ?? 0),
      pnl: Number(trade.pnl_usd ?? 0),
      symbol: tradeSymbol(trade.symbol),
    }))
    .filter((row) => Number.isFinite(row.ts) && row.ts > 0 && Number.isFinite(row.pnl))
    .sort((a, b) => a.ts - b.ts);

  let equity = base;
  const used = new Map<string, number>();
  const points: EquityPoint[] = [{ t: "start", equity: round2(equity), symbol: null, pnl: null }];
  for (const row of rows) {
    equity += row.pnl;
    points.push({
      t: uniqueLabel(used, tickLabel(row.ts, row.symbol)),
      equity: round2(equity),
      symbol: row.symbol,
      pnl: round2(row.pnl),
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

function tradeSymbol(value: unknown): string {
  const text = String(value ?? "").trim().toUpperCase();
  if (!text) return "TRADE";
  for (const quote of ["USDT", "USDC", "BUSD", "USD"]) {
    if (text.endsWith(quote) && text.length > quote.length) {
      return text.slice(0, -quote.length);
    }
  }
  return text;
}

function tickLabel(ts: number, symbol: string): string {
  const when = new Date(ts).toLocaleString("en-GB", {
    timeZone: "Asia/Bangkok",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  return `${when.replace(",", "")} ${symbol}`;
}

function uniqueLabel(used: Map<string, number>, label: string): string {
  const seen = used.get(label) ?? 0;
  used.set(label, seen + 1);
  if (seen === 0) return label;
  return `${label} (${seen + 1})`;
}

function round2(value: number): number {
  return Number(value.toFixed(2));
}
