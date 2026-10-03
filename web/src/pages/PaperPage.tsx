import { useEffect, useState } from "react";
import { EquityChart, WinLossChart } from "@/components/Charts";
import { KpiStrip } from "@/components/KpiStrip";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type MetricsPayload, type PaperPayload } from "@/lib/api";
import { buildEquityPoints } from "@/lib/equity";
import { formatNum } from "@/lib/utils";

export function PaperPage() {
  const [paper, setPaper] = useState<PaperPayload | null>(null);
  const [metrics, setMetrics] = useState<MetricsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    setError(null);
    try {
      const [p, m] = await Promise.all([api.paper(), api.metrics()]);
      setPaper(p);
      setMetrics(m);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function persist() {
    setBusy(true);
    try {
      await api.persistMetrics();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const points = buildEquityPoints(paper?.orders || [], paper?.equity);
  const positions = paper?.positions || [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Paper ledger</h1>
          <p className="text-sm text-[var(--color-muted)]">
            Simulated portfolio — no exchange orders from this page.
          </p>
        </div>
        <Button variant="outline" disabled={busy} onClick={() => void persist()}>
          Persist metrics snapshot
        </Button>
      </div>

      {error ? <p className="text-sm text-[var(--color-danger)]">{error}</p> : null}

      <KpiStrip
        items={[
          { label: "Equity", value: paper?.equity },
          { label: "Cash", value: paper?.cash },
          { label: "EV", value: metrics?.expected_value_ev },
          {
            label: "Win rate",
            value: metrics?.win_rate != null ? Number(metrics.win_rate) * 100 : null,
            hint: "percent",
          },
        ]}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Equity curve</CardTitle>
            <CardDescription>Derived from fill history (visual estimate)</CardDescription>
          </CardHeader>
          <CardContent>
            <EquityChart points={points} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Expectancy mix</CardTitle>
            <CardDescription>
              MDD {formatNum(metrics?.max_drawdown_pct)}%
              {metrics?.ratios_ready
                ? ` · Sharpe ${formatNum(metrics.sharpe_ratio)}`
                : ` · ratios need ≥${metrics?.need_trades_for_ratios ?? "—"} trades`}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <WinLossChart
              wins={Number(metrics?.wins || 0)}
              losses={Number(metrics?.losses || 0)}
              scratches={Number(metrics?.scratches || 0)}
            />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Auto-decide quality</CardTitle>
          <CardDescription>
            Closed-trade EV by decision source (30d). Soft gate never loosens risk.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          {(() => {
            const src = metrics?.by_decision_source_30d || {};
            const keys = Object.keys(src);
            if (keys.length === 0) {
              return (
                <p className="text-[var(--color-muted)]">
                  No attributed closed trades yet. Approve fills stamp{" "}
                  <code>decision_source</code> for later EV splits.
                </p>
              );
            }
            const gate = metrics?.auto_ev_gate;
            return (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="text-[var(--color-muted)]">
                      <tr className="border-b border-[var(--color-border)]">
                        <th className="py-2 pr-3">Source</th>
                        <th className="py-2 pr-3">N</th>
                        <th className="py-2 pr-3">EV</th>
                        <th className="py-2 pr-3">Win rate</th>
                      </tr>
                    </thead>
                    <tbody>
                      {keys.map((k) => {
                        const row = src[k];
                        return (
                          <tr key={k} className="border-b border-[var(--color-border)]/60">
                            <td className="py-2 pr-3 font-medium">{k}</td>
                            <td className="py-2 pr-3 tabular-nums">{row?.closed_trades ?? 0}</td>
                            <td className="py-2 pr-3 tabular-nums">
                              {formatNum(row?.expected_value_ev)}
                            </td>
                            <td className="py-2 pr-3 tabular-nums">
                              {row?.win_rate != null
                                ? `${formatNum(Number(row.win_rate) * 100)}%`
                                : "—"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                {gate?.enabled ? (
                  <p className="text-xs text-[var(--color-muted)]">
                    Soft EV gate:{" "}
                    {gate.blocked
                      ? `blocking auto-decide (EV ${formatNum(gate.ev)} < ${formatNum(gate.min_ev)}, N=${gate.n})`
                      : `armed (min EV ${formatNum(gate.min_ev)}, need N≥${gate.min_n}; now N=${gate.n}, EV ${formatNum(gate.ev)})`}
                  </p>
                ) : (
                  <p className="text-xs text-[var(--color-muted)]">
                    Soft EV gate off — set <code>JARVISE_AUTO_DECIDE_MIN_EV</code> to enable.
                  </p>
                )}
              </>
            );
          })()}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Open positions</CardTitle>
        </CardHeader>
        <CardContent>
          {positions.length === 0 ? (
            <p className="text-sm text-[var(--color-muted)]">
              No open paper positions. Run <code>jarvise paper run</code> then Approve.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-[var(--color-muted)]">
                  <tr className="border-b border-[var(--color-border)]">
                    <th className="py-2 pr-3">Symbol</th>
                    <th className="py-2 pr-3">Side</th>
                    <th className="py-2 pr-3">Qty</th>
                    <th className="py-2 pr-3">Entry</th>
                    <th className="py-2">uPnL</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p, i) => (
                    <tr key={i} className="border-b border-[var(--color-border)]/60">
                      <td className="py-2 pr-3">{String(p.symbol ?? "")}</td>
                      <td className="py-2 pr-3">{String(p.side ?? "")}</td>
                      <td className="py-2 pr-3 tabular-nums">{formatNum(p.qty)}</td>
                      <td className="py-2 pr-3 tabular-nums">{formatNum(p.entry_price)}</td>
                      <td className="py-2 tabular-nums">{formatNum(p.unrealized_pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
