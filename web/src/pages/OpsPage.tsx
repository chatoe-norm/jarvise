import { useEffect, useState } from "react";
import {
  Activity,
  BarChart3,
  BookOpen,
  Bot,
  Coins,
  Cpu,
  Layers,
  Shield,
  Sliders,
  Workflow,
} from "lucide-react";
import { CryptoUniverseCard } from "@/components/CryptoUniverseCard";
import { IngestHealthControlCard } from "@/components/IngestHealthControlCard";
import { ModelRoutingCard } from "@/components/ModelRoutingCard";
import { ObservabilityControlCard } from "@/components/ObservabilityControlCard";
import { PaperAutoDecideCard } from "@/components/PaperAutoDecideCard";
import { PipelineControlCard } from "@/components/PipelineControlCard";
import { QdrantTradingRulesCard } from "@/components/QdrantTradingRulesCard";
import { RiskCapsControlCard } from "@/components/RiskCapsControlCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, type StatusPayload } from "@/lib/api";
import { cn } from "@/lib/utils";

type OpsTabId =
  | "risk"
  | "pipeline"
  | "health"
  | "universe"
  | "auto_decide"
  | "models"
  | "rules"
  | "observability"
  | "all";

interface OpsTab {
  id: OpsTabId;
  label: string;
  labelTh: string;
  icon: React.ComponentType<{ className?: string }>;
  badge?: string;
  badgeVariant?: "ok" | "danger" | "outline" | "default" | "muted";
  indicatorColor?: string;
}

