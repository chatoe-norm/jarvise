import { useCallback, useEffect, useState, type FormEvent } from "react";
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
import { api } from "@/lib/api";
import { formatNum } from "@/lib/utils";

const PAGE_SIZES = [10, 15, 20, 50, 100] as const;

export function DecisionsPage() {
  const [symbol, setSymbol] = useState("");
  const [timeframe, setTimeframe] = useState("");
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(10);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Decisions</h1>
        <p className="text-sm text-[var(--color-muted)]">
          Latest analysis output — regime, action, and thesis.
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
                      <th className="py-2 pr-2">Symbol</th>
                      <th className="py-2 pr-2">TF</th>
                      <th className="py-2 pr-2">Regime</th>
                      <th className="py-2 pr-2">Action</th>
                      <th className="py-2 pr-2">Conf</th>
                      <th className="py-2 pr-2">Size%</th>
                      <th className="py-2">Thesis</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r, i) => (
                      <tr
                        key={i}
                        className="border-b border-[var(--color-border)]/60 align-top"
                      >
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
                        <td className="py-2 pr-2 tabular-nums">
                          {formatNum(r.confidence_score)}
                        </td>
                        <td className="py-2 pr-2 tabular-nums">
                          {formatNum(r.size_pct_equity)}
                        </td>
                        <td className="py-2 max-w-md text-[var(--color-muted)]">
                          {String(r.thesis ?? "")}
                        </td>
                      </tr>
                    ))}
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
