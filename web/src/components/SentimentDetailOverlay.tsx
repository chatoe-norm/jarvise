import { ArrowUp, Coins, Gauge, Sparkles, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { SemiDynamicContext } from "@/types/marketContext";
import { cn } from "@/lib/utils";

export interface SentimentDetailOverlayProps {
  isOpen: boolean;
  isMobile: boolean;
  modalTab: "fear_greed" | "altcoin";
  setModalTab: (tab: "fear_greed" | "altcoin") => void;
  closeDetails: () => void;
  context: SemiDynamicContext | null;
  displayScore: number;
  hasFng: boolean;
  statusLabel: string;
  themeColor: string;
  staticDef: string;
  actionSuggestion?: string;
  altScore: number;
  hasAlt: boolean;
  altPhase: {
    phase: number;
    label: string;
    themeColor: string;
    desc: string;
    badgeVariant: "ok" | "default" | "muted" | "danger" | "outline";
  };
  btcDom: number | null;
  globalMcap: number | null;
  scrollToApprovalQueue: () => void;
  fngComponents: Array<{
    icon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>;
    weight: string;
    title: string;
    desc: string;
  }>;
  rotationPhases: Array<{
    step: number;
    name: string;
    th: string;
    desc: string;
  }>;
}

export function SentimentDetailOverlay({
  isOpen,
  isMobile,
  modalTab,
  setModalTab,
  closeDetails,
  context,
  displayScore,
  hasFng,
  statusLabel,
  themeColor,
  staticDef,
  actionSuggestion,
  altScore,
  hasAlt,
  altPhase,
  btcDom,
  globalMcap,
  scrollToApprovalQueue,
  fngComponents,
  rotationPhases,
}: SentimentDetailOverlayProps) {
  if (!isOpen) return null;

  return (
    <div
      className={cn(
        "fixed inset-0 z-50 bg-black/75 backdrop-blur-sm animate-fade-in",
        isMobile
          ? "flex flex-col justify-end"
          : "flex items-center justify-center p-4 sm:p-6",
      )}
      onClick={closeDetails}
    >
      <div
        className={cn(
          "w-full overflow-y-auto bg-[#0d131f] border border-[var(--color-border)] shadow-2xl transition-all",
          isMobile
            ? "max-h-[88vh] rounded-t-2xl p-5 animate-slide-up"
            : "max-w-2xl max-h-[88vh] rounded-2xl p-6 animate-scale-in",
        )}
        style={{
          boxShadow: `0 20px 50px rgba(0,0,0,0.85), 0 0 25px ${
            modalTab === "fear_greed" ? themeColor : altPhase.themeColor
          }20`,
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Mobile Sheet Drag Handle */}
        {isMobile && (
          <div className="mx-auto mb-3 h-1.5 w-12 rounded-full bg-[var(--color-border)] cursor-pointer" />
        )}

        {/* Modal Top Bar */}
        <div className="flex items-center justify-between pb-3 border-b border-[var(--color-border)]">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setModalTab("fear_greed")}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all cursor-pointer",
                modalTab === "fear_greed"
                  ? "bg-[var(--color-surface)] text-[var(--color-foreground)] border border-[var(--color-border)] shadow-sm"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              <Gauge
                className="w-3.5 h-3.5"
                style={{ color: modalTab === "fear_greed" ? themeColor : undefined }}
              />
              <span>Fear &amp; Greed</span>
            </button>
            <button
              type="button"
              onClick={() => setModalTab("altcoin")}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all cursor-pointer",
                modalTab === "altcoin"
                  ? "bg-[var(--color-surface)] text-[var(--color-foreground)] border border-[var(--color-border)] shadow-sm"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              <Coins
                className="w-3.5 h-3.5"
                style={{ color: modalTab === "altcoin" ? altPhase.themeColor : undefined }}
              />
              <span>Altcoin Season</span>
            </button>
          </div>

          <Button
            variant="ghost"
            size="sm"
            onClick={closeDetails}
            className="h-8 w-8 p-0 text-[var(--color-muted)] hover:text-[var(--color-foreground)] rounded-full cursor-pointer"
          >
            <X className="w-4 h-4" />
          </Button>
        </div>

        {/* TAB 1: FEAR & GREED DETAILS */}
        {modalTab === "fear_greed" && (
          <div className="mt-4 space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-base font-bold text-[var(--color-foreground)]">
                  Fear &amp; Greed Analysis
                </h3>
                <p className="text-xs text-[var(--color-muted)]">
                  ดัชนีวัดอารมณ์ความกลัวและความโลภของตลาด (0-100)
                </p>
              </div>
              <div className="text-right">
                <span
                  className="text-2xl font-black tabular-nums font-mono tracking-tight"
                  style={{ color: themeColor }}
                >
                  {hasFng ? displayScore : "—"}
                </span>
                <span className="block text-[11px] font-semibold" style={{ color: themeColor }}>
                  {statusLabel}
                </span>
              </div>
            </div>

            {/* Part 1: Static Definition */}
            <div className="rounded-xl border border-[var(--color-border)] p-3.5 bg-[var(--color-surface)]/50">
              <span className="text-[11px] font-semibold text-[var(--color-muted)] uppercase tracking-wider block mb-1">
                [ความหมายของสภาวะตลาดปัจจุบัน]
              </span>
              <p className="text-xs text-[var(--color-foreground)] leading-relaxed">{staticDef}</p>
            </div>

            {/* Part 2: Dynamic Market Cause */}
            {context?.dynamicMarketCause && context.dynamicMarketCause.bullets.length > 0 && (
              <div className="rounded-xl border border-[var(--color-border)] p-3.5 bg-[var(--color-surface)]/50">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-[11px] font-semibold text-[var(--color-muted)] uppercase tracking-wider flex items-center gap-1.5">
                    <Sparkles className="w-3.5 h-3.5 text-amber-400" />
                    <span>[สาเหตุตลาดวันนี้] 🤖 AI Intelligence</span>
                  </span>
                  {context.dynamicMarketCause.timestampText && (
                    <span className="text-[10px] text-[var(--color-muted)] font-mono">
                      {context.dynamicMarketCause.timestampText}
                    </span>
                  )}
                </div>
                <ul className="space-y-1.5 text-xs text-[var(--color-foreground)]">
                  {context.dynamicMarketCause.bullets.map((b, i) => (
                    <li key={i} className="flex items-start gap-2">
                      <span className="text-[var(--color-accent)] mt-0.5">•</span>
                      <span>{b}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* 5 Core Sentiment Drivers */}
            <div className="rounded-xl border border-[var(--color-border)] p-3.5 bg-[var(--color-surface)]/30">
              <span className="text-[11px] font-semibold text-[var(--color-muted)] uppercase tracking-wider block mb-2">
                [5 องค์ประกอบหลักในการคำนวณดัชนี Sentiment]
              </span>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
                {fngComponents.map((c, i) => {
                  const Icon = c.icon;
                  return (
                    <div
                      key={i}
                      className="p-2.5 rounded-lg border border-[var(--color-border)]/60 bg-[var(--color-card)]/40 flex items-start gap-2.5"
                    >
                      <div
                        className="p-1.5 rounded-md shrink-0"
                        style={{ backgroundColor: `${themeColor}15` }}
                      >
                        <Icon className="w-3.5 h-3.5" style={{ color: themeColor }} />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between">
                          <span className="font-semibold text-[11px] text-[var(--color-foreground)]">
                            {c.title}
                          </span>
                          <span className="text-[10px] font-mono text-[var(--color-muted)]">
                            {c.weight}
                          </span>
                        </div>
                        <p className="text-[10px] text-[var(--color-muted)] mt-0.5 leading-relaxed">
                          {c.desc}
                        </p>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Part 3: Action Suggestion */}
            {actionSuggestion && (
              <div
                className="rounded-xl border p-3.5 bg-gradient-to-r from-transparent to-amber-500/5"
                style={{ borderColor: `${themeColor}40` }}
              >
                <span className="text-[11px] font-semibold uppercase tracking-wider block mb-1" style={{ color: themeColor }}>
                  [⚡ คำแนะนำระบบ Antigravity]
                </span>
                <p className="text-xs text-[var(--color-foreground)] leading-relaxed">{actionSuggestion}</p>
                <div className="mt-3 flex justify-end">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={scrollToApprovalQueue}
                    className="text-xs font-medium cursor-pointer border-[var(--color-border)] hover:bg-[var(--color-surface)]"
                  >
                    <ArrowUp className="w-3 h-3 mr-1" />
                    ไปที่ Approval Queue ด้านบน
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}

        {/* TAB 2: ALTCOIN SEASON DETAILS */}
        {modalTab === "altcoin" && (
          <div className="mt-4 space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-base font-bold text-[var(--color-foreground)]">
                  Altcoin Season Index
                </h3>
                <p className="text-xs text-[var(--color-muted)]">
                  วัฏจักรการหมุนเวียนเม็ดเงินระหว่าง Bitcoin และ Altcoins รอบ 90 วัน
                </p>
              </div>
              <div className="text-right">
                <span
                  className="text-2xl font-black tabular-nums font-mono tracking-tight"
                  style={{ color: altPhase.themeColor }}
                >
                  {hasAlt ? altScore : "—"}
                </span>
                <span className="block text-[11px] font-semibold" style={{ color: altPhase.themeColor }}>
                  {altPhase.label}
                </span>
              </div>
            </div>

            {/* Active Phase Banner */}
            <div
              className="rounded-xl border p-3.5 bg-gradient-to-br from-transparent to-[var(--color-surface)]"
              style={{ borderColor: `${altPhase.themeColor}50` }}
            >
              <div className="flex items-center gap-2 mb-1.5">
                <Badge variant={altPhase.badgeVariant} className="text-[10px] font-mono">
                  Phase {altPhase.phase} of 4
                </Badge>
                <span className="text-xs font-semibold text-[var(--color-foreground)]">
                  {altPhase.label}
                </span>
              </div>
              <p className="text-xs text-[var(--color-foreground)] leading-relaxed">{altPhase.desc}</p>
            </div>

            {/* 4 Phases of Money Flow */}
            <div className="space-y-2">
              <span className="text-[11px] font-semibold text-[var(--color-muted)] uppercase tracking-wider block">
                [4 ระยะการไหลเวียนของสภาพคล่อง (Crypto Market Flow)]
              </span>
              <div className="grid grid-cols-1 gap-2">
                {rotationPhases.map((phase) => {
                  const isActive = altPhase.phase === phase.step;
                  return (
                    <div
                      key={phase.step}
                      className={cn(
                        "p-3 rounded-xl border transition-all flex items-start gap-3",
                        isActive
                          ? "border-[var(--color-accent)] bg-[var(--color-accent)]/10 shadow-sm"
                          : "border-[var(--color-border)]/60 bg-[var(--color-card)]/40 opacity-70",
                      )}
                    >
                      <span
                        className={cn(
                          "w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold shrink-0 mt-0.5",
                          isActive
                            ? "bg-[var(--color-accent)] text-black"
                            : "bg-[var(--color-surface)] text-[var(--color-muted)] border border-[var(--color-border)]",
                        )}
                      >
                        {phase.step}
                      </span>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between">
                          <span
                            className={cn(
                              "text-xs font-semibold",
                              isActive
                                ? "text-[var(--color-foreground)]"
                                : "text-[var(--color-muted)]",
                            )}
                          >
                            {phase.name}
                          </span>
                          <span className="text-[10px] text-[var(--color-muted)] font-mono">
                            {phase.th}
                          </span>
                        </div>
                        <p className="text-[11px] text-[var(--color-muted)] mt-1 leading-relaxed">
                          {phase.desc}
                        </p>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Macro Stats */}
            <div className="grid grid-cols-2 gap-2 pt-2 border-t border-[var(--color-border)] text-center text-xs">
              <div className="p-2.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]/30">
                <span className="text-[10px] text-[var(--color-muted)] block">BTC Dominance</span>
                <span className="font-mono font-bold text-sm text-[var(--color-foreground)]">
                  {btcDom != null ? `${btcDom.toFixed(1)}%` : "N/A"}
                </span>
              </div>
              <div className="p-2.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]/30">
                <span className="text-[10px] text-[var(--color-muted)] block">Total Market Cap</span>
                <span className="font-mono font-bold text-sm text-[var(--color-foreground)]">
                  {globalMcap != null
                    ? `$${(globalMcap / 1e12).toFixed(2)}T`
                    : "N/A"}
                </span>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
