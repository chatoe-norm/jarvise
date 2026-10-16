/** Turn an API error body into one owner-readable line instead of raw JSON. */
export function errorMessage(text: string, status: number): string {
  const fallback = `HTTP ${status}`;
  const raw = (text || "").trim();
  if (!raw) return fallback;
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    // Plain text or an HTML error page: keep it short.
    return raw.startsWith("<") ? fallback : raw.slice(0, 300);
  }
  return pickMessage(data) ?? fallback;
}

function pickMessage(data: unknown): string | null {
  if (typeof data === "string") return data || null;
  if (!data || typeof data !== "object") return null;
  const obj = data as Record<string, unknown>;
  const fieldErrors = obj.field_errors;
  if (fieldErrors && typeof fieldErrors === "object") {
    const parts = Object.entries(fieldErrors as Record<string, unknown>).map(
      ([k, v]) => `${k}: ${String(v)}`,
    );
    if (parts.length) return parts.join("; ");
  }
  for (const key of ["error", "detail", "message", "reason"]) {
    const v = obj[key];
    if (typeof v === "string" && v) return v;
    if (Array.isArray(v) && v.length) {
      // FastAPI validation: [{loc, msg}]
      const msgs = v
        .map((item) =>
          item && typeof item === "object" && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .filter(Boolean);
      if (msgs.length) return msgs.join("; ");
    }
    if (v && typeof v === "object") {
      const nested = pickMessage(v);
      if (nested) return nested;
    }
  }
  const result = obj.result;
  if (result && typeof result === "object") return pickMessage(result);
  return null;
}
