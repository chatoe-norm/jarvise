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
import { RiskMonitorCard } from "@/components/RiskMonitorCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { api, type StatusPayload } from "@/lib/api";
import { cn, formatDecisionStamp } from "@/lib/utils";

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
  const [refreshing, setRefreshing] = useState(false);
  const { confirm, dialog } = useConfirm();

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
          "models",
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
    setRefreshing(true);
    try {
      setStatus(await api.status());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
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
    const ok = await confirm(
      state === "on"
        ? {
            title: "เปิด Kill-Switch?",
            body: "หยุดการเติมคำสั่ง paper และการอนุมัติทั้งหมดจนกว่าจะปลดล็อก",
            confirmLabel: "เปิด Kill-Switch",
            tone: "danger",
          }
        : {
            title: "ปลด Kill-Switch และกลับมาทำงาน?",
            body: "ตรวจสอบสาเหตุที่ทำให้ระบบหยุดก่อน ตารางงาน paper อัตโนมัติจะกลับมาทำงานทันที",
            confirmLabel: "ปลดล็อก",
            reasonLabel: "เหตุผลที่ปลดล็อก (บันทึกไว้)",
          },
    );
    if (!ok) return;
    setBusy(true);
    try {
      await api.killSwitch(state, typeof ok === "string" ? ok : undefined);
      await load();
      onStatusChange?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const ksState = status?.kill_switch_state as
    | { engaged?: boolean; known?: boolean; reason?: string | null; error?: string | null }
    | undefined;
  // known:false = Redis unreadable. Approvals fail closed, but it is not a confirmed engage.
  const killMode: "loading" | "unknown" | "engaged" | "clear" = !status
    ? "loading"
    : ksState?.known === false
      ? "unknown"
      : status.kill_switch
        ? "engaged"
        : "clear";
  const qdrantPoints = (status?.qdrant as Record<string, unknown> | undefined)?.points;
  const riskHalted = (status?.risk_monitor as { halted?: unknown } | null | undefined)?.halted === true;
  const isHealthy = status?.ingest_health?.ok;

  const tabs: OpsTab[] = [
    {
      id: "risk",
      label: "Risk Caps",
      labelTh: "เพดานความเสี่ยง",
      icon: Sliders,
      badge: riskHalted ? "Halted" : status ? "Enforced" : undefined,
      badgeVariant: riskHalted ? "danger" : "ok",
      indicatorColor: riskHalted ? "bg-rose-400" : "bg-emerald-400",
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
      badge: "แผน",
      badgeVariant: "muted",
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
      badge: undefined,
      badgeVariant: "outline",
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
          <h1 className="text-xl font-bold tracking-tight text-(--color-foreground)">
            Network Operations Center (NOC)
          </h1>
          <p className="text-sm text-(--color-muted)">
            Infrastructure health, market safety kill-switch, telemetry, and pipeline schedulers.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={refreshing}
          onClick={() => void load()}
          className="border-border text-xs font-mono h-11 sm:h-8"
        >
          {refreshing ? "Polling…" : "↻ Refresh Telemetry"}
        </Button>
      </div>

      {dialog}
      {error ? <p className="text-sm text-danger">{error}</p> : null}

      {/* Emergency Market Safety Kill-Switch Compact Header */}
      <Card className="border-border bg-card/40">
        <CardContent className="p-3.5 sm:p-4">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div
                className={cn(
                  "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border",
                  killMode === "engaged"
                    ? "border-rose-500/50 bg-rose-500/15 text-rose-400 animate-pulse"
                    : killMode === "unknown"
                      ? "border-amber-500/50 bg-amber-500/15 text-amber-400"
                      : killMode === "clear"
                        ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-400"
                        : "border-border text-(--color-muted)",
                )}
              >
                <Shield className="h-5 w-5" />
              </div>
              <div className="space-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold text-(--color-foreground)">
                    Market Safety Kill-Switch
                  </span>
                  <Badge
                    variant={killMode === "engaged" ? "danger" : killMode === "clear" ? "ok" : "outline"}
                    className="text-[10px] py-0 px-1.5"
                  >
                    {killMode === "engaged"
                      ? "ENGAGED"
                      : killMode === "unknown"
                        ? "ไม่ทราบสถานะ"
                        : killMode === "clear"
                          ? "CLEAR · NORMAL"
                          : "กำลังโหลด…"}
                  </Badge>
                </div>
                <div className="text-xs text-(--color-muted) font-mono">
                  {killMode === "engaged" ? (
                    <span className="text-rose-400">Circuit Breaker: {ksState?.reason || "Safety Triggered"}</span>
                  ) : killMode === "unknown" ? (
                    <span className="text-amber-400">
                      อ่านสถานะจาก Redis ไม่ได้ ({ksState?.error || "redis"}) · การอนุมัติถูกบล็อกไว้ก่อน (fail-closed)
                    </span>
                  ) : killMode === "clear" ? (
                    <span className="text-emerald-400/90">
                      All fail-closed guardrails active · Automated paper schedules permitted
                    </span>
                  ) : null}
                </div>
              </div>
            </div>

            <div className="flex items-center gap-3 w-full sm:w-auto">
              <Button
                variant="danger"
                size="sm"
                className="flex-1 sm:flex-initial text-xs font-mono font-semibold h-11 sm:h-9"
                disabled={busy || killMode === "engaged" || killMode === "loading"}
                onClick={() => void setKill("on")}
              >
                Engage Kill-Switch
              </Button>
              <Button
                variant="ok"
                size="sm"
                className="flex-1 sm:flex-initial text-xs font-mono font-semibold h-11 sm:h-9"
                disabled={busy || killMode !== "engaged"}
                onClick={() => void setKill("off")}
              >
                Clear &amp; Resume
              </Button>
            </div>
          </div>
          <OwnerDecisionLog rows={status?.owner_decisions} />
        </CardContent>
      </Card>

      {/* Tablet & Desktop Tab Menu Bar */}
      <div className="rounded-xl border border-border bg-[#0d121a]/90 p-1.5 shadow-sm backdrop-blur-md">
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
                    ? "bg-[#18202c] text-(--color-foreground) shadow-sm border border-border font-semibold"
                    : "text-(--color-muted) hover:bg-card/50 hover:text-(--color-foreground) border border-transparent",
                )}
              >
                <Icon
                  className={cn(
                    "h-3.5 w-3.5 shrink-0 transition-colors",
                    isActive
                      ? "text-(--color-accent)"
                      : "text-(--color-muted) group-hover:text-(--color-foreground)",
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
                      t.indicatorColor || "bg-(--color-accent)",
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
        {(activeTab === "risk" || activeTab === "all") && <RiskMonitorCard monitor={status?.risk_monitor} />}

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
            gate={status?.auto_ev_gate}
            initialConfig={status?.auto_decide_config}
            onRefresh={() => void load()}
          />
        )}

        {(activeTab === "models" || activeTab === "auto_decide" || activeTab === "all") && (
          <ModelRoutingCard activeModel={status?.auto_decide_config?.model} />
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

const DECISION_KIND: Record<string, string> = {
  ev_gate_reset: "ปลดล็อกเกต EV",
  kill_switch_off: "ปลด Kill-Switch",
  kill_switch_on: "เปิด Kill-Switch",
};

function OwnerDecisionLog({ rows }: { rows: StatusPayload["owner_decisions"] }) {
  const items = (rows || []).slice(0, 5);
  return (
    <div className="mt-3 border-t border-border pt-3">
      <div className="text-xs font-medium text-(--color-foreground)">บันทึกการตัดสินใจล่าสุด</div>
      {items.length === 0 ? (
        <p className="mt-1 text-xs text-(--color-muted)">ยังไม่มีบันทึก</p>
      ) : (
        <ul className="mt-2 space-y-1">
          {items.map((row) => {
            const stamp = formatDecisionStamp(row.at_ms);
            return (
              <li key={row.id} className="text-xs text-(--color-muted)">
                <span className="font-mono text-(--color-foreground)">
                  {stamp.date} {stamp.time}
                </span>
                {" · "}
                {DECISION_KIND[row.kind] ?? row.kind}
                {" · "}
                {row.reason || "—"}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
