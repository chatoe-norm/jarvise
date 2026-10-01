export function buildEquityPoints(
  orders: Array<Record<string, unknown>>,
  currentEquity: unknown,
): Array<{ t: string; equity: number }> {
  const sorted = [...orders].sort((a, b) => {
    const ta = Number(a.ts ?? a.timestamp ?? 0);
    const tb = Number(b.ts ?? b.timestamp ?? 0);
    return ta - tb;
  });
  if (sorted.length === 0) {
    const eq = Number(currentEquity);
    if (Number.isFinite(eq)) {
      return [{ t: "now", equity: eq }];
    }
    return [];
  }

  let running = Number(currentEquity);
  if (!Number.isFinite(running)) running = 10_000;
  // Walk backwards from current equity using rough notional of fills.
  const backwards: number[] = new Array(sorted.length);
  for (let i = sorted.length - 1; i >= 0; i -= 1) {
    backwards[i] = running;
    const qty = Number(sorted[i].qty ?? 0);
    const price = Number(sorted[i].price ?? 0);
    const side = String(sorted[i].side || "").toLowerCase();
    const notional = qty * price;
    if (Number.isFinite(notional) && notional > 0) {
      // Approximate: buys reduce cash path, sells add — equity wobble for viz only.
      running += side.includes("sell") || side === "flat" ? -notional * 0.01 : notional * 0.01;
    }
  }

  return sorted.map((o, i) => {
    const ts = Number(o.ts ?? o.timestamp ?? 0);
    const label = Number.isFinite(ts) && ts > 0
      ? new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" })
      : `#${i + 1}`;
    return { t: label, equity: Number(backwards[i].toFixed(2)) };
  });
}
