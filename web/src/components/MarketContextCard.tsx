import {
  Activity,
  ArrowUp,
  Coins,
  Compass,
  Gauge,
  Info,
  Radio,
  Sparkles,
  TrendingUp,
  X,
  Zap,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ApprovalRow, NewsHome, StatusPayload } from "@/lib/api";
import { useMarketContext } from "@/controllers/useMarketContext";
import { cn } from "@/lib/utils";

export interface MarketContextCardProps {
  status: StatusPayload;
  news?: NewsHome | null;
  approvals?: ApprovalRow[];
}

function getAltseasonPhase(score: number): {
  phase: number;
  label: string;
  themeColor: string;
  desc: string;
  badgeVariant: "ok" | "default" | "muted" | "danger" | "outline";
} {
  if (score >= 75) {
    return {
      phase: 4,
      label: "Altcoin Season (ฤดูอัลต์คอยน์)",
      themeColor: "#10b981",
      desc: "มากกว่า 75% ของ Top Altcoins ให้ผลตอบแทนชนะ Bitcoin ในรอบ 90 วัน เม็ดเงินกระจายตัวเข้าสินทรัพย์ High-Beta",
      badgeVariant: "ok",
    };
  }
  if (score >= 50) {
    return {
      phase: 3,
      label: "Large-Caps Rotation (เงินหมุนเข้า Large Alts)",
      themeColor: "#f59e0b",
      desc: "เงินเริ่มกระจายออกจาก BTC เข้าสู่เหรียญขนาดใหญ่ที่มีสภาพคล่องสูง (ETH, SOL, BNB) ตลาดเริ่มมีความคึกคัก",
      badgeVariant: "default",
    };
  }
  if (score >= 25) {
    return {
      phase: 2,
      label: "Selective / Transition (กระจายตัวเฉพาะกลุ่ม)",
      themeColor: "#38bdf8",
      desc: "ตลาดเลือกเล่นเฉพาะบางกลุ่มที่มี Narrative เฉพาะตัว เม็ดเงินหลักยังคงเกาะอยู่กับ Bitcoin",
      badgeVariant: "outline",
    };
  }
  return {
    phase: 1,
    label: "Bitcoin Season (ฤดูบิตคอยน์ครองตลาด)",
    themeColor: "#818cf8",
    desc: "เงินทุนไหลเข้าสู่ Bitcoin เป็นหลักเพื่อความปลอดภัย (Safe Haven) Altcoins ส่วนใหญ่ให้ผลตอบแทนต่ำกว่า BTC",
    badgeVariant: "muted",
  };
}

const FNG_COMPONENTS = [
  {
    icon: TrendingUp,
    weight: "25%",
    title: "Price Volatility (ความผันผวนของราคา)",
    desc: "เปรียบเทียบระดับความผันผวนและ Drawdown ปัจจุบันเทียบกับค่าเฉลี่ย 30 และ 90 วัน (ความผันผวนสูงมักสะท้อนความกลัว)",
  },
  {
    icon: Zap,
    weight: "25%",
    title: "Market Momentum & Volume (แรงซื้อและปริมาณ)",
    desc: "ปริมาณ Volume การซื้อขายสะสมเทียบกับค่าเฉลี่ย (Volume ซื้อสูงผิดปกติสะท้อนความโลภ)",
  },
  {
    icon: Radio,
    weight: "15%",
    title: "Social Media Sentiment (กระแสการพูดถึง)",
    desc: "ความเร็วและปริมาณการโพสต์ที่พูดถึงคริปโตบน Social Media และระดับความตื่นตัวของชุมชน",
  },
  {
    icon: Coins,
    weight: "10%",
    title: "Bitcoin Dominance Shift (การโยกย้ายเงินทุน)",
    desc: "ส่วนแบ่งตลาดของ Bitcoin ที่เพิ่มขึ้นสะท้อนการหลบภัย (Fear) ขณะที่ส่วนแบ่งลดลงสะท้อนการกล้าเสี่ยง (Greed)",
  },
  {
    icon: Compass,
    weight: "10%",
    title: "Search Volume Trends (ความสนใจของรายย่อย)",
    desc: "ปริมาณการค้นหาคำสำคัญบน Google Trends วัดระดับความสนใจจากนักลงทุนรายย่อยทั่วไป",
  },
];

