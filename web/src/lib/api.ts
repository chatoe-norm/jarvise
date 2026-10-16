import { errorMessage } from "@/lib/errors";
import { dashboardSchema, paperSchema, statusSchema } from "@/lib/schemas";

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

export type AutoDecideConfigPayload = {
  enabled: boolean;
  max_risk_per_trade_pct: number;
  hard_max_risk_pct?: number;
  model?: string;
};

export type QdrantStatus = {
  exists?: boolean;
  points?: number;
  vectors_count?: number;
  status?: string;
  collection?: string;
  url?: string;
  error?: string;
  optimizer_status?: string;
};

export type DoctrineSnippet = {
  text: string;
  source?: string;
  score?: number;
};

export type DoctrineQueryResponse = {
  ok: boolean;
  query: string;
  hits: DoctrineSnippet[];
  paper_only?: boolean;
  error?: string;
};

export type StatusPayload = {
  paper_only: boolean;
  live_trading: boolean;
  kill_switch: boolean;
  /** known:false = Redis unreadable; approvals fail closed. */
  kill_switch_state?: {
    engaged?: boolean;
    known?: boolean;
    reason?: string | null;
    error?: string | null;
  } | null;
  ingest: unknown;
  rag: unknown;
  paper: unknown;
  paper_expire: unknown;
  ingest_health?: IngestHealthStatus | null;
  paper_auto?: PaperAutoStatus | null;
  auto_decide_config?: AutoDecideConfigPayload | null;
  risk_caps: Record<string, unknown>;
  qdrant?: QdrantStatus | Record<string, unknown>;
  fear_greed_index?: number | null;
  altcoin_season_index?: number | null;
  btc_dominance_pct?: number | null;
  global_market_cap_usd?: number | null;
  sentiment_note?: string | null;
  risk_monitor?: unknown;
  auth_warning?: string | null;
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
  analysis_id?: string | null;
  status?: string;
};

export type OhlcvBar = {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
};

export type OhlcvPayload = {
  ok: boolean;
  approval_id: string;
  symbol: string;
  timeframe: string;
  bars: OhlcvBar[];
  invalidation_price: number | null;
  action: string;
};

export type ConfidenceStep = {
  label: string;
  delta: number;
};

export type ConfidenceBreakdown = {
  regime: string;
  side: string;
  base: number;
  steps: ConfidenceStep[];
  total: number;
  gates: {
    flat_below: number;
    doctrine_free_approve: number;
  };
  insufficient: boolean;
};

export type AnalysisExplainPayload = {
  ok: boolean;
  analysis_id: string;
  symbol: string;
  timeframe: string;
  timestamp: number;
  candle: {
    close: number | null;
    ema_20: number | null;
    ema_200: number | null;
    rsi_14: number | null;
    atr_14: number | null;
  } | null;
  breakdown: ConfidenceBreakdown;
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
  news_context?: { not_a_trade_signal: true; lines: string[] } | null;
  thesis?: string | null;
  claude: {
    decision: string;
    reason: string | null;
    model: string;
    at_ms: number;
  } | null;
  confidence_breakdown?: ConfidenceBreakdown;
  gates?: { flat: number; approve: number };
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

export type LossExitBucket = {
  n: number;
  pnl_usd: number;
};

export type LossLesson = {
  closed_trades: number;
  wins: number;
  losses: number;
  by_exit: {
    stop_hit: LossExitBucket;
    flat: LossExitBucket;
    /** Owner Close button; older backends omit it and count these under flat. */
    manual?: LossExitBucket;
  };
  by_symbol: Record<string, LossExitBucket>;
  auto_closed: number;
  auto_ev: number | null;
  floors_unchanged: boolean;
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
  starting_equity?: number;
  trades?: Array<{
    close_ts?: number;
    pnl_usd?: number;
    symbol?: string;
    exit_reason?: string;
    qty?: number;
    entry_price?: number;
    exit_price?: number;
  }>;
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
  loss_lesson?: LossLesson;
  error?: string;
};

export type CryptoSuitability = {
  symbol: string;
  score: number;
  grade: "high" | "medium_high" | "moderate" | "low";
  label: string;
  color: string;
  reasons: string[];
  action: string;
  regime_state: string;
  confidence_score: number;
  thesis?: string | null;
  analysis_timestamp?: number | null;
  candle: {
    close: number | null;
    ema_20: number | null;
    ema_200: number | null;
    rsi_14: number | null;
    atr_14: number | null;
  };
  health: {
    rows: number;
    ema200_ready: number;
    newest_age_min: number | null;
    gaps: number;
  };
  has_open_position: boolean;
  is_active?: boolean;
};

export type UniversePayload = {
  ok: boolean;
  universe_id: string;
  timeframe: string;
  active_symbols: string[];
  items: CryptoSuitability[];
  backfill_hint?: string;
  error?: string;
};

export type AnalysisOutcomeKind =
  | "filled"
  | "hold"
  | "rejected"
  | "timed_out"
  | "failed"
  | "pending"
  | "not_queued";

export type AnalysisOutcome = {
  kind: AnalysisOutcomeKind;
  approval_id: string | null;
  approval_status: string | null;
  resolve_reason: string | null;
  resolved_at_ms: number | null;
  fills: number;
  fill_reasons: string[];
};

export type AnalysisSummary = {
  by_kind?: Partial<Record<AnalysisOutcomeKind, number>>;
  by_action?: Record<string, number>;
  total?: number;
};

export type AnalysisPayload = {
  ok: boolean;
  rows: Array<Record<string, unknown>>;
  total?: number;
  limit?: number;
  offset?: number;
  summary?: AnalysisSummary;
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
    pricing?: string | null;
  }>;
  total_usd?: number | null;
  priced_count?: number;
  unpriced_count?: number;
  error?: string;
};

