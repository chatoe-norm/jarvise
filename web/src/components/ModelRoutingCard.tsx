import { useEffect, useState } from "react";
import {
  Bot,
  CheckCircle2,
  Cpu,
  Layers,
  RefreshCw,
  Scale,
  Zap,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { AgentProfile, ModelRoutingMatrix } from "@/types/modelRouting";
import { modelRoutingService } from "@/services/modelRoutingService";
import { syncModelRoutingAction } from "@/actions/model-routing.action";
import { cn } from "@/lib/utils";

export function ModelRoutingCard() {
  const [matrix, setMatrix] = useState<ModelRoutingMatrix>(() => modelRoutingService.getMatrix());
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState<string | null>(null);

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
      if (res.error) {
        setSyncMessage(res.error);
      } else {
        setSyncMessage("Synchronized with OpenClaw Gateway");
      }
    } catch (err) {
      setSyncMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setSyncing(false);
    }
  }

  const agentsList: AgentProfile[] = Object.values(matrix.agents);

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
                <span>Multi-Tier Model Routing Matrix</span>
                <Badge variant="outline" className="text-[10px] font-mono text-emerald-400 border-emerald-500/30">
                  OpenRouter 5-Tier
                </Badge>
              </CardTitle>
              <CardDescription className="text-xs">
                Autonomous agent role specialization, fallback tiers, and ephemeral prompt caching.
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
              <span>{syncing ? "Syncing..." : "Sync OpenClaw"}</span>
            </Button>
          </div>
        </div>

        {syncMessage && (
          <div className="mt-2 text-[11px] font-mono text-[var(--color-muted)] flex items-center gap-1.5">
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
            <span>{syncMessage}</span>
          </div>
        )}
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Guardrail and Efficiency Highlights Banner */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5 text-xs">
          <div className="p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]/40 flex items-start gap-2.5">
            <div className="p-1.5 rounded-lg bg-blue-500/10 border border-blue-500/20 shrink-0">
              <Zap className="w-3.5 h-3.5 text-blue-400" />
            </div>
            <div>
              <span className="font-semibold text-[var(--color-foreground)] block">Ephemeral Caching</span>
              <span className="text-[11px] text-[var(--color-muted)]">
                Doctrine prompts use <code className="text-blue-300">cache_control: ephemeral</code> for 90% cost reduction.
              </span>
            </div>
          </div>

          <div className="p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]/40 flex items-start gap-2.5">
            <div className="p-1.5 rounded-lg bg-amber-500/10 border border-amber-500/20 shrink-0">
              <Layers className="w-3.5 h-3.5 text-amber-400" />
            </div>
            <div>
              <span className="font-semibold text-[var(--color-foreground)] block">Claw-Compactor (300KB)</span>
              <span className="text-[11px] text-[var(--color-muted)]">
                Sessions exceeding 300KB auto-compact history to preserve context and limit costs.
              </span>
            </div>
          </div>

          <div className="p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)]/40 flex items-start gap-2.5">
            <div className="p-1.5 rounded-lg bg-emerald-500/10 border border-emerald-500/20 shrink-0">
              <Scale className="w-3.5 h-3.5 text-emerald-400" />
            </div>
            <div>
              <span className="font-semibold text-[var(--color-foreground)] block">Automatic Failover</span>
              <span className="text-[11px] text-[var(--color-muted)]">
                Rate-limit (429) &amp; 5xx errors smoothly trigger fallback models in sequence.
              </span>
            </div>
          </div>
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
                        {agent.name}
                      </span>
                      <span className="text-[10px] font-mono text-[var(--color-muted)]">
                        {agent.role}
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
                <span className="text-emerald-400 font-medium">Ready</span>
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
