export type IngestHealthStatus = {
  ok: boolean;
  timeframe: string;
  symbols: Record<
    string,
    { rows: number; ema200_ready: number; newest_age_min: number | null; gaps: number }
  >;
  alerts: string[];
  backfill_hint?: string;
  at_ms: number;
  telegram_sent?: boolean;
};

export type PaperAutoStatus = {
  ok: boolean;
  skipped?: boolean;
  reason?: string;
  error?: string;
  halted?: string | null;
  model?: string;
  prompt_version?: string;
  processed?: number;
  approved?: Array<{ id: string; symbol?: string; reason?: string; fills?: number }>;
  rejected?: Array<{ id: string; symbol?: string; reason?: string }>;
  deferred?: Array<{ id: string; symbol?: string; reason?: string }>;
  filtered_out?: Array<{ id: string; symbol?: string; reason?: string }>;
  apply_failed?: Array<{ id: string; symbol?: string; error?: string }>;
  doctrine_unavailable?: boolean;
  duration_s?: number;
  at_ms?: number;
  telegram_sent?: boolean;
  auto_ev_gate?: {
    enabled?: boolean;
    blocked?: boolean;
    min_ev?: number | null;
    min_n?: number;
    n?: number;
    ev?: number | null;
    reason?: string | null;
  };
};

export type StatusPayload = {
  paper_only: boolean;
  live_trading: boolean;
  kill_switch: boolean;
  ingest: unknown;
  rag: unknown;
  paper: unknown;
  paper_expire: unknown;
  ingest_health?: IngestHealthStatus | null;
  paper_auto?: PaperAutoStatus | null;
  risk_caps: Record<string, unknown>;
  qdrant: Record<string, unknown>;
};

export type ApprovalRow = {
  id: string;
  symbol?: string;
  timeframe?: string;
  action?: string;
  confidence_score?: number;
  size_pct_equity?: number;
  expires_at_ms?: number;
  regime_state?: string;
  thesis?: string;
  status?: string;
};

export type RecommendationPayload = {
  ok: boolean;
  approval_id: string;
  symbol: string;
  timeframe: string;
  action: string;
  status?: string;
  recommendation: "approve" | "approve_with_caution" | "reject";
  recommendation_source: "template" | "claude";
  confidence_label: string;
  headline: string;
  what_happened: string[];
  risk: {
    size_pct_equity: number | null;
    notional_usd: number | null;
    equity_usd: number;
    invalidation_price: number | null;
    est_loss_usd?: number;
    stop_atr_multiple: number;
  };
  doctrine: string[];
  checklist: string[];
  thesis?: string | null;
  claude: {
    decision: string;
    reason: string | null;
    model: string;
    at_ms: number;
  } | null;
};

export type PaperPayload = {
  ok: boolean;
  paper_only?: boolean;
  equity?: number | string | null;
  cash?: number | string | null;
  positions: Array<Record<string, unknown>>;
  orders: Array<Record<string, unknown>>;
  error?: string;
};

export type SourceMetrics = {
  closed_trades?: number;
  wins?: number;
  losses?: number;
  scratches?: number;
  expected_value_ev?: number | null;
  win_rate?: number | null;
};

export type MetricsPayload = {
  ok?: boolean;
  closed_trades?: number;
  wins?: number;
  losses?: number;
  scratches?: number;
  expected_value_ev?: number | null;
  win_rate?: number | null;
  max_drawdown_pct?: number | null;
  sharpe_ratio?: number | null;
  sortino_ratio?: number | null;
  ratios_ready?: boolean;
  need_trades_for_ratios?: number;
  unmatched_orders?: number;
  by_decision_source?: Record<string, SourceMetrics>;
  by_decision_source_30d?: Record<string, SourceMetrics>;
  auto_ev_gate?: {
    enabled?: boolean;
    blocked?: boolean;
    min_ev?: number | null;
    min_n?: number;
    n?: number;
    ev?: number | null;
    reason?: string | null;
  };
  error?: string;
};

export type AnalysisPayload = {
  ok: boolean;
  rows: Array<Record<string, unknown>>;
  total?: number;
  limit?: number;
  offset?: number;
  error?: string;
};

export type ExchangePayload = {
  ok: boolean;
  available: boolean;
  venue?: string;
  fetched_at_ms?: number;
  balances?: Array<{
    asset: string;
    free: string;
    locked: string;
    total: string;
    usd: number | null;
  }>;
  total_usd?: number | null;
  error?: string;
};

export type DashboardPayload = {
  ok: boolean;
  status: StatusPayload;
  pending_count: number;
  approvals: ApprovalRow[];
  paper: PaperPayload;
  metrics: MetricsPayload;
};

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  const resp = await fetch(path, {
    ...init,
    headers,
    credentials: "include",
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(text || `HTTP ${resp.status}`);
  }
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return (await resp.json()) as T;
  }
  return {} as T;
}

export const api = {
  dashboard: () => request<DashboardPayload>("/api/dashboard"),
  status: () => request<StatusPayload>("/api/status"),
  paper: () => request<PaperPayload>("/api/paper"),
  metrics: () => request<MetricsPayload>("/api/paper/metrics"),
  analysis: (
    symbol = "",
    timeframe = "",
    opts?: { limit?: number; offset?: number },
  ) => {
    const q = new URLSearchParams();
    if (symbol) q.set("symbol", symbol);
    if (timeframe) q.set("timeframe", timeframe);
    if (opts?.limit != null) q.set("limit", String(opts.limit));
    if (opts?.offset != null) q.set("offset", String(opts.offset));
    const qs = q.toString();
    return request<AnalysisPayload>(`/api/analysis${qs ? `?${qs}` : ""}`);
  },
  approvals: (status = "pending") =>
    request<{ ok: boolean; rows: ApprovalRow[] }>(
      `/api/approvals?status=${encodeURIComponent(status)}`,
    ),
  recommendation: (id: string) =>
    request<RecommendationPayload>(
      `/api/approvals/${encodeURIComponent(id)}/recommendation`,
    ),
  exchange: () => request<ExchangePayload>("/api/exchange"),
  approve: (id: string) =>
    request<{ ok: boolean }>("/approvals/approve", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ id }),
    }),
  reject: (id: string) =>
    request<{ ok: boolean }>("/approvals/reject", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ id }),
    }),
  killSwitch: (state: "on" | "off") =>
    request<{ ok: boolean; kill_switch: boolean }>("/kill-switch", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ state }),
    }),
  persistMetrics: () =>
    request<{ ok: boolean }>("/paper/metrics/persist", { method: "POST" }),
};
