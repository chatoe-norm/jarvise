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
import { api, type AnalysisExplainPayload, type AnalysisSummary } from "@/lib/api";
import { outcomeCopy, parseAnalysisOutcome } from "@/lib/copy";
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

      <Card>
        <CardContent className="pt-4">
          <form className="flex flex-wrap items-end gap-3" onSubmit={onFilter}>
            <label className="text-xs text-[var(--color-muted)]">
              Symbol
              <input
                className="mt-1 block rounded-md border border-[var(--color-border)] bg-[#161d27] px-2 py-1.5 text-sm text-[var(--color-foreground)]"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                placeholder="BTCUSDT"
              />
            </label>
            <label className="text-xs text-[var(--color-muted)]">
              Timeframe
              <input
                className="mt-1 block rounded-md border border-[var(--color-border)] bg-[#161d27] px-2 py-1.5 text-sm text-[var(--color-foreground)]"
                value={timeframe}
                onChange={(e) => setTimeframe(e.target.value)}
                placeholder="4h"
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
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-[var(--color-muted)]">
                    <tr className="border-b border-[var(--color-border)]">
                      <th className="py-2 pr-2">No</th>
                      <th className="py-2 pr-2">Date</th>
                      <th className="py-2 pr-2">Time</th>
                      <th className="py-2 pr-2">Symbol</th>
                      <th className="py-2 pr-2">TF</th>
                      <th className="py-2 pr-2">Regime</th>
                      <th className="py-2 pr-2">Action</th>
                      <th className="py-2 pr-2">Outcome</th>
                      <th className="py-2 pr-2">Conf</th>
                      <th className="py-2 pr-2">Size%</th>
                      <th className="py-2">Thesis</th>
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
                            <td className="py-2 pr-2 tabular-nums text-[var(--color-muted)]">
                              {from + i}
                            </td>
                            <td className="py-2 pr-2 tabular-nums whitespace-nowrap">
                              {stamp.date}
                            </td>
                            <td className="py-2 pr-2 tabular-nums whitespace-nowrap">
                              {stamp.time}
                            </td>
                            <td className="py-2 pr-2 font-medium">
                              {String(r.symbol ?? "")}
                            </td>
                            <td className="py-2 pr-2">{String(r.timeframe ?? "")}</td>
                            <td className="py-2 pr-2">
                              <Badge variant="muted">
                                {String(r.regime_state ?? "—")}
                              </Badge>
                            </td>
                            <td className="py-2 pr-2">
                              <Badge variant="outline">
                                {String(r.action ?? "—")}
                              </Badge>
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
                            <td className="py-2 pr-2 tabular-nums">
                              {formatNum(r.size_pct_equity)}
                            </td>
                            <td className="py-2 max-w-md text-[var(--color-muted)]">
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
  const title = outcome?.resolve_reason || undefined;
  return (
    <div className="min-w-[9rem]" title={title}>
      <Badge variant={copy.variant}>{copy.label}</Badge>
      {copy.detail ? (
        <div className="mt-0.5 text-[11px] text-[var(--color-muted)]">{copy.detail}</div>
      ) : null}
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
