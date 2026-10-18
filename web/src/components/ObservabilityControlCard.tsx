import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type ObservabilityPreviewPayload } from "@/lib/api";
import { formatNum } from "@/lib/utils";

interface ObservabilityControlCardProps {
  onRefresh?: () => void;
}

export function ObservabilityControlCard({ onRefresh }: ObservabilityControlCardProps) {
  const [data, setData] = useState<ObservabilityPreviewPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [persisting, setPersisting] = useState(false);
  const [rawOpen, setRawOpen] = useState(false);
  const [feedback, setFeedback] = useState<{ type: "ok" | "err"; msg: string } | null>(null);

  const loadPreview = async () => {
    setLoading(true);
    setFeedback(null);
    try {
      const res = await api.getObservabilityPreview();
      if (res.ok) {
        setData(res);
      }
    } catch (err) {
      setFeedback({ type: "err", msg: err instanceof Error ? err.message : String(err) });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadPreview();
  }, []);

  const handlePersist = async () => {
    setPersisting(true);
    setFeedback(null);
    try {
      const res = await api.persistObservabilitySnapshot();
      if (res.ok) {
        setFeedback({ type: "ok", msg: "บันทึก Metrics Snapshot ลงฐานข้อมูล Analytics สำเร็จ" });
        await loadPreview();
        onRefresh?.();
      } else {
        setFeedback({ type: "err", msg: "ไม่สามารถบันทึก Snapshot ได้" });
      }
    } catch (err) {
      setFeedback({ type: "err", msg: err instanceof Error ? err.message : String(err) });
    } finally {
      setPersisting(false);
    }
  };

  const hostname = typeof window !== "undefined" ? window.location.hostname : "127.0.0.1";
  const grafanaUrl = data?.endpoints?.grafana || `http://${hostname}:3000`;
  const prometheusUrl = data?.endpoints?.prometheus || `http://${hostname}:9090`;
  const metricsUrl = data?.endpoints?.web_metrics || "/metrics";

  return (
    <Card className="border-[var(--color-border)]">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2">
              <span>Observability &amp; Telemetry Control</span>
              <span className="inline-block h-2 w-2 rounded-full bg-cyan-400 shadow-[0_0_8px_rgba(34,211,238,0.8)]" />
            </CardTitle>
            <CardDescription>
              Prometheus metrics, service cluster connections, and on-demand performance snapshot persistence.
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={loading}
              onClick={() => void loadPreview()}
              className="h-11 sm:h-8 pointer-coarse:min-h-11 pointer-coarse:min-w-11 text-xs font-mono"
            >
              {loading ? "กำลังดึง…" : "↻ อัปเดต Gauges"}
            </Button>
            <Button
              type="button"
              variant="ok"
              size="sm"
              disabled={persisting}
              onClick={() => void handlePersist()}
              className="h-11 sm:h-8 pointer-coarse:min-h-11 pointer-coarse:min-w-11 text-xs font-mono font-semibold"
            >
              {persisting ? "กำลังบันทึก…" : "📸 Persist Snapshot"}
            </Button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Live Gauges Strip */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2.5 text-sm">
          {/* Equity */}
          <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/40 p-3">
            <div className="text-[11px] text-[var(--color-muted)] font-mono">Paper Equity</div>
            <div className="mt-1 font-mono font-bold text-base text-[var(--color-foreground)]">
              {data?.gauges?.paper_equity != null ? `$${formatNum(data.gauges.paper_equity)}` : "—"}
            </div>
          </div>

          {/* Open Positions */}
          <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/40 p-3">
            <div className="text-[11px] text-[var(--color-muted)] font-mono">Open Positions</div>
            <div className="mt-1 font-mono font-bold text-base text-[var(--color-foreground)]">
              {data?.gauges?.open_positions_count ?? 0}
            </div>
          </div>

          {/* Pending Approvals */}
          <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/40 p-3">
            <div className="text-[11px] text-[var(--color-muted)] font-mono">Pending Approvals</div>
            <div className="mt-1 font-mono font-bold text-base text-amber-300">
              {data?.gauges?.pending_approvals_count ?? 0}
            </div>
          </div>

          {/* Live Trading Door */}
          <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/40 p-3">
            <div className="text-[11px] text-[var(--color-muted)] font-mono">Live Door</div>
            <div className="mt-1">
              <Badge variant={data?.gauges?.live_trading ? "danger" : "muted"}>
                {data?.gauges?.live_trading ? "ARMED" : "GATED OFF"}
              </Badge>
            </div>
          </div>

          {/* Kill Switch */}
          <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/40 p-3">
            <div className="text-[11px] text-[var(--color-muted)] font-mono">Kill Switch</div>
            <div className="mt-1">
              <Badge variant={data?.gauges?.kill_switch ? "danger" : "ok"}>
                {data?.gauges?.kill_switch ? "ENGAGED" : "CLEAR"}
              </Badge>
            </div>
          </div>
        </div>

        {/* Service Links and Status */}
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-[var(--color-border)] bg-[#11161f] p-3">
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <span className="font-medium text-[var(--color-muted)]">Active Service Endpoints:</span>
            <a
              href={grafanaUrl}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-1.5 rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/60 px-2.5 py-1 text-xs hover:border-cyan-500/60 transition-colors"
            >
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-cyan-400" />
              <span>Grafana Dashboard (:3000) ↗</span>
            </a>
            <a
              href={prometheusUrl}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-1.5 rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/60 px-2.5 py-1 text-xs hover:border-cyan-500/60 transition-colors"
            >
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-orange-400" />
              <span>Prometheus TSDB (:9090) ↗</span>
            </a>
            <a
              href={metricsUrl}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-1.5 rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/60 px-2.5 py-1 text-xs hover:border-cyan-500/60 transition-colors font-mono"
            >
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-400" />
              <span>Web /metrics ↗</span>
            </a>
          </div>

          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => setRawOpen((v) => !v)}
            className="text-xs font-mono text-[var(--color-muted)] h-7"
          >
            {rawOpen ? "ซ่อน Raw Preview" : "ดู Raw Telemetry JSON"}
          </Button>
        </div>

        {/* Feedback message */}
        {feedback && (
          <div
            className={`rounded-md px-3 py-2 text-xs font-mono ${
              feedback.type === "ok"
                ? "border border-emerald-500/30 bg-emerald-950/20 text-emerald-300"
                : "border border-rose-500/30 bg-rose-950/20 text-rose-300"
            }`}
          >
            {feedback.msg}
          </div>
        )}

        {/* Raw View */}
        {rawOpen && (
          <pre className="overflow-x-auto rounded-md bg-[#0c1017] border border-[var(--color-border)] p-3 text-xs text-[var(--color-muted)] font-mono max-h-64">
            {JSON.stringify(data, null, 2)}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}