export type NewsHome = {
  ok?: boolean;
  not_a_trade_signal?: boolean;
  available?: boolean;
  lines?: string[];
  digest_note?: string | null;
  digest_items?: Array<{
    theme: string;
    title: string;
    url?: string;
    comment?: string;
  }> | null;
  errors?: string[];
  generated_at_ms?: number | null;
  window_years?: number | null;
};

export type DashboardPayload = {
  ok: boolean;
  status: StatusPayload;
  pending_count: number;
  approvals: ApprovalRow[];
  paper: PaperPayload;
  metrics: MetricsPayload;
  news?: NewsHome | null;
};

async function request<T>(
  path: string,
  init?: RequestInit,
  schema?: { parse: (data: unknown) => unknown },
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
    throw new Error(errorMessage(text, resp.status));
  }
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    const data: unknown = await resp.json();
    if (schema) {
      return schema.parse(data) as T;
    }
    return data as T;
  }
  return {} as T;
}

/** A 200 body with ok:false is still a refusal; never let it read as success. */
function requireOk<T extends { ok: boolean; error?: string | null }>(data: T): T {
  if (data && data.ok === false) {
    throw new Error(data.error || "request refused");
  }
  return data;
}

export const api = {
  dashboard: () => request<DashboardPayload>("/api/dashboard", undefined, dashboardSchema),
  status: () => request<StatusPayload>("/api/status", undefined, statusSchema),
  paper: () => request<PaperPayload>("/api/paper", undefined, paperSchema),
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
  analysisExplain: (analysisId: string) =>
    request<AnalysisExplainPayload>(
      `/api/analysis/${encodeURIComponent(analysisId)}/explain`,
    ),
  approvals: (status = "pending") =>
    request<{ ok: boolean; rows: ApprovalRow[] }>(
      `/api/approvals?status=${encodeURIComponent(status)}`,
    ),
  recommendation: (id: string) =>
    request<RecommendationPayload>(
      `/api/approvals/${encodeURIComponent(id)}/recommendation`,
    ),
  ohlcv: (id: string) =>
    request<OhlcvPayload>(`/api/approvals/${encodeURIComponent(id)}/ohlcv`),
  exchange: () => request<ExchangePayload>("/api/exchange"),
  approve: (id: string) =>
    request<{ ok: boolean; error?: string | null }>("/approvals/approve", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ id }),
    }).then(requireOk),
  reject: (id: string) =>
    request<{ ok: boolean; error?: string | null }>("/approvals/reject", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ id }),
    }).then(requireOk),
  killSwitch: (state: "on" | "off") =>
    request<{ ok: boolean; kill_switch: boolean }>("/kill-switch", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ state }),
    }),
  persistMetrics: () =>
    request<{ ok: boolean }>("/paper/metrics/persist", { method: "POST" }),
  closePosition: (symbol: string) =>
    request<{ ok: boolean; error?: string; fills?: unknown[] }>("/paper/close", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ symbol }),
    }),
  universe: () => request<UniversePayload>("/api/universe"),
  addUniverseSymbol: (symbol: string, assetClass = "crypto") =>
    request<{ ok: boolean; symbol: string; universe_id: string; active_symbols: string[] }>(
      "/api/universe/add",
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ symbol, asset_class: assetClass }),
      },
    ),
  removeUniverseSymbol: (symbol: string) =>
    request<{ ok: boolean; symbol: string; universe_id: string; active_symbols: string[] }>(
      "/api/universe/remove",
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ symbol }),
      },
    ),
  resetUniverse: (preset: "paper_core" | "paper_liquid") =>
    request<{ ok: boolean; preset: string; active_symbols: string[] }>(
      "/api/universe/reset",
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ preset }),
      },
    ),
  syncUniverse: (symbol?: string) =>
    request<{ ok: boolean; synced_symbols: string[]; count: number; timeframe: string }>(
      "/api/universe/sync",
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(symbol ? { symbol } : {}),
      },
    ),
  syncRag: () =>
    request<{
      ok: boolean;
      error?: string;
      status_code?: number;
      notebook?: unknown;
      index?: unknown;
      profile?: string;
      notebook_id?: string;
      url?: string;
      at?: string;
    }>("/api/rag/sync", {
      method: "POST",
    }),
  getAutoDecideConfig: () =>
    request<{ ok: boolean } & AutoDecideConfigPayload>("/api/paper/auto-decide/config"),
  saveAutoDecideConfig: (config: { enabled?: boolean; max_risk_per_trade_pct?: number }) =>
    request<{ ok: boolean } & AutoDecideConfigPayload>("/api/paper/auto-decide/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    }),
  runAutoDecide: () =>
    request<PaperAutoStatus & { ok: boolean; error?: string }>("/api/paper/auto-decide/run", {
      method: "POST",
    }),
  queryDoctrine: (q: string, limit: number = 3) =>
    request<DoctrineQueryResponse>(
      `/api/doctrine/query?${new URLSearchParams({ q, limit: String(limit) }).toString()}`,
    ),
  getRiskCaps: () =>
    request<{ ok: boolean; risk_caps: RiskCapsPayload }>("/api/risk/caps"),
  saveRiskCaps: (caps: Partial<RiskCapsPayload>) =>
    request<{ ok: boolean; risk_caps: RiskCapsPayload }>("/api/risk/caps", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(caps),
    }),
  runPipelineJob: (job: string) =>
    request<PipelineRunResult>("/api/pipeline/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job }),
    }),
  runIngestHealth: () =>
    request<IngestHealthStatus & { ok: boolean }>("/api/ingest/health/run", {
      method: "POST",
    }),
  runIngestBackfill: (params: { symbols?: string[]; timeframe?: string; since?: string }) =>
    request<BackfillPayload>("/api/ingest/backfill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    }),
  getObservabilityPreview: () =>
    request<ObservabilityPreviewPayload>("/api/observability/preview"),
  persistObservabilitySnapshot: () =>
    request<{ ok: boolean; metrics?: unknown }>("/api/observability/persist", {
      method: "POST",
    }),
  getModelRouting: () => request<import("@/types/modelRouting").ModelRoutingMatrix>("/api/openclaw/model-routing"),
};

