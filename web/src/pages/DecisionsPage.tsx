import { Fragment, useCallback, useEffect, useState, type FormEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { ConfidenceLadder } from "@/components/ConfidenceLadder";
import { InfoTip } from "@/components/InfoTip";
import { api, type AnalysisExplainPayload, type AnalysisSummary } from "@/lib/api";
import { outcomeCopy, parseAnalysisOutcome, resolveReasonLabel } from "@/lib/copy";
import { paperTimeframeLabel } from "@/lib/dashboardRules";
import { formatDecisionStamp, formatNum, formatThesisDisplay } from "@/lib/utils";

const PAGE_SIZES = [10, 15, 20, 50, 100] as const;

export function DecisionsPage() {
  const [symbol, setSymbol] = useState("");
  const [timeframe, setTimeframe] = useState("");
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<AnalysisSummary | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(10);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [explains, setExplains] = useState<Record<string, AnalysisExplainPayload>>({});
  const [explainError, setExplainError] = useState<Record<string, string>>({});
  const [explainLoading, setExplainLoading] = useState<Record<string, boolean>>({});
  const [paperTf, setPaperTf] = useState<string | null>(null);

  const load = useCallback(
    async (
      sym = symbol,
      tf = timeframe,
      nextPage = page,
      nextSize = pageSize,
    ) => {
      setLoading(true);
      setError(null);
      const offset = Math.max(0, (nextPage - 1) * nextSize);
      try {
        const data = await api.analysis(sym, tf, {
          limit: nextSize,
          offset,
        });
        if (!data.ok) setError(data.error || "Failed");
        setRows(data.rows || []);
        setTotal(Number(data.total ?? data.rows?.length ?? 0));
        setSummary(data.summary ?? null);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    },
    [symbol, timeframe, page, pageSize],
  );

  useEffect(() => {
    void load("", "", 1, 10);
    void api.status().then((status) => setPaperTf(status.rules?.paper_timeframe ?? null)).catch(() => setPaperTf(null));
  }, []);

  const pageCount = Math.max(1, Math.ceil(total / pageSize) || 1);
  const safePage = Math.min(page, pageCount);
  const from = total === 0 ? 0 : (safePage - 1) * pageSize + 1;
  const to = Math.min(safePage * pageSize, total);

  function onFilter(e: FormEvent) {
    e.preventDefault();
    setPage(1);
    void load(symbol, timeframe, 1, pageSize);
  }

  function changePageSize(size: number) {
    setPageSize(size);
    setPage(1);
    void load(symbol, timeframe, 1, size);
  }

  function goPage(next: number) {
    const clamped = Math.max(1, Math.min(next, pageCount));
    setPage(clamped);
    void load(symbol, timeframe, clamped, pageSize);
  }

  async function toggleExplain(analysisId: string) {
    const next = !expanded[analysisId];
    setExpanded((prev) => ({ ...prev, [analysisId]: next }));
    if (!next || explains[analysisId] || explainLoading[analysisId]) return;
    setExplainLoading((prev) => ({ ...prev, [analysisId]: true }));
    setExplainError((prev) => {
      const cleared = { ...prev };
      delete cleared[analysisId];
      return cleared;
    });
    try {
      const payload = await api.analysisExplain(analysisId);
      setExplains((prev) => ({ ...prev, [analysisId]: payload }));
    } catch (err) {
      setExplainError((prev) => ({
        ...prev,
        [analysisId]: err instanceof Error ? err.message : String(err),
      }));
    } finally {
      setExplainLoading((prev) => ({ ...prev, [analysisId]: false }));
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Decisions</h1>
        <p className="text-sm text-[var(--color-muted)]">
          Latest analysis output — regime, action, and thesis.
        </p>
        <p className="mt-1 text-xs text-[var(--color-muted)]">
          0.55 = FLAT gate · 0.70 = auto-approve without trading rules · click Conf to
          see the ladder
        </p>
        <p className="mt-1 text-xs text-[var(--color-muted)]">
          Outcome = ผลจากคิวอนุมัติ; ถือไม้เดิม = มี position ฝั่งเดียวกันอยู่แล้ว
          ระบบไม่เพิ่มไม้
        </p>
      </div>

      <Card className="border-[var(--color-border)]">
        <CardContent className="pt-4 space-y-3">
          <form className="flex flex-wrap items-end gap-3" onSubmit={onFilter}>
            <label className="text-xs text-[var(--color-muted)]">
              Symbol
              <input
                className="mt-1 block rounded-md border border-[var(--color-border)] bg-[#161d27] px-2 py-1.5 text-sm text-[var(--color-foreground)] font-mono"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                placeholder="BTCUSDT"
              />
            </label>
            <label className="text-xs text-[var(--color-muted)]">
              Timeframe
              <input
                className="mt-1 block rounded-md border border-[var(--color-border)] bg-[#161d27] px-2 py-1.5 text-sm text-[var(--color-foreground)] font-mono"
                value={timeframe}
                onChange={(e) => setTimeframe(e.target.value)}
                placeholder={paperTf ?? "—"}
              />
            </label>
            <Button type="submit" size="sm">
              Filter
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                setSymbol("");
                setTimeframe("");
                setPage(1);
                void load("", "", 1, pageSize);
              }}
            >
              Clear
            </Button>
          </form>

          <div className="flex flex-wrap items-center gap-2 pt-1 border-t border-[var(--color-border)]/50 text-xs text-[var(--color-muted)]">
            <span>Quick:</span>
            {["ALL", "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"].map((sym) => {
              const active = (sym === "ALL" && !symbol) || symbol === sym;
              return (
                <button
                  key={sym}
                  type="button"
                  onClick={() => {
                    const target = sym === "ALL" ? "" : sym;
                    setSymbol(target);
                    setPage(1);
                    void load(target, timeframe, 1, pageSize);
                  }}
                  className={`rounded min-h-11 px-3 sm:min-h-0 sm:px-2 py-0.5 font-mono text-[11px] transition-colors ${
                    active
                      ? "bg-[var(--color-accent)] text-white font-medium"
                      : "bg-[#161d27] hover:bg-[#1e2a38] text-[var(--color-foreground)]"
                  }`}
                >
                  {sym}
                </button>
              );
            })}
            <span className="ml-2">TF:</span>
            {(paperTf && paperTf !== "1h" && paperTf !== "1d" ? ["ALL", paperTf, "1h", "1d"] : ["ALL", "1h", "1d"]).map((tf) => {
              const active = (tf === "ALL" && !timeframe) || timeframe === tf;
              const label = tf === "ALL" ? "ALL" : tf === paperTf ? `${paperTimeframeLabel(tf)} (paper)` : tf;
              return (
                <button
                  key={tf}
                  type="button"
                  onClick={() => {
                    const target = tf === "ALL" ? "" : tf;
                    setTimeframe(target);
                    setPage(1);
                    void load(symbol, target, 1, pageSize);
                  }}
                  className={`rounded min-h-11 px-3 sm:min-h-0 sm:px-2 py-0.5 font-mono text-[11px] transition-colors pointer-coarse:min-h-11 ${
                    active
                      ? "bg-[var(--color-accent)] text-white font-medium"
                      : "bg-[#161d27] hover:bg-[#1e2a38] text-[var(--color-foreground)]"
                  }`}
                >
                  {label}
                </button>
              );
            })}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Analysis</CardTitle>
          {summary ? <OutcomeSummaryChips summary={summary} /> : null}
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-sm text-[var(--color-muted)]">Loading…</p>
          ) : null}
          {error ? <p className="text-sm text-[var(--color-danger)]">{error}</p> : null}
          {!loading && !error && rows.length === 0 ? (
            <p className="text-sm text-[var(--color-muted)]">
              No analysis rows. Run <code>jarvise analyze</code> first.
            </p>
          ) : null}
          {rows.length > 0 ? (
            <>
              <ul className="space-y-3 sm:hidden">
                {rows.map((r, i) => {
                  const stamp = formatDecisionStamp(r.timestamp);
                  const analysisId =
                    r.analysis_id != null && r.analysis_id !== "" ? String(r.analysis_id) : "";
                  const isOpen = Boolean(analysisId && expanded[analysisId]);
                  const act = String(r.action ?? "—");
                  return (
                    <li key={analysisId || String(i)} className="rounded-lg border border-[var(--color-border)] p-3">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium">{String(r.symbol ?? "")}</span>
                        <Badge variant={act === "long" ? "ok" : act === "short" ? "danger" : "outline"}>
                          {act.toUpperCase()}
                        </Badge>
                      </div>
                      <dl className="mt-2 grid grid-cols-2 gap-2 text-xs">
                        <div>
                          <dt className="text-[var(--color-muted)]">Conf</dt>
                          <dd className="font-mono tabular-nums">{formatNum(r.confidence_score)}</dd>
                        </div>
                        <div>
                          <dt className="text-[var(--color-muted)]">Date</dt>
                          <dd className="tabular-nums">{stamp.date}</dd>
                        </div>
                      </dl>
                      <div className="mt-2">
                        <OutcomeCell row={r} />
                      </div>
                      {analysisId ? (
                        <button
                          type="button"
                          className="mt-2 text-xs underline decoration-dotted"
                          aria-expanded={isOpen}
                          onClick={() => void toggleExplain(analysisId)}
                        >
                          {isOpen ? "ซ่อนบันได" : "ดูบันไดความมั่นใจ"}
                        </button>
                      ) : null}
                      {isOpen ? (
                        <div className="mt-2">
                          {explainLoading[analysisId] ? (
                            <p className="text-xs text-[var(--color-muted)]">Loading confidence ladder…</p>
                          ) : null}
                          {explainError[analysisId] ? (
                            <p className="text-xs text-[var(--color-danger)]">{explainError[analysisId]}</p>
                          ) : null}
                          {explains[analysisId] ? (
                            <ConfidenceLadder breakdown={explains[analysisId].breakdown} />
                          ) : null}
                        </div>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
              <div className="hidden overflow-x-auto sm:block -mx-1 sm:mx-0">
                <table className="w-full md:min-w-[760px] text-left text-sm">
                  <thead className="text-[var(--color-muted)]">
                    <tr className="border-b border-[var(--color-border)]">
                      <th className="hidden md:table-cell py-2 pr-2">No</th>
                      <th className="py-2 pr-2">Date</th>
                      <th className="hidden md:table-cell py-2 pr-2">Time</th>
                      <th className="py-2 pr-2">Symbol</th>
                      <th className="hidden md:table-cell py-2 pr-2">TF</th>
                      <th className="hidden md:table-cell py-2 pr-2">Regime</th>
                      <th className="py-2 pr-2">Action</th>
                      <th className="py-2 pr-2">Outcome</th>
                      <th className="py-2 pr-2">Conf</th>
                      <th className="hidden md:table-cell py-2 pr-2">Size%</th>
                      <th className="hidden md:table-cell py-2">Thesis</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => {
                      const stamp = formatDecisionStamp(r.timestamp);
                      const analysisId =
                        r.analysis_id != null && r.analysis_id !== ""
                          ? String(r.analysis_id)
                          : "";
                      const rowKey = analysisId || String(i);
                      const isOpen = Boolean(analysisId && expanded[analysisId]);
                      return (
                        <Fragment key={rowKey}>
                          <tr className="border-b border-[var(--color-border)]/60 align-top">
                            <td className="hidden md:table-cell py-2 pr-2 tabular-nums text-[var(--color-muted)]">
                              {from + i}
                            </td>
                            <td className="py-2 pr-2 tabular-nums whitespace-nowrap">
                              {stamp.date}
                            </td>
                            <td className="hidden md:table-cell py-2 pr-2 tabular-nums whitespace-nowrap">
                              {stamp.time}
                            </td>
                            <td className="py-2 pr-2 font-medium">
                              {String(r.symbol ?? "")}
                            </td>
                            <td className="hidden md:table-cell py-2 pr-2">{String(r.timeframe ?? "")}</td>
                            <td className="hidden md:table-cell py-2 pr-2">
                              {(() => {
                                const reg = String(r.regime_state ?? "—");
                                const variant = reg === "trend_up" ? "ok" : reg === "trend_down" ? "danger" : "muted";
                                return <Badge variant={variant}>{reg}</Badge>;
                              })()}
                            </td>
                            <td className="py-2 pr-2">
                              {(() => {
                                const act = String(r.action ?? "—");
                                const variant = act === "long" ? "ok" : act === "short" ? "danger" : "outline";
                                return <Badge variant={variant}>{act.toUpperCase()}</Badge>;
                              })()}
                            </td>
                            <td className="py-2 pr-2">
                              <OutcomeCell row={r} />
                            </td>
                            <td className="py-2 pr-2 tabular-nums">
                              {analysisId ? (
                                <button
                                  type="button"
                                  className="underline decoration-dotted underline-offset-2 hover:text-[var(--color-accent)]"
                                  onClick={() => void toggleExplain(analysisId)}
                                  aria-expanded={isOpen}
                                >
                                  {formatNum(r.confidence_score)}
                                  {isOpen ? " ▾" : " ▸"}
                                </button>
                              ) : (
                                formatNum(r.confidence_score)
                              )}
                            </td>
                            <td className="hidden md:table-cell py-2 pr-2 tabular-nums">
                              {formatNum(r.size_pct_equity)}
                            </td>
                            <td className="hidden md:table-cell py-2 max-w-md text-[var(--color-muted)]">
                              {formatThesisDisplay(r.thesis)}
                            </td>
                          </tr>
                          {isOpen ? (
                            <tr className="border-b border-[var(--color-border)]/60 bg-[#121922]/60">
                              <td colSpan={11} className="px-3 py-3">
                                {explainLoading[analysisId] ? (
                                  <p className="text-xs text-[var(--color-muted)]">
                                    Loading confidence ladder…
                                  </p>
                                ) : null}
                                {explainError[analysisId] ? (
                                  <p className="text-xs text-[var(--color-danger)]">
                                    {explainError[analysisId]}
                                  </p>
                                ) : null}
                                {explains[analysisId] ? (
                                  <div className="max-w-lg">
                                    <ConfidenceLadder
                                      breakdown={explains[analysisId].breakdown}
                                    />
                                  </div>
                                ) : null}
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-[var(--color-muted)]">
                  Showing {from}–{to} of {total}
                </p>
                <div className="flex flex-wrap items-center gap-3">
                  <label className="flex items-center gap-2 text-xs text-[var(--color-muted)]">
                    Rows
                    <select
                      className="rounded-md border border-[var(--color-border)] bg-[#161d27] px-2 py-1.5 text-sm text-[var(--color-foreground)]"
                      value={pageSize}
                      onChange={(e) => changePageSize(Number(e.target.value))}
                    >
                      {PAGE_SIZES.map((n) => (
                        <option key={n} value={n}>
                          {n}
                        </option>
                      ))}
                    </select>
                  </label>
                  <Pagination className="mx-0 w-auto justify-end">
                    <PaginationContent>
                      <PaginationItem>
                        <PaginationPrevious
                          disabled={safePage <= 1 || loading}
                          onClick={() => goPage(safePage - 1)}
                        />
                      </PaginationItem>
                      <PaginationItem>
                        <span className="px-2 text-xs text-[var(--color-muted)]">
                          Page {safePage} / {pageCount}
                        </span>
                      </PaginationItem>
                      <PaginationItem>
                        <PaginationNext
                          disabled={safePage >= pageCount || loading}
                          onClick={() => goPage(safePage + 1)}
                        />
                      </PaginationItem>
                    </PaginationContent>
                  </Pagination>
                </div>
              </div>
            </>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}

function OutcomeCell({ row }: { row: Record<string, unknown> }) {
  const outcome = parseAnalysisOutcome(row.outcome);
  const copy = outcomeCopy(outcome, String(row.action ?? ""));
  // Tooltip shows owner copy too; stored reasons can carry หลักคำสอน / "doctrine".
  const title = outcome?.resolve_reason ? resolveReasonLabel(outcome.resolve_reason) : "";
  return (
    <div className="flex items-start gap-1">
      <div>
        <Badge variant={copy.variant}>{copy.label}</Badge>
        {copy.detail ? (
          <div className="mt-0.5 text-[11px] text-[var(--color-muted)]">{copy.detail}</div>
        ) : null}
      </div>
      {title ? <InfoTip label="เหตุผล" text={title} /> : null}
    </div>
  );
}

function OutcomeSummaryChips({ summary }: { summary: AnalysisSummary }) {
  const byKind = summary.by_kind || {};
  const chips: Array<{ label: string; value: number }> = [
    { label: "ทั้งหมด", value: Number(summary.total ?? 0) },
    { label: "เปิด/ปิดไม้จริง", value: Number(byKind.filled ?? 0) },
    { label: "ถือไม้เดิม", value: Number(byKind.hold ?? 0) },
    { label: "ปฏิเสธ", value: Number(byKind.rejected ?? 0) },
    { label: "หมดเวลา", value: Number(byKind.timed_out ?? 0) },
    { label: "ไม่เข้าคิว", value: Number(byKind.not_queued ?? 0) },
  ];
  const pending = Number(byKind.pending ?? 0);
  const failed = Number(byKind.failed ?? 0);
  if (pending > 0) chips.push({ label: "รออนุมัติ", value: pending });
  if (failed > 0) chips.push({ label: "ล้มเหลว", value: failed });
  return (
    <div className="flex flex-wrap gap-1.5 pt-1">
      {chips.map((chip) => (
        <Badge key={chip.label} variant="muted">
          {chip.label} {chip.value}
        </Badge>
      ))}
    </div>
  );
}
