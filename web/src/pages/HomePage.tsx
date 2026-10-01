import { ApprovalQueue } from "@/components/ApprovalQueue";
import { EquityChart, WinLossChart } from "@/components/Charts";
import { KpiStrip } from "@/components/KpiStrip";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { DashboardPayload } from "@/lib/api";
import { buildEquityPoints } from "@/lib/equity";

export function HomePage({
  data,
  loading,
  error,
  onRefresh,
}: {
  data: DashboardPayload | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  if (loading && !data) {
    return <p className="text-sm text-[var(--color-muted)]">Loading dashboard…</p>;
  }
  if (error && !data) {
    return (
      <p className="text-sm text-[var(--color-danger)]">
        Failed to load: {error}
      </p>
    );
  }
  if (!data) return null;

  const equityPoints = buildEquityPoints(data.paper.orders || [], data.paper.equity);
  const m = data.metrics;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Now</h1>
        <p className="text-sm text-[var(--color-muted)]">
          Decide pending paper candidates, then glance at portfolio health.
        </p>
      </div>

      <ApprovalQueue
        rows={data.approvals}
        killSwitch={data.status.kill_switch}
        onChanged={onRefresh}
      />

      <KpiStrip
        items={[
          { label: "Paper equity", value: data.paper.equity, hint: "Simulated ledger" },
          { label: "Cash", value: data.paper.cash },
          {
            label: "Expected value",
            value: m.expected_value_ev,
            hint: `${m.closed_trades ?? 0} closed trades`,
          },
          {
            label: "Open positions",
            value: (data.paper.positions || []).length,
            hint: "Paper only",
          },
        ]}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Equity trajectory</CardTitle>
          </CardHeader>
          <CardContent>
            <EquityChart points={equityPoints} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Win / loss mix</CardTitle>
          </CardHeader>
          <CardContent>
            <WinLossChart
              wins={Number(m.wins || 0)}
              losses={Number(m.losses || 0)}
              scratches={Number(m.scratches || 0)}
            />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