export type RiskCapsPayload = {
  max_notional_per_order: number;
  max_daily_loss_usd: number;
  drawdown_lock_pct: number;
  max_open_positions: number;
  max_gross_notional_pct: number;
  max_symbol_notional_pct: number;
  max_correlated_bucket_pct: number;
  max_risk_per_trade_pct: number;
};

export type PipelineRunResult = {
  ok: boolean;
  job: string;
  duration_s: number;
  result: Record<string, unknown>;
  executed_at: number;
};

export type BackfillPayload = {
  ok: boolean;
  synced_symbols: string[];
  count: number;
  timeframe: string;
  since: string;
};

export type ObservabilityPreviewPayload = {
  ok: boolean;
  gauges: {
    kill_switch: boolean;
    kill_switch_state?: Record<string, unknown>;
    paper_equity: number;
    paper_cash: number;
    open_positions_count: number;
    pending_approvals_count: number;
    live_trading: boolean;
  };
  job_runs: {
    ingest?: unknown;
    rag?: unknown;
    paper?: unknown;
    paper_expire?: unknown;
    paper_auto?: unknown;
    ingest_health?: IngestHealthStatus | null;
    risk_monitor?: unknown;
  };
  metrics_summary?: Record<string, unknown>;
  endpoints: {
    grafana: string;
    prometheus: string;
    web_metrics: string;
  };
};