export function OpsPage({ onStatusChange }: { onStatusChange?: () => void }) {
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [activeTab, setActiveTab] = useState<OpsTabId>(() => {
    if (typeof window !== "undefined") {
      const hash = window.location.hash.replace("#", "").toLowerCase() as OpsTabId;
      if (
        [
          "risk",
          "pipeline",
          "health",
          "universe",
          "auto_decide",
          "rules",
          "observability",
          "all",
        ].includes(hash)
      ) {
        return hash;
      }
    }
    return "risk";
  });

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

  const handleTabChange = (tab: OpsTabId) => {
    setActiveTab(tab);
    if (typeof window !== "undefined") {
      window.history.replaceState(null, "", `#${tab}`);
    }
  };

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

  const qdrantPoints = (status?.qdrant as Record<string, unknown> | undefined)?.points;
  const isHealthy = status?.ingest_health?.ok;

  const tabs: OpsTab[] = [
    {
      id: "risk",
      label: "Risk Caps",
      labelTh: "เพดานความเสี่ยง",
      icon: Sliders,
      badge: "Enforced",
      badgeVariant: "ok",
      indicatorColor: "bg-emerald-400",
    },
    {
      id: "pipeline",
      label: "Pipeline & Jobs",
      labelTh: "ไปป์ไลน์ & Jobs",
      icon: Workflow,
      badge: "7 Jobs",
      badgeVariant: "outline",
      indicatorColor: "bg-violet-400",
    },
    {
      id: "health",
      label: "Ingest Health",
      labelTh: "สุขภาพแท่งเทียน",
      icon: Activity,
      badge: isHealthy == null ? undefined : isHealthy ? "OK" : "Alert",
      badgeVariant: isHealthy == null ? "outline" : isHealthy ? "ok" : "danger",
      indicatorColor: isHealthy ? "bg-emerald-400" : "bg-rose-400",
    },
    {
      id: "universe",
      label: "Universe",
      labelTh: "พอร์ตเหรียญ",
      icon: Coins,
      badge: "Crypto",
      badgeVariant: "outline",
      indicatorColor: "bg-amber-400",
    },
    {
      id: "auto_decide",
      label: "Auto-Decide",
      labelTh: "AI Autotrade",
      icon: Bot,
      badge: status?.auto_decide_config?.enabled ? "AI ON" : "AI OFF",
      badgeVariant: status?.auto_decide_config?.enabled ? "ok" : "muted",
      indicatorColor: status?.auto_decide_config?.enabled ? "bg-emerald-400" : "bg-slate-400",
    },
    {
      id: "models",
      label: "AI Models",
      labelTh: "โมเดล OpenRouter",
      icon: Cpu,
      badge: "5 Tiers",
      badgeVariant: "outline",
      indicatorColor: "bg-emerald-400",
    },
    {
      id: "rules",
      label: "Trading Rules",
      labelTh: "กฎการเทรด RAG",
      icon: BookOpen,
      badge: qdrantPoints != null ? `${String(qdrantPoints)} pts` : "Qdrant",
      badgeVariant: "outline",
      indicatorColor: "bg-blue-400",
    },
    {
      id: "observability",
      label: "Observability",
      labelTh: "การตรวจวัด",
      icon: BarChart3,
      badge: "Live",
      badgeVariant: "ok",
      indicatorColor: "bg-cyan-400",
    },
    {
      id: "all",
      label: "View All",
      labelTh: "แสดงทั้งหมด",
      icon: Layers,
      badge: "Full",
      badgeVariant: "muted",
      indicatorColor: "bg-slate-400",
    },
  ];

  return (
    <div className="space-y-5 max-w-7xl mx-auto">
      {/* Top Header */}
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

      {/* Emergency Market Safety Kill-Switch Compact Header */}
      <Card className="border-[var(--color-border)] bg-[var(--color-card)]/40">
        <CardContent className="p-3.5 sm:p-4">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div
                className={cn(
                  "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border",
                  status?.kill_switch
                    ? "border-rose-500/50 bg-rose-500/15 text-rose-400 animate-pulse"
                    : "border-emerald-500/50 bg-emerald-500/15 text-emerald-400",
                )}
              >
                <Shield className="h-5 w-5" />
              </div>
              <div className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold text-[var(--color-foreground)]">
                    Market Safety Kill-Switch
                  </span>
                  <Badge variant={status?.kill_switch ? "danger" : "ok"} className="text-[10px] py-0 px-1.5">
                    {status?.kill_switch ? "ENGAGED" : "CLEAR · NORMAL"}
                  </Badge>
                </div>
                <div className="text-xs text-[var(--color-muted)] font-mono">
                  {status?.kill_switch ? (
                    <span className="text-rose-400">
                      Circuit Breaker: {String((status as Record<string, unknown>).kill_switch_state ? ((status as Record<string, unknown>).kill_switch_state as Record<string, unknown>).reason || "Safety Triggered" : "Safety Triggered")}
                    </span>
                  ) : (
                    <span className="text-emerald-400/90">
                      All fail-closed guardrails active · Automated paper schedules permitted
                    </span>
                  )}
                </div>
              </div>
            </div>

            <div className="flex items-center gap-2 w-full sm:w-auto">
              <Button
                variant="danger"
                size="sm"
                className="flex-1 sm:flex-initial text-xs font-mono font-semibold h-8"
                disabled={busy || status?.kill_switch}
                onClick={() => void setKill("on")}
              >
                Engage Kill-Switch
              </Button>
              <Button
                variant="ok"
                size="sm"
                className="flex-1 sm:flex-initial text-xs font-mono font-semibold h-8"
                disabled={busy || !status?.kill_switch}
                onClick={() => void setKill("off")}
              >
                Clear &amp; Resume
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Tablet & Desktop Tab Menu Bar */}
      <div className="rounded-xl border border-[var(--color-border)] bg-[#0d121a]/90 p-1.5 shadow-sm backdrop-blur-md">
        <div className="flex items-center gap-1.5 overflow-x-auto pb-1 sm:pb-0 sm:flex-wrap">
          {tabs.map((t) => {
            const isActive = activeTab === t.id;
            const Icon = t.icon;
            return (
              <button
                key={t.id}
                type="button"
                onClick={() => handleTabChange(t.id)}
                className={cn(
                  "group relative flex shrink-0 items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium transition-all duration-150 cursor-pointer",
                  isActive
                    ? "bg-[#18202c] text-[var(--color-foreground)] shadow-sm border border-[var(--color-border)] font-semibold"
                    : "text-[var(--color-muted)] hover:bg-[var(--color-card)]/50 hover:text-[var(--color-foreground)] border border-transparent",
                )}
              >
                <Icon
                  className={cn(
                    "h-3.5 w-3.5 shrink-0 transition-colors",
                    isActive
                      ? "text-[var(--color-accent)]"
                      : "text-[var(--color-muted)] group-hover:text-[var(--color-foreground)]",
                  )}
                />
                <span>{t.label}</span>
                {t.badge && (
                  <Badge
                    variant={t.badgeVariant ?? "outline"}
                    className={cn(
                      "text-[10px] py-0 px-1.5 font-mono",
                      !isActive && "opacity-75",
                    )}
                  >
                    {t.badge}
                  </Badge>
                )}
                {isActive && (
                  <span
                    className={cn(
                      "absolute -bottom-1.5 left-1/2 h-0.5 w-6 -translate-x-1/2 rounded-full",
                      t.indicatorColor || "bg-[var(--color-accent)]",
                    )}
                  />
                )}
              </button>
            );
          })}
        </div>
      </div>

      {/* Tab Panels */}
      <div className="space-y-6">
        {(activeTab === "risk" || activeTab === "all") && (
          <RiskCapsControlCard
            initialCaps={status?.risk_caps}
            onRefresh={() => void load()}
          />
        )}

        {(activeTab === "pipeline" || activeTab === "all") && (
          <PipelineControlCard
            statusPayload={status as unknown as Record<string, unknown>}
            onRefresh={() => void load()}
          />
        )}

        {(activeTab === "health" || activeTab === "all") && (
          <IngestHealthControlCard
            health={status?.ingest_health}
            onRefresh={() => void load()}
          />
        )}

        {(activeTab === "universe" || activeTab === "all") && (
          <CryptoUniverseCard />
        )}

        {(activeTab === "auto_decide" || activeTab === "all") && (
          <PaperAutoDecideCard
            auto={status?.paper_auto}
            initialConfig={status?.auto_decide_config}
            onRefresh={() => void load()}
          />
        )}

        {(activeTab === "models" || activeTab === "auto_decide" || activeTab === "all") && (
          <ModelRoutingCard />
        )}

        {(activeTab === "rules" || activeTab === "all") && (
          <QdrantTradingRulesCard
            qdrant={status?.qdrant}
            ragStatus={status?.rag}
            onRefresh={() => void load()}
          />
        )}

        {(activeTab === "observability" || activeTab === "all") && (
          <ObservabilityControlCard
            onRefresh={() => void load()}
          />
        )}
      </div>
    </div>
  );
}
