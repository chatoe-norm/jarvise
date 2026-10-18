export type DashboardRules = {
  atr_stop_mult?: number | null;
  breakeven_r?: number | null;
  breakeven_buffer_bps?: number | null;
  trail_after_r?: number | null;
  trail_distance_r?: number | null;
  paper_fee_bps?: number | null;
  paper_timeframe?: string | null;
};

export type DashboardFreshness = {
  market_safety_max_age_min?: number | null;
  paper_timeframe?: string | null;
  paper_bar_ms?: number | null;
};

/** Format a number from the API. Missing values stay a dash. */
export function ruleText(value: number | null | undefined, suffix = ""): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const text = Number.isInteger(value) ? String(value) : String(value);
  return suffix ? `${text}${suffix}` : text;
}

export function paperTimeframeLabel(timeframe: string | null | undefined): string {
  const tf = timeframe?.trim();
  return tf ? tf : "—";
}

/** True when the age is past the backend threshold. Null when either input is missing. */
export function ageIsStale(
  atMs: number | null | undefined,
  maxAgeMin: number | null | undefined,
  nowMs = Date.now(),
): boolean | null {
  if (atMs == null || maxAgeMin == null || !Number.isFinite(atMs) || !Number.isFinite(maxAgeMin)) {
    return null;
  }
  return nowMs - atMs > maxAgeMin * 60_000;
}

export function autoDecideChip(
  enabled: boolean | undefined,
  latched: boolean | undefined,
): { tone: "ok" | "warn" | "off"; label: string } {
  if (enabled !== true) return { tone: "off", label: "off" };
  if (latched) return { tone: "warn", label: "latched" };
  return { tone: "ok", label: "on" };
}
