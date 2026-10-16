import { useEffect, useState } from "react";
import { CryptoUniverseCard } from "@/components/CryptoUniverseCard";
import { IngestHealthControlCard } from "@/components/IngestHealthControlCard";
import { ObservabilityControlCard } from "@/components/ObservabilityControlCard";
import { PaperAutoDecideCard } from "@/components/PaperAutoDecideCard";
import { PipelineControlCard } from "@/components/PipelineControlCard";
import { QdrantTradingRulesCard } from "@/components/QdrantTradingRulesCard";
import { RiskCapsControlCard } from "@/components/RiskCapsControlCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type StatusPayload } from "@/lib/api";

export function OpsPage({ onStatusChange }: { onStatusChange?: () => void }) {
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      setStatus(await api.status());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function setKill(state: "on" | "off") {
    setBusy(true);
    try {
      await api.killSwitch(state);
      await load();
      onStatusChange?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6 max-w-7xl mx-auto">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-[var(--color-foreground)]">
            Network Operations Center (NOC)
          </h1>
          <p className="text-sm text-[var(--color-muted)]">
            Infrastructure health, market safety kill-switch, telemetry, and pipeline schedulers.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => void load()}
          className="border-[var(--color-border)] text-xs font-mono"
        >
          {busy ? "Polling…" : "↻ Refresh Telemetry"}
        </Button>
      </div>

      {error ? <p className="text-sm text-[var(--color-danger)]">{error}</p> : null}

      {/* Market Safety Kill-Switch */}
      <Card className="border-[var(--color-border)]">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Market Safety Kill-Switch</CardTitle>
            <Badge variant={status?.kill_switch ? "danger" : "ok"}>
              {status?.kill_switch ? "ENGAGED — Pipeline Blocked" : "CLEAR — Systems Nominal"}
            </Badge>
          </div>
          <CardDescription>
            Master circuit breaker. When engaged, automated paper schedules are paused and order placement is blocked.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/40 p-4">
            <div className="space-y-1">
              <div className="text-xs text-[var(--color-muted)]">Circuit Breaker Status</div>
              <div className="text-sm font-medium">
                {status?.kill_switch ? (
                  <span className="text-rose-400 font-mono">
                    Engaged: {(status as Record<string, unknown>).kill_switch_state ? String(((status as Record<string, unknown>).kill_switch_state as Record<string, unknown>).reason || "Safety Triggered") : "Safety Triggered"}
                  </span>
                ) : (
                  <span className="text-[var(--color-ok)] font-mono">
                    Normal · All fail-closed safety checks green
                  </span>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2 w-full sm:w-auto">
              <Button
                variant="danger"
                size="sm"
                className="flex-1 sm:flex-initial"
                disabled={busy || status?.kill_switch}
                onClick={() => void setKill("on")}
              >
                Engage Kill-Switch
              </Button>
              <Button
                variant="ok"
                size="sm"
                className="flex-1 sm:flex-initial"
                disabled={busy || !status?.kill_switch}
                onClick={() => void setKill("off")}
              >
                Clear &amp; Resume
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* 1. Risk Caps & Guardrails (Control / Settings Component) */}
      <RiskCapsControlCard
        initialCaps={status?.risk_caps}
        onRefresh={() => void load()}
      />

      {/* 2. Observability (Live Telemetry & Snapshot Controls) */}
      <ObservabilityControlCard
        onRefresh={() => void load()}
      />

      {/* 3. Pipeline (Interactive Job Controllers & Execution Triggers) */}
      <PipelineControlCard
        statusPayload={status as unknown as Record<string, unknown>}
        onRefresh={() => void load()}
      />

      {/* Crypto Universe Management */}
      <CryptoUniverseCard />

      {/* 4. Ingest Health & One-Click Data Repair */}
      <IngestHealthControlCard
        health={status?.ingest_health}
        onRefresh={() => void load()}
      />

      {/* Paper Auto-Decide (AI Autotrade Toggle & Risk Control) */}
      <PaperAutoDecideCard
        auto={status?.paper_auto}
        initialConfig={status?.auto_decide_config}
        onRefresh={() => void load()}
      />

      {/* Qdrant Trading Rules & Query Tester */}
      <QdrantTradingRulesCard
        qdrant={status?.qdrant}
        ragStatus={status?.rag}
        onRefresh={() => void load()}
      />
    </div>
  );
}