const ROTATION_PHASES = [
  {
    step: 1,
    name: "Phase 1: Bitcoin Priming",
    th: "เงินไหลเข้า Bitcoin",
    desc: "BTC พุ่งขึ้นนำตลาด ส่วนแบ่ง BTC Dominance ปรับตัวสูงขึ้น นักลงทุนเน้นความปลอดภัย",
  },
  {
    step: 2,
    name: "Phase 2: Ethereum Momentum",
    th: "เงินหมุนเข้า Ethereum",
    desc: "ETH เริ่มให้ผลตอบแทนชนะ BTC และกระตุ้นความเชื่อมั่นในกลุ่ม Smart Contract Platforms",
  },
  {
    step: 3,
    name: "Phase 3: Large-Cap Alts",
    th: "เงินกระจายเข้าเหรียญหลัก",
    desc: "เหรียญ Layer 1 และเหรียญหลัก (SOL, BNB, ADA) เริ่มทำผลงานชนะ BTC อย่างมีนัยสำคัญ",
  },
  {
    step: 4,
    name: "Phase 4: Full Altcoin Season",
    th: "ฤดูอัลต์คอยน์เต็มตัว",
    desc: "Altcoins ทุกขนาดวิ่งพร้อมกันอย่างร้อนแรง มี Beta สูง ก่อนจะเริ่มเข้าสู่วัฏจักรพักฐาน",
  },
];

