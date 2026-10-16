import { useState } from "react";
import {
  Activity,
  ArrowUp,
  Gauge,
  Info,
  Sparkles,
  X,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ApprovalRow, NewsHome, StatusPayload } from "@/lib/api";
import { useMarketContext } from "@/controllers/useMarketContext";

export interface MarketContextCardProps {
  status: StatusPayload;
  news?: NewsHome | null;
  approvals?: ApprovalRow[];
}

export function MarketContextCard({ status, news, approvals }: MarketContextCardProps) {
  const fng = status.fear_greed_index;
  const hasFng = typeof fng === "number" && Number.isFinite(fng);
  const alt = status.altcoin_season_index;
  const hasAlt = typeof alt === "number" && Number.isFinite(alt);

  const {
    context,
    displayScore,
    isOpen,
    isMobile,
    openDetails,
    closeDetails,
    toggleDetails,
    scrollToApprovalQueue,
  } = useMarketContext({
    score: hasFng ? fng : null,
    news,
    approvals,
  });

  const [desktopHover, setDesktopHover] = useState(false);

  // If no F&G score is available in status, show fallback state
  if (!hasFng) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Market Context</CardTitle>
          <CardDescription>
            Macro sentiment &amp; dominance context (not a directional trade signal)
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2 text-sm">
          <div className="rounded-lg border border-[var(--color-border)] p-3">
            <div className="text-xs text-[var(--color-muted)]">Fear &amp; Greed Index</div>
            <p className="mt-1 text-[var(--color-muted)]">
              {status.sentiment_note || "ไม่มีข้อมูล"}
            </p>
          </div>
          <div className="rounded-lg border border-[var(--color-border)] p-3">
            <div className="text-xs text-[var(--color-muted)]">Altcoin Season Index</div>
            <div className="mt-1.5">
              {hasAlt ? (
                <span className="text-2xl font-bold tabular-nums font-mono text-[var(--color-foreground)]">
                  {alt}
                </span>
              ) : (
                <div className="flex items-center gap-2">
                  <span className="text-sm text-[var(--color-muted)]">Bitcoin Dominant</span>
                  <Badge variant="outline" className="text-[10px]">
                    Macro Context
                  </Badge>
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  const themeColor = context?.themeColor || "#FDD835";
  const statusLabel = context?.statusLabel || "Loading…";
  const staticDef = context?.staticDefinition;
  const dynamicBullets = context?.dynamicMarketCause.bullets || [];
  const timestampText = context?.dynamicMarketCause.timestampText;
  const actionSuggestion = context?.systemActionSuggestion.suggestion;

  const showDesktopPopover = !isMobile && (desktopHover || isOpen);

  return (
    <Card className="relative overflow-visible">
      {/* Card Header (Desktop & Tablet) */}
      <CardHeader className="hidden md:block pb-2">
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-base font-semibold">Market Context</CardTitle>
            <CardDescription>
              Macro sentiment &amp; dominance context (บริบทตลาด ไม่ใช่สัญญาณเปิดออเดอร์)
            </CardDescription>
          </div>
          <Badge variant="outline" className="text-[11px] font-mono">
            Semi-Dynamic Intel
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="p-3 md:p-6">
        {/* MOBILE PWA COLLAPSED VIEW (60px - 70px single-line bar) */}
        <div className="block md:hidden">
          <div
            role="button"
            tabIndex={0}
            onClick={openDetails}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openDetails();
              }
            }}
            className="flex items-center justify-between w-full h-[64px] px-3.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/90 cursor-pointer active:scale-[0.99] transition-all touch-target-safe select-none"
            style={{
              boxShadow: `0 0 12px ${themeColor}15`,
            }}
          >
            {/* Left: Gauge Icon + Title */}
            <div className="flex items-center gap-2.5">
              <div
                className="w-8 h-8 rounded-full flex items-center justify-center shrink-0"
                style={{ backgroundColor: `${themeColor}20` }}
              >
                <Gauge className="w-4 h-4" style={{ color: themeColor }} />
              </div>
              <div className="flex flex-col">
                <span className="text-xs font-semibold text-[var(--color-foreground)]">
                  Fear &amp; Greed
                </span>
                <span className="text-[10px] text-[var(--color-muted)]">Context Layer</span>
              </div>
            </div>

            {/* Right: Score + Badge + Info Icon */}
            <div className="flex items-center gap-2">
              <span
                className="text-xl font-extrabold tabular-nums font-mono tracking-tight"
                style={{
                  color: themeColor,
                  textShadow: `0 0 10px ${themeColor}60`,
                }}
              >
                {displayScore}
              </span>
              <span
                className="px-2 py-0.5 rounded text-[11px] font-medium shrink-0"
                style={{
                  backgroundColor: `${themeColor}20`,
                  color: themeColor,
                  border: `1px solid ${themeColor}40`,
                }}
              >
                {statusLabel}
              </span>
              <div className="w-7 h-7 rounded-full flex items-center justify-center text-[var(--color-muted)] hover:text-[var(--color-foreground)]">
                <Info className="w-4 h-4" />
              </div>
            </div>
          </div>
        </div>

        {/* DESKTOP GRID VIEW (Fear & Greed + Altcoin Season) */}
        <div className="hidden md:grid gap-4 sm:grid-cols-2 text-sm">
          {/* F&G Card with Hover Popover */}
          <div
            className="relative rounded-lg border border-[var(--color-border)] p-3.5 bg-[var(--color-card)]/60 hover:bg-[var(--color-card)] transition-colors cursor-pointer group"
            onMouseEnter={() => setDesktopHover(true)}
            onMouseLeave={() => setDesktopHover(false)}
            onClick={toggleDetails}
          >
            <div className="flex items-center justify-between text-xs text-[var(--color-muted)]">
              <div className="flex items-center gap-1.5">
                <Gauge className="w-3.5 h-3.5" style={{ color: themeColor }} />
                <span>Fear &amp; Greed Index</span>
              </div>
              <button
                type="button"
                className="p-1 rounded hover:bg-[var(--color-border)]/50 text-[var(--color-muted)] group-hover:text-[var(--color-accent)] transition-colors touch-target-safe"
                aria-label="ดูรายละเอียดจิตวิทยาตลาด"
              >
                <Info className="w-3.5 h-3.5" />
              </button>
            </div>

            <div className="mt-2 flex flex-wrap items-center gap-3">
              <span
                className="text-3xl font-extrabold tabular-nums font-mono"
                style={{
                  color: themeColor,
                  textShadow: `0 0 16px ${themeColor}55`,
                }}
              >
                {displayScore}
              </span>
              <span
                className="px-2.5 py-1 rounded-md text-xs font-semibold"
                style={{
                  backgroundColor: `${themeColor}18`,
                  color: themeColor,
                  border: `1px solid ${themeColor}35`,
                  boxShadow: `0 0 8px ${themeColor}20`,
                }}
              >
                {statusLabel}
              </span>
            </div>

            <p className="mt-2 text-xs text-[var(--color-muted)] line-clamp-1">
              {staticDef}
            </p>

            {/* DESKTOP SMART POPOVER / TOOLTIP */}
            {showDesktopPopover && (
              <div
                className="absolute z-50 left-0 top-full mt-2 w-96 p-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] shadow-2xl animate-fade-in backdrop-blur-md"
                style={{
                  boxShadow: `0 12px 36px rgba(0,0,0,0.6), 0 0 18px ${themeColor}20`,
                }}
                onClick={(e) => e.stopPropagation()}
              >
                {/* Popover Header */}
                <div className="flex items-center justify-between pb-2 border-b border-[var(--color-border)]/80">
                  <div className="flex items-center gap-2">
                    <span className="text-base font-bold text-[var(--color-foreground)]">
                      📊 Fear &amp; Greed: {displayScore}
                    </span>
                    <Badge
                      variant="outline"
                      className="text-[11px] font-semibold"
                      style={{ color: themeColor, borderColor: `${themeColor}50` }}
                    >
                      {statusLabel}
                    </Badge>
                  </div>
                  <button
                    type="button"
                    onClick={closeDetails}
                    className="p-1 rounded text-[var(--color-muted)] hover:text-[var(--color-foreground)]"
                  >
                    <X className="w-4 h-4" />
                  </button>
                </div>

                {/* Section 1: Definition */}
                <div className="mt-3 text-xs text-[var(--color-muted)] leading-relaxed">
                  <span className="font-semibold text-[var(--color-foreground)]">
                    [ความหมาย]:
                  </span>{" "}
                  {staticDef}
                </div>

                {/* Section 2: AI Intelligence */}
                <div className="mt-3 p-2.5 rounded-lg bg-[var(--color-background)]/70 border border-[var(--color-border)]/70">
                  <div className="flex items-center justify-between text-[11px] font-semibold text-[var(--color-accent)] mb-1">
                    <div className="flex items-center gap-1.5">
                      <Sparkles className="w-3.5 h-3.5" />
                      <span>[สาเหตุตลาดวันนี้] 🤖 AI Intelligence</span>
                    </div>
                    {timestampText ? (
                      <span className="text-[10px] text-[var(--color-muted)] font-mono">
                        {timestampText}
                      </span>
                    ) : null}
                  </div>
                  <ul className="space-y-1 text-xs text-[var(--color-foreground)]/90">
                    {dynamicBullets.map((bullet, idx) => (
                      <li key={idx} className="flex items-start gap-1.5">
                        <span className="text-[var(--color-accent)] shrink-0">•</span>
                        <span>{bullet}</span>
                      </li>
                    ))}
                  </ul>
                </div>

                {/* Section 3: Antigravity Action Suggestion */}
                <div
                  className="mt-3 p-2.5 rounded-lg border text-xs"
                  style={{
                    backgroundColor: `${themeColor}10`,
                    borderColor: `${themeColor}35`,
                  }}
                >
                  <div className="flex items-center gap-1 font-semibold mb-1" style={{ color: themeColor }}>
                    <Activity className="w-3.5 h-3.5" />
                    <span>[⚡ คำแนะนำระบบ Antigravity]</span>
                  </div>
                  <p className="text-[var(--color-foreground)] leading-snug">
                    {actionSuggestion}
                  </p>
                </div>

                {/* Quick Action Button */}
                <div className="mt-3 pt-2 border-t border-[var(--color-border)]/80 flex justify-end">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={scrollToApprovalQueue}
                    className="w-full flex items-center justify-center gap-1.5 text-xs font-semibold touch-target-safe"
                  >
                    <span>ไปที่ Approval Queue</span>
                    <ArrowUp className="w-3.5 h-3.5" />
                  </Button>
                </div>
              </div>
            )}
          </div>

          {/* Altcoin Season Index */}
          <div className="rounded-lg border border-[var(--color-border)] p-3.5 bg-[var(--color-card)]/60">
            <div className="text-xs text-[var(--color-muted)]">Altcoin Season Index</div>
            <div className="mt-2">
              {hasAlt ? (
                <span className="text-3xl font-extrabold tabular-nums font-mono text-[var(--color-foreground)]">
                  {alt}
                </span>
              ) : (
                <div className="flex items-center gap-2">
                  <span className="text-sm text-[var(--color-muted)]">Bitcoin Dominant</span>
                  <Badge variant="outline" className="text-[10px]">
                    Macro Context
                  </Badge>
                </div>
              )}
            </div>
            <p className="mt-2 text-xs text-[var(--color-muted)]">
              ประเมินการหมุนเวียนเม็ดเงินระหว่าง BTC และ Altcoins ในภาพรวม
            </p>
          </div>
        </div>
      </CardContent>

      {/* MOBILE PWA BOTTOM SHEET (DRAWER) */}
      {isMobile && isOpen && (
        <div
          className="fixed inset-0 z-50 flex flex-col justify-end bg-black/75 backdrop-blur-sm animate-fade-in"
          onClick={closeDetails}
        >
          <div
            className="w-full max-h-[85vh] overflow-y-auto rounded-t-2xl border-t border-[var(--color-border)] bg-[var(--color-card)] p-5 animate-slide-up shadow-2xl"
            style={{
              boxShadow: `0 -8px 30px rgba(0,0,0,0.8), 0 0 20px ${themeColor}20`,
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Drawer Handle for Swipe-down gesture cue */}
            <div className="mx-auto mb-4 h-1.5 w-12 rounded-full bg-[var(--color-border)] cursor-pointer" />

            {/* Header */}
            <div className="flex items-center justify-between pb-3 border-b border-[var(--color-border)]">
              <div className="flex items-center gap-2">
                <span className="text-lg font-bold text-[var(--color-foreground)]">
                  📊 Fear &amp; Greed: {displayScore}
                </span>
                <span
                  className="px-2.5 py-0.5 rounded text-xs font-semibold"
                  style={{
                    backgroundColor: `${themeColor}20`,
                    color: themeColor,
                    border: `1px solid ${themeColor}40`,
                  }}
                >
                  {statusLabel}
                </span>
              </div>
              <button
                type="button"
                onClick={closeDetails}
                className="w-10 h-10 flex items-center justify-center rounded-full bg-[var(--color-border)]/40 text-[var(--color-muted)] active:scale-95 touch-target-safe"
                aria-label="ปิดหน้าต่าง"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Section 1: Definition */}
            <div className="mt-4">
              <div className="text-xs font-semibold text-[var(--color-muted)] mb-1 uppercase tracking-wider">
                [ความหมาย]
              </div>
              <p className="text-sm text-[var(--color-foreground)] leading-relaxed">
                {staticDef}
              </p>
            </div>

            {/* Section 2: AI Intelligence */}
            <div className="mt-4 p-3.5 rounded-xl bg-[var(--color-background)] border border-[var(--color-border)]">
              <div className="flex items-center justify-between text-xs font-semibold text-[var(--color-accent)] mb-2">
                <div className="flex items-center gap-1.5">
                  <Sparkles className="w-4 h-4" />
                  <span>[สาเหตุตลาดวันนี้] 🤖 AI Intelligence</span>
                </div>
                {timestampText ? (
                  <span className="text-[11px] text-[var(--color-muted)] font-mono">
                    {timestampText}
                  </span>
                ) : null}
              </div>
              <ul className="space-y-2 text-sm text-[var(--color-foreground)]/90">
                {dynamicBullets.map((bullet, idx) => (
                  <li key={idx} className="flex items-start gap-2">
                    <span className="text-[var(--color-accent)] font-bold shrink-0">•</span>
                    <span>{bullet}</span>
                  </li>
                ))}
              </ul>
            </div>

            {/* Section 3: Antigravity Action Suggestion */}
            <div
              className="mt-4 p-3.5 rounded-xl border text-sm"
              style={{
                backgroundColor: `${themeColor}12`,
                borderColor: `${themeColor}40`,
              }}
            >
              <div className="flex items-center gap-1.5 font-bold mb-1.5" style={{ color: themeColor }}>
                <Activity className="w-4 h-4" />
                <span>[⚡ คำแนะนำระบบ Antigravity]</span>
              </div>
              <p className="text-[var(--color-foreground)] leading-relaxed">
                {actionSuggestion}
              </p>
            </div>

            {/* Quick Action Button */}
            <div className="mt-5 pt-3 border-t border-[var(--color-border)]">
              <Button
                size="lg"
                onClick={scrollToApprovalQueue}
                className="w-full h-12 flex items-center justify-center gap-2 text-sm font-bold touch-target-safe shadow-lg"
              >
                <span>ไปที่ Approval Queue</span>
                <ArrowUp className="w-4 h-4" />
              </Button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}
