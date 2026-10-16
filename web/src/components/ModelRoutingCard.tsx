import { useEffect, useState } from "react";
import { AlertTriangle, Bot, CheckCircle2, Cpu, RefreshCw } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { AgentProfile, ModelRoutingMatrix } from "@/types/modelRouting";
import { modelRoutingService } from "@/services/modelRoutingService";
import { syncModelRoutingAction } from "@/actions/model-routing.action";
import { cn } from "@/lib/utils";

function sourceLabel(source: string | undefined): string {
  if (!source) return "ไม่ทราบแหล่งที่มา";
  if (source === "defaults") return "ค่าเริ่มต้นในโค้ด (ไม่พบ openclaw.json ใน container ของเว็บ)";
  return `อ่านจาก ${source}`;
}

/** The trading-rules judge role is shown in owner copy, not as the "Doctrine" identifier. */
function displayName(agent: AgentProfile): string {
  return agent.role === "DOCTRINE_JUDGE" ? "Trading Rules Judge (กฎการเทรด)" : agent.name;
}

export function ModelRoutingCard({ activeModel }: { activeModel?: string | null } = {}) {
  const [matrix, setMatrix] = useState<ModelRoutingMatrix>(() => modelRoutingService.getMatrix());
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState<string | null>(null);
  const [syncFailed, setSyncFailed] = useState(false);

  useEffect(() => {
    void handleSync();
  }, []);

  async function handleSync() {
    setSyncing(true);
    setSyncMessage(null);
    try {
      const res = await syncModelRoutingAction();
      if (res.data) {
        setMatrix(res.data);
      }
      setSyncFailed(Boolean(res.error));
      setSyncMessage(res.error ?? sourceLabel(res.data?.source));
    } catch (err) {
      setSyncFailed(true);
      setSyncMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setSyncing(false);
    }
  }

  // The API is not schema-checked; a payload without agents must not blank the page.
  const agentsList: AgentProfile[] =
    matrix?.agents && typeof matrix.agents === "object" ? Object.values(matrix.agents) : [];

  return (
    <Card className="relative overflow-hidden border-[var(--color-border)] bg-[var(--color-card)]/80 backdrop-blur-sm">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center">
              <Bot className="w-4 h-4 text-emerald-400" />
            </div>
            <div>
              <CardTitle className="text-base font-semibold flex items-center gap-2">
                <span>Model Routing Plan</span>
                <Badge variant="muted" className="text-[10px] font-mono">
                  ยังไม่เชื่อมต่อการทำงาน
                </Badge>
              </CardTitle>
              <CardDescription className="text-xs">
                แผนการกำหนดโมเดลต่อบทบาท — ยังไม่มีงานใดในระบบเรียกใช้ตารางนี้
              </CardDescription>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={syncing}
              onClick={() => void handleSync()}
              className="text-xs h-8 border-[var(--color-border)] hover:bg-[var(--color-surface)] cursor-pointer"
            >
              <RefreshCw className={cn("w-3.5 h-3.5 mr-1.5", syncing && "animate-spin text-emerald-400")} />
              <span>{syncing ? "กำลังโหลด…" : "โหลดใหม่"}</span>
            </Button>
          </div>
        </div>

        {syncMessage && (
          <div className="mt-2 text-[11px] font-mono text-[var(--color-muted)] flex items-center gap-1.5">
            {syncFailed ? (
              <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
            ) : (
              <CheckCircle2 className="w-3.5 h-3.5 text-[var(--color-muted)] shrink-0" />
            )}
            <span>{syncMessage}</span>
          </div>
        )}
      </CardHeader>

      <CardContent className="space-y-4">
        <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]/40 p-3 text-xs space-y-1">
          <div className="font-semibold text-[var(--color-foreground)]">ที่ระบบใช้งานจริงตอนนี้</div>
          <p className="text-[var(--color-muted)]">
            Paper auto-decide (Claude รีวิวการ์ด) ใช้โมเดล{" "}
            <code className="text-[var(--color-foreground)]">{activeModel || "—"}</code> จากการตั้งค่า auto-decide
            ไม่ได้อ่านจากตารางด้านล่าง
          </p>
        </div>

        {/* 5-Tier Agent Routing Grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {agentsList.map((agent) => (
            <div
              key={agent.role}
              className="p-3.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] flex flex-col justify-between hover:border-[var(--color-border)]/80 transition-all shadow-sm"
            >
              <div>
                {/* Agent Header */}
                <div className="flex items-center justify-between gap-2 pb-2 border-b border-[var(--color-border)]/60">
                  <div className="flex items-center gap-2">
                    <span className="text-lg select-none">{agent.emoji}</span>
                    <div>
                      <span className="font-semibold text-xs text-[var(--color-foreground)] block leading-tight">
                        {displayName(agent)}
                      </span>
                      <span className="text-[10px] font-mono text-[var(--color-muted)]">
                        {agent.key}
                      </span>
                    </div>
                  </div>
                  <Badge variant="outline" className="text-[10px] font-mono">
                    T={agent.temperature.toFixed(1)}
                  </Badge>
                </div>

                {/* Core Task */}
                <p className="text-[11px] text-[var(--color-muted)] my-2.5 leading-relaxed line-clamp-2">
                  {agent.coreTask}
                </p>

                {/* Primary Model */}
                <div className="space-y-1 mt-2">
                  <span className="text-[10px] uppercase font-semibold text-[var(--color-muted)] tracking-wider block">
                    Primary Model
                  </span>
                  <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-[var(--color-surface)] border border-[var(--color-border)]/60 font-mono text-xs text-emerald-400 font-semibold truncate">
                    <Cpu className="w-3 h-3 shrink-0" />
                    <span className="truncate">{agent.primaryModel}</span>
                  </div>
                </div>

                {/* Fallbacks */}
                {agent.fallbackModels && agent.fallbackModels.length > 0 && (
                  <div className="space-y-1 mt-2">
                    <span className="text-[10px] uppercase font-semibold text-[var(--color-muted)] tracking-wider block">
                      Fallback Tiers
                    </span>
                    <div className="space-y-1">
                      {agent.fallbackModels.map((fb, idx) => (
                        <div
                          key={fb}
                          className="flex items-center gap-1.5 px-2 py-0.5 rounded bg-[var(--color-surface)]/60 border border-[var(--color-border)]/40 font-mono text-[10px] text-[var(--color-muted)] truncate"
                        >
                          <span className="text-[9px] text-[var(--color-muted)]">#{idx + 1}</span>
                          <span className="truncate">{fb}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* Footer status */}
              <div className="mt-3 pt-2 border-t border-[var(--color-border)]/40 flex items-center justify-between text-[10px] text-[var(--color-muted)]">
                <span className="font-mono">Cache: {agent.cacheControl}</span>
                <span className="font-medium">แผน</span>
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