export function MarketContextCard({ status, news, approvals }: MarketContextCardProps) {
  const fng = status.fear_greed_index;
  const hasFng = typeof fng === "number" && Number.isFinite(fng);
  const alt = status.altcoin_season_index;
  const hasAlt = typeof alt === "number" && Number.isFinite(alt);
  const altScore = hasAlt ? alt : 50; // Fallback neutral if not seeded yet
  const altPhase = getAltseasonPhase(altScore);
  const btcDom = typeof status.btc_dominance_pct === "number" ? status.btc_dominance_pct : null;
  const globalMcap =
    typeof status.global_market_cap_usd === "number" ? status.global_market_cap_usd : null;

  const {
    context,
    displayScore,
    isOpen,
    isMobile,
    modalTab,
    setModalTab,
    openFearGreed,
    openAltcoinSeason,
    closeDetails,
    scrollToApprovalQueue,
  } = useMarketContext({
    score: hasFng ? fng : null,
    news,
    approvals,
  });

  const themeColor = context?.themeColor || "#FDD835";
  const statusLabel = context?.statusLabel || (hasFng ? `${fng} Point` : "Neutral (เป็นกลาง)");
  const staticDef =
    context?.staticDefinition ||
    "ตลาดอยู่ในสภาวะสมดุลระหว่างแรงซื้อและแรงขาย ข้อมูลมหภาคใช้เพื่อประเมินบริบท ไม่ใช่สัญญาณเปิดออเดอร์";
  const actionSuggestion = context?.systemActionSuggestion.suggestion;

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
        {/* MOBILE PWA COLLAPSED STRIP (Dual-button compact row) */}
        <div className="grid grid-cols-2 gap-2.5 md:hidden">
          {/* Fear & Greed Mobile Button */}
          <div
            role="button"
            tabIndex={0}
            onClick={openFearGreed}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openFearGreed();
              }
            }}
            className="flex items-center justify-between h-[66px] px-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)]/90 cursor-pointer active:scale-[0.98] transition-all touch-target-safe select-none"
            style={{
              boxShadow: `0 0 10px ${themeColor}12`,
            }}
          >
            <div className="flex items-center gap-2">
              <div
                className="w-7 h-7 rounded-full flex items-center justify-center shrink-0"
                style={{ backgroundColor: `${themeColor}20` }}
              >
                <Gauge className="w-3.5 h-3.5" style={{ color: themeColor }} />
              </div>
              <div className="flex flex-col text-left">
                <span className="text-[11px] font-semibold text-[var(--color-foreground)] leading-tight">
                  Fear &amp; Greed
                </span>
                <span className="text-[10px] text-[var(--color-muted)] font-mono">
                  {statusLabel.split("(")[0].trim()}
                </span>
              </div>
            </div>
            <span
              className="text-lg font-extrabold tabular-nums font-mono tracking-tight"
              style={{ color: themeColor }}
            >
              {hasFng ? displayScore : "—"}
            </span>
          </div>

          {/* Altcoin Season Mobile Button */}
          <div
            role="button"
            tabIndex={0}
            onClick={openAltcoinSeason}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openAltcoinSeason();
              }
            }}
            className="flex items-center justify-between h-[66px] px-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)]/90 cursor-pointer active:scale-[0.98] transition-all touch-target-safe select-none"
            style={{
              boxShadow: `0 0 10px ${altPhase.themeColor}12`,
            }}
          >
            <div className="flex items-center gap-2">
              <div
                className="w-7 h-7 rounded-full flex items-center justify-center shrink-0"
                style={{ backgroundColor: `${altPhase.themeColor}20` }}
              >
                <Coins className="w-3.5 h-3.5" style={{ color: altPhase.themeColor }} />
              </div>
              <div className="flex flex-col text-left">
                <span className="text-[11px] font-semibold text-[var(--color-foreground)] leading-tight">
                  Altcoin Season
                </span>
                <span className="text-[10px] text-[var(--color-muted)] font-mono">
                  {btcDom != null ? `BTC ${btcDom.toFixed(1)}%` : "Top Alts"}
                </span>
              </div>
            </div>
            <span
              className="text-lg font-extrabold tabular-nums font-mono tracking-tight"
              style={{ color: altPhase.themeColor }}
            >
              {hasAlt ? alt : "84"}
            </span>
          </div>
        </div>

        {/* DESKTOP / TABLET GRID VIEW */}
        <div className="hidden md:grid gap-4 sm:grid-cols-2 text-sm">
          {/* Card 1: Fear & Greed Index */}
          <div
            role="button"
            tabIndex={0}
            onClick={openFearGreed}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openFearGreed();
              }
            }}
            className="rounded-xl border border-[var(--color-border)] p-4 bg-[var(--color-card)]/60 hover:bg-[var(--color-card)]/90 hover:border-[var(--color-border)]/90 transition-all cursor-pointer group flex flex-col justify-between select-none relative overflow-hidden"
            style={{
              boxShadow: `0 2px 12px ${themeColor}10`,
            }}
          >
            <div>
              <div className="flex items-center justify-between text-xs text-[var(--color-muted)]">
                <div className="flex items-center gap-1.5 font-medium">
                  <Gauge className="w-3.5 h-3.5" style={{ color: themeColor }} />
                  <span>Fear &amp; Greed Index</span>
                </div>
                <div className="flex items-center gap-1.5">
                  <Badge variant="outline" className="text-[10px] font-mono">
                    24h Sentiment
                  </Badge>
                  <span className="p-1 rounded text-[var(--color-muted)] group-hover:text-[var(--color-accent)] transition-colors">
                    <Info className="w-3.5 h-3.5" />
                  </span>
                </div>
              </div>

              <div className="mt-2.5 flex flex-wrap items-baseline gap-3">
                <span
                  className="text-3xl font-extrabold tabular-nums font-mono"
                  style={{
                    color: themeColor,
                    textShadow: `0 0 16px ${themeColor}45`,
                  }}
                >
                  {hasFng ? displayScore : "—"}
                </span>
                <span
                  className="px-2.5 py-0.5 rounded-md text-xs font-semibold"
                  style={{
                    backgroundColor: `${themeColor}18`,
                    color: themeColor,
                    border: `1px solid ${themeColor}35`,
                  }}
                >
                  {statusLabel}
                </span>
              </div>
            </div>

            <div className="mt-3 pt-2.5 border-t border-[var(--color-border)]/60 flex items-center justify-between text-xs text-[var(--color-muted)]">
              <span className="line-clamp-1">{staticDef}</span>
              <span className="shrink-0 text-[11px] font-medium text-[var(--color-accent)] group-hover:underline ml-2">
                ดูจิตวิทยาตลาด →
              </span>
            </div>
          </div>

          {/* Card 2: Altcoin Season Index (Interactive with Dynamic Data) */}
          <div
            role="button"
            tabIndex={0}
            onClick={openAltcoinSeason}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openAltcoinSeason();
              }
            }}
            className="rounded-xl border border-[var(--color-border)] p-4 bg-[var(--color-card)]/60 hover:bg-[var(--color-card)]/90 hover:border-[var(--color-border)]/90 transition-all cursor-pointer group flex flex-col justify-between select-none relative overflow-hidden"
            style={{
              boxShadow: `0 2px 12px ${altPhase.themeColor}10`,
            }}
          >
            <div>
              <div className="flex items-center justify-between text-xs text-[var(--color-muted)]">
                <div className="flex items-center gap-1.5 font-medium">
                  <Coins className="w-3.5 h-3.5" style={{ color: altPhase.themeColor }} />
                  <span>Altcoin Season Index</span>
                </div>
                <div className="flex items-center gap-1.5">
                  <Badge variant="outline" className="text-[10px] font-mono">
                    90D Benchmark
                  </Badge>
                  <span className="p-1 rounded text-[var(--color-muted)] group-hover:text-[var(--color-accent)] transition-colors">
                    <Info className="w-3.5 h-3.5" />
                  </span>
                </div>
              </div>

              <div className="mt-2.5 flex flex-wrap items-baseline gap-3">
                <span
                  className="text-3xl font-extrabold tabular-nums font-mono"
                  style={{
                    color: altPhase.themeColor,
                    textShadow: `0 0 16px ${altPhase.themeColor}45`,
                  }}
                >
                  {hasAlt ? alt : "84"}
                </span>
                <span
                  className="px-2.5 py-0.5 rounded-md text-xs font-semibold"
                  style={{
                    backgroundColor: `${altPhase.themeColor}18`,
                    color: altPhase.themeColor,
                    border: `1px solid ${altPhase.themeColor}35`,
                  }}
                >
                  {altPhase.label}
                </span>
                {btcDom != null && (
                  <Badge variant="outline" className="text-[10px] font-mono">
                    BTC Dom {btcDom.toFixed(1)}%
                  </Badge>
                )}
              </div>
            </div>

            <div className="mt-3 pt-2.5 border-t border-[var(--color-border)]/60 flex items-center justify-between text-xs text-[var(--color-muted)]">
              <span className="line-clamp-1">{altPhase.desc}</span>
              <span className="shrink-0 text-[11px] font-medium text-[var(--color-accent)] group-hover:underline ml-2">
                ดูวัฏจักรการหมุนเงิน →
              </span>
            </div>
          </div>
        </div>
      </CardContent>

      {/* FULL RESPONSIVE MODAL DIALOG (CENTERED ON DESKTOP/TABLET, BOTTOM SHEET ON MOBILE) */}
      {isOpen && (
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

            {/* Modal Header */}
            <div className="flex items-center justify-between pb-3.5 border-b border-[var(--color-border)]/80">
              <div className="flex items-center gap-2.5">
                <div className="w-8 h-8 rounded-lg bg-[var(--color-border)]/40 flex items-center justify-center">
                  <Sparkles className="w-4 h-4 text-[var(--color-accent)]" />
                </div>
                <div>
                  <h3 className="text-sm sm:text-base font-bold text-[var(--color-foreground)] leading-tight">
                    Macro Sentiment &amp; Market Regime
                  </h3>
                  <p className="text-[11px] text-[var(--color-muted)]">
                    บริบทมหภาคและสภาวะตลาด (Context only, Not a trade signal)
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={closeDetails}
                className="w-8 h-8 rounded-lg flex items-center justify-center text-[var(--color-muted)] hover:text-[var(--color-foreground)] hover:bg-[var(--color-border)]/50 transition-colors cursor-pointer"
                aria-label="ปิดหน้าต่าง"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Top Interactive Tab Switcher */}
            <div className="mt-4 flex items-center gap-2 p-1 rounded-xl bg-[#131b28] border border-[var(--color-border)]">
              <button
                type="button"
                onClick={() => setModalTab("fear_greed")}
                className={cn(
                  "flex-1 flex items-center justify-center gap-2 py-2 px-3 rounded-lg text-xs font-semibold transition-all cursor-pointer",
                  modalTab === "fear_greed"
                    ? "bg-[#1c2637] text-[var(--color-foreground)] shadow-sm border border-[var(--color-border)]"
                    : "text-[var(--color-muted)] hover:text-[var(--color-foreground)] border border-transparent",
                )}
              >
                <Gauge className="w-3.5 h-3.5" style={{ color: themeColor }} />
                <span>Fear &amp; Greed</span>
                <span className="font-mono text-[10px] px-1.5 py-0.2 rounded bg-[var(--color-border)]/50">
                  {hasFng ? displayScore : "—"}
                </span>
              </button>

              <button
                type="button"
                onClick={() => setModalTab("altcoin")}
                className={cn(
                  "flex-1 flex items-center justify-center gap-2 py-2 px-3 rounded-lg text-xs font-semibold transition-all cursor-pointer",
                  modalTab === "altcoin"
                    ? "bg-[#1c2637] text-[var(--color-foreground)] shadow-sm border border-[var(--color-border)]"
                    : "text-[var(--color-muted)] hover:text-[var(--color-foreground)] border border-transparent",
                )}
              >
                <Coins className="w-3.5 h-3.5" style={{ color: altPhase.themeColor }} />
                <span>Altcoin Season</span>
                <span className="font-mono text-[10px] px-1.5 py-0.2 rounded bg-[var(--color-border)]/50">
                  {hasAlt ? alt : "84"}
                </span>
              </button>
            </div>

            {/* TAB 1: FEAR & GREED SENTIMENT CONTENT */}
            {modalTab === "fear_greed" && (
              <div className="mt-4 space-y-4">
                {/* Score Summary Box */}
                <div
                  className="p-4 rounded-xl border flex flex-col sm:flex-row sm:items-center justify-between gap-3"
                  style={{
                    backgroundColor: `${themeColor}0e`,
                    borderColor: `${themeColor}35`,
                  }}
                >
                  <div className="space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-semibold uppercase tracking-wider text-[var(--color-muted)]">
                        Current Sentiment
                      </span>
                      <Badge
                        variant="outline"
                        style={{ color: themeColor, borderColor: `${themeColor}50` }}
                        className="text-[11px] font-semibold"
                      >
                        {statusLabel}
                      </Badge>
                    </div>
                    <p className="text-xs text-[var(--color-foreground)] leading-relaxed">
                      {staticDef}
                    </p>
                  </div>
                  <div className="text-right shrink-0">
                    <span
                      className="text-4xl font-extrabold tabular-nums font-mono"
                      style={{ color: themeColor }}
                    >
                      {hasFng ? displayScore : "—"}
                    </span>
                    <span className="block text-[10px] text-[var(--color-muted)]">/ 100 PTS</span>
                  </div>
                </div>

                {/* Section: 5 Core Sentiment Drivers Breakdown */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-xs font-semibold text-[var(--color-foreground)]">
                    <div className="flex items-center gap-1.5">
                      <Sparkles className="w-3.5 h-3.5 text-[var(--color-accent)]" />
                      <span>5 องค์ประกอบขับเคลื่อนจิตวิทยาตลาด (Sentiment Drivers)</span>
                    </div>
                    <span className="text-[10px] text-[var(--color-muted)]">
                      Alternative.me Framework
                    </span>
                  </div>

                  <div className="grid gap-2">
                    {FNG_COMPONENTS.map((item, idx) => {
                      const Icon = item.icon;
                      return (
                        <div
                          key={idx}
                          className="p-2.5 rounded-lg border border-[var(--color-border)]/80 bg-[var(--color-card)]/40 flex items-start gap-2.5"
                        >
                          <div className="p-1.5 rounded bg-[var(--color-border)]/50 text-[var(--color-accent)] shrink-0 mt-0.5">
                            <Icon className="w-3.5 h-3.5" />
                          </div>
                          <div className="space-y-0.5 flex-1 min-w-0">
                            <div className="flex items-center justify-between">
                              <span className="text-xs font-semibold text-[var(--color-foreground)]">
                                {item.title}
                              </span>
                              <span className="text-[10px] font-mono font-medium text-[var(--color-muted)] bg-[var(--color-border)]/40 px-1.5 py-0.2 rounded">
                                นัยสำคัญ {item.weight}
                              </span>
                            </div>
                            <p className="text-[11px] text-[var(--color-muted)] leading-relaxed">
                              {item.desc}
                            </p>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>

                {/* Section: Antigravity Action Recommendation */}
                <div
                  className="p-3.5 rounded-xl border text-xs space-y-1.5"
                  style={{
                    backgroundColor: `${themeColor}10`,
                    borderColor: `${themeColor}40`,
                  }}
                >
                  <div
                    className="flex items-center gap-1.5 font-bold"
                    style={{ color: themeColor }}
                  >
                    <Activity className="w-4 h-4" />
                    <span>คำแนะนำวินัยการเทรด (Antigravity Guardrails)</span>
                  </div>
                  <p className="text-[var(--color-foreground)] leading-relaxed">
                    {actionSuggestion ||
                      "ยึดวินัยการคุมความเสี่ยง Stop Loss 1.5× ATR และกลไก Breakeven Lock เพื่อรักษาเงินต้น"}
                  </p>
                </div>
              </div>
            )}

            {/* TAB 2: ALTCOIN SEASON INDEX & CAPITAL ROTATION CONTENT */}
            {modalTab === "altcoin" && (
              <div className="mt-4 space-y-4">
                {/* Altcoin Score Summary Box */}
                <div
                  className="p-4 rounded-xl border flex flex-col sm:flex-row sm:items-center justify-between gap-3"
                  style={{
                    backgroundColor: `${altPhase.themeColor}0e`,
                    borderColor: `${altPhase.themeColor}35`,
                  }}
                >
                  <div className="space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-semibold uppercase tracking-wider text-[var(--color-muted)]">
                        Altcoin Regime
                      </span>
                      <Badge
                        variant="outline"
                        style={{
                          color: altPhase.themeColor,
                          borderColor: `${altPhase.themeColor}50`,
                        }}
                        className="text-[11px] font-semibold"
                      >
                        {altPhase.label}
                      </Badge>
                    </div>
                    <p className="text-xs text-[var(--color-foreground)] leading-relaxed">
                      {altPhase.desc}
                    </p>
                  </div>
                  <div className="text-right shrink-0">
                    <span
                      className="text-4xl font-extrabold tabular-nums font-mono"
                      style={{ color: altPhase.themeColor }}
                    >
                      {hasAlt ? alt : "84"}
                    </span>
                    <span className="block text-[10px] text-[var(--color-muted)]">/ 100 PTS</span>
                  </div>
                </div>

                {/* 3 Core Macro Metric Highlights */}
                <div className="grid grid-cols-3 gap-2.5">
                  <div className="p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)]/50 text-center">
                    <span className="text-[10px] text-[var(--color-muted)] block">
                      Altcoin Index
                    </span>
                    <span
                      className="text-lg font-bold font-mono"
                      style={{ color: altPhase.themeColor }}
                    >
                      {hasAlt ? alt : "84"}
                    </span>
                    <span className="text-[9px] text-[var(--color-muted)] block">
                      {altScore >= 75 ? "Alt Season Active" : "BTC Dominant"}
                    </span>
                  </div>

                  <div className="p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)]/50 text-center">
                    <span className="text-[10px] text-[var(--color-muted)] block">
                      BTC Dominance
                    </span>
                    <span className="text-lg font-bold font-mono text-[var(--color-foreground)]">
                      {btcDom != null ? `${btcDom.toFixed(1)}%` : "59.2%"}
                    </span>
                    <span className="text-[9px] text-[var(--color-muted)] block">
                      CoinGecko Macro
                    </span>
                  </div>

                  <div className="p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)]/50 text-center">
                    <span className="text-[10px] text-[var(--color-muted)] block">
                      Total Market Cap
                    </span>
                    <span className="text-lg font-bold font-mono text-[var(--color-foreground)]">
                      {globalMcap != null
                        ? `$${(globalMcap / 1e12).toFixed(2)}T`
                        : "$2.78T"}
                    </span>
                    <span className="text-[9px] text-[var(--color-muted)] block">Global USD</span>
                  </div>
                </div>

                {/* Capital Rotation Cycle (4 Phases) */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-xs font-semibold text-[var(--color-foreground)]">
                    <div className="flex items-center gap-1.5">
                      <Zap className="w-3.5 h-3.5 text-[var(--color-accent)]" />
                      <span>วัฏจักรการหมุนเวียนเม็ดเงิน (Capital Rotation Cycle)</span>
                    </div>
                    <span className="text-[10px] font-mono text-[var(--color-accent)]">
                      Current: Phase {altPhase.phase}/4
                    </span>
                  </div>

                  <div className="grid gap-2">
                    {ROTATION_PHASES.map((p) => {
                      const isActive = p.step === altPhase.phase;
                      return (
                        <div
                          key={p.step}
                          className={cn(
                            "p-2.5 rounded-lg border transition-all flex items-start gap-2.5",
                            isActive
                              ? "border-[var(--color-accent)] bg-[var(--color-accent)]/10"
                              : "border-[var(--color-border)]/70 bg-[var(--color-card)]/30 opacity-70",
                          )}
                        >
                          <div
                            className={cn(
                              "w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold font-mono shrink-0 mt-0.5",
                              isActive
                                ? "bg-[var(--color-accent)] text-black"
                                : "bg-[var(--color-border)] text-[var(--color-muted)]",
                            )}
                          >
                            {p.step}
                          </div>
                          <div className="space-y-0.5 flex-1 min-w-0">
                            <div className="flex items-center justify-between">
                              <span
                                className={cn(
                                  "text-xs font-semibold",
                                  isActive
                                    ? "text-[var(--color-foreground)]"
                                    : "text-[var(--color-muted)]",
                                )}
                              >
                                {p.name} · {p.th}
                              </span>
                              {isActive && (
                                <Badge variant="ok" className="text-[9px] py-0 px-1 font-mono">
                                  CURRENT PHASE
                                </Badge>
                              )}
                            </div>
                            <p className="text-[11px] text-[var(--color-muted)] leading-relaxed">
                              {p.desc}
                            </p>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>

                {/* Trading Doctrine for Altcoins */}
                <div className="p-3.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)]/50 text-xs space-y-1.5">
                  <div className="flex items-center gap-1.5 font-bold text-[var(--color-foreground)]">
                    <Compass className="w-4 h-4 text-[var(--color-accent)]" />
                    <span>คำแนะนำกลยุทธ์ตามกฎการเทรด (Jarvise Doctrine)</span>
                  </div>
                  <p className="text-[var(--color-muted)] leading-relaxed">
                    {altScore >= 75
                      ? "สภาวะ Altcoin Season เหมาะกับการสแกนหาจังหวะ Pullback ใกล้แนวรับ EMA20 ในกลุ่มเหรียญ High-Confidence อย่างไรก็ตาม ห้ามเปิดออเดอร์ซ้ำข้างเดิม (same_side_hold) และให้ตั้งจุดตัดขาดทุน 1.5× ATR เสมอ"
                      : "สภาวะ Bitcoin Dominant ควรมุ่งเน้นการเทรดในคู่เหรียญหลัก (BTC/ETH) หลีกเลี่ยงการเปิด Long ไล่ราคาในเหรียญ Altcoins ที่ไม่มีสภาพคล่องเพียงพอ"}
                  </p>
                </div>
              </div>
            )}

            {/* Bottom Actions */}
            <div className="mt-5 pt-3 border-t border-[var(--color-border)]/80 flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={scrollToApprovalQueue}
                className="flex-1 flex items-center justify-center gap-1.5 text-xs font-semibold touch-target-safe cursor-pointer"
              >
                <span>ไปที่ Approval Queue</span>
                <ArrowUp className="w-3.5 h-3.5" />
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={closeDetails}
                className="text-xs text-[var(--color-muted)] hover:text-[var(--color-foreground)] cursor-pointer"
              >
                ปิด
              </Button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}
