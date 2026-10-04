import { Link } from "react-router-dom";
import { ApprovalQueue } from "@/components/ApprovalQueue";
import { EquityChart, WinLossChart } from "@/components/Charts";
import { KpiStrip } from "@/components/KpiStrip";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { DashboardPayload, PaperAutoStatus } from "@/lib/api";
import { buildEquityPoints } from "@/lib/equity";
import { relativeAge } from "@/lib/utils";

type AutoEntry = {
  id: string;
  symbol?: string;
  reason?: string;
  kind: "approved" | "deferred" | "rejected" | "failed";
};

function collectAutoEntries(auto: PaperAutoStatus | null | undefined): AutoEntry[] {
  if (!auto || auto.skipped) return [];
  const out: AutoEntry[] = [];
  for (const row of auto.approved || []) {
    out.push({ id: row.id, symbol: row.symbol, reason: row.reason, kind: "approved" });
  }
  for (const row of auto.deferred || []) {
    out.push({ id: row.id, symbol: row.symbol, reason: row.reason, kind: "deferred" });
  }
  for (const row of auto.rejected || []) {
    out.push({ id: row.id, symbol: row.symbol, reason: row.reason, kind: "rejected" });
  }
  for (const row of auto.apply_failed || []) {
    out.push({
      id: row.id,
      symbol: row.symbol,
      reason: row.error,
      kind: "failed",
    });
  }
  return out;
}

function kindVariant(
  kind: AutoEntry["kind"],
): "ok" | "muted" | "danger" | "default" {
  switch (kind) {
    case "approved":
      return "ok";
    case "deferred":
      return "muted";
    case "rejected":
    case "failed":
      return "danger";
    default: {
      const _exhaustive: never = kind;
      return _exhaustive;
    }
  }
}

function LastAutoDecideCard({
  auto,
}: {
  auto: PaperAutoStatus | null | undefined;
}) {
  const entries = collectAutoEntries(auto);
  if (!auto || entries.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Last auto-decide</CardTitle>
        <CardDescription>
          Queue empty — latest Claude / rule outcomes
          {auto.at_ms != null ? ` · ${relativeAge(auto.at_ms)}` : ""}
          {auto.model ? ` · ${auto.model}` : ""}.{" "}
          <Link to="/ops" className="text-[var(--color-accent)] underline-offset-2 hover:underline">
            Full detail on Ops
          </Link>
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="space-y-3 text-sm">
          {entries.slice(0, 6).map((e) => (
            <li key={`${e.kind}-${e.id}`} className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={kindVariant(e.kind)}>{e.kind}</Badge>
                <span className="font-medium">{e.symbol || e.id}</span>
              </div>
              <p className="text-[var(--color-muted)]">{e.reason || "—"}</p>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

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
  const queueEmpty = (data.approvals || []).length === 0;

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

      {queueEmpty ? <LastAutoDecideCard auto={data.status.paper_auto} /> : null}

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
