import type { RiskCapsPayload } from "@/lib/api";

export type RiskCapField = keyof RiskCapsPayload;

const PCT_FIELDS: ReadonlySet<RiskCapField> = new Set([
  "drawdown_lock_pct",
  "max_gross_notional_pct",
  "max_symbol_notional_pct",
  "max_correlated_bucket_pct",
]);

/**
 * Same rules as the backend `validate_risk_cap_updates`: every cap must be a positive number.
 * A blank or 0 cap is never saved — max_daily_loss_usd=0 would halt trading on the next risk run.
 */
export function validateRiskCap(field: RiskCapField, raw: string): string | null {
  const text = raw.trim();
  if (!text) return "ต้องกรอกค่า";
  const val = Number(text);
  if (!Number.isFinite(val)) return "ต้องเป็นตัวเลข";
  if (val <= 0) return "ต้องมากกว่า 0";
  if (field === "max_open_positions" && !Number.isInteger(val)) return "ต้องเป็นจำนวนเต็ม";
  if (field === "max_risk_per_trade_pct" && val > 1.0) return "สูงสุด 1.0%";
  if (PCT_FIELDS.has(field) && val > 100) return "สูงสุด 100%";
  return null;
}

export function validateRiskCaps(raw: Record<RiskCapField, string>): {
  caps: RiskCapsPayload | null;
  errors: Partial<Record<RiskCapField, string>>;
} {
  const errors: Partial<Record<RiskCapField, string>> = {};
  const caps = {} as RiskCapsPayload;
  for (const field of Object.keys(raw) as RiskCapField[]) {
    const err = validateRiskCap(field, raw[field]);
    if (err) errors[field] = err;
    else caps[field] = Number(raw[field].trim());
  }
  return { caps: Object.keys(errors).length ? null : caps, errors };
}

export function capsToRaw(caps: RiskCapsPayload): Record<RiskCapField, string> {
  const out = {} as Record<RiskCapField, string>;
  for (const field of Object.keys(caps) as RiskCapField[]) out[field] = String(caps[field]);
  return out;
}

/** Save stays off until the server caps have loaded. Defaults must not overwrite them. */
export function canSaveRiskCaps(opts: {
  loaded: boolean;
  dirty: boolean;
  saving: boolean;
  hasErrors: boolean;
}): boolean {
  return opts.loaded && opts.dirty && !opts.saving && !opts.hasErrors;
}
