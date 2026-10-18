import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApprovalQueue } from "@/components/ApprovalQueue";
import { InfoTip } from "@/components/InfoTip";
import { KpiStrip } from "@/components/KpiStrip";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { MarketContextCard } from "@/components/MarketContextCard";
import { StateChip } from "@/components/StateChip";
import type { DashboardPayload, NewsHome, PaperAutoStatus } from "@/lib/api";
import { resolveReasonLabel } from "@/lib/copy";
import { autoDecideChip, paperTimeframeLabel, ruleText, type DashboardRules } from "@/lib/dashboardRules";
import { evaluateCloseRecommendation } from "@/lib/positionAdvice";
import { cn, formatNum, formatPrice, relativeAge } from "@/lib/utils";
import { showQueueJump } from "@/lib/ownerReview";


type AutoEntry = {
  id: string;
  symbol?: string;
  reason?: string;
  kind: "approved" | "deferred" | "rejected" | "failed";
};

function collectAutoEntries(auto: PaperAutoStatus | null | undefined): AutoEntry[] {
  if (!auto) return [];
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

function ActivePositionsCard({
  positions,
  autoDecideEnabled,
  rules,
}: {
  positions: Array<Record<string, unknown>>;
  autoDecideEnabled: boolean;
  rules?: DashboardRules | null;
}) {
  if (!positions || positions.length === 0) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base font-semibold">Active Paper Positions</CardTitle>
            <Badge variant="muted">0 Open</Badge>
          </div>
          <CardDescription>Simulated portfolio trades currently in play</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-col items-center justify-center py-6 text-center text-sm text-[var(--color-muted)]">
            <p>
              {autoDecideEnabled
                ? "ยังไม่มีไม้เปิด"
                : "ยังไม่มีไม้เปิด · auto-decide ปิดอยู่ ไม้ใหม่เปิดเมื่อคุณ Approve เท่านั้น"}
            </p>
          </div>
        </CardContent>
      </Card>
    );
  }

  const totalUnrealized = positions.reduce(
    (sum, p) => sum + (Number(p.unrealized_pnl) || 0),
    0,
  );

  return (
    <Card className="border-[var(--color-border)]">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-semibold">Active Paper Positions</CardTitle>
            <Badge variant="ok">{positions.length} Active</Badge>
          </div>
          <div className="flex items-center gap-3">
            <div className="text-xs text-[var(--color-muted)]">
              Total uPnL:{" "}
              <span
                className={cn(
                  "font-mono font-medium",
                  totalUnrealized >= 0 ? "text-[var(--color-ok)]" : "text-[var(--color-danger)]",
                )}
              >
                {totalUnrealized >= 0 ? "+" : ""}
                {formatNum(totalUnrealized)} USD
              </span>
            </div>
            <Link
              to="/paper"
              className="text-xs text-[var(--color-accent)] hover:underline font-medium"
            >
              Full Ledger &amp; Charts →
            </Link>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--color-muted)]">
          <span>Stop {ruleText(rules?.atr_stop_mult, "× ATR")}</span>
          <span>BE {ruleText(rules?.breakeven_r, "R")}</span>
          <span>Trail {ruleText(rules?.trail_distance_r, "R")}</span>
          <InfoTip
            label="จุดคุ้มครองของโพสิชัน"
            text={`Live simulated positions protected by ${ruleText(rules?.atr_stop_mult, "× ATR")} stop, ${ruleText(rules?.breakeven_r, "R")} Breakeven lock, and ${ruleText(rules?.trail_distance_r, "R")} Trail`}
          />
        </div>
      </CardHeader>
      <CardContent>
        <ul className="space-y-3 sm:hidden">
          {positions.map((p, i) => {
            const side = String(p.side || "").toLowerCase();
            const uPnl = Number(p.unrealized_pnl) || 0;
            const closeAdvice = evaluateCloseRecommendation(p);
            const rMultiple = closeAdvice.rMultiple;
            return (
              <li key={i} className="rounded-lg border border-[var(--color-border)] p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{String(p.symbol || "")}</span>
                  <span className="rounded px-1.5 py-0.5 text-xs font-mono uppercase">{side}</span>
                </div>
                <dl className="mt-2 grid grid-cols-2 gap-2 text-xs">
                  <div>
                    <dt className="text-[var(--color-muted)]">uPnL</dt>
                    <dd className="font-mono tabular-nums">
                      {uPnl >= 0 ? "+" : ""}
                      {formatNum(uPnl)}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-[var(--color-muted)]">R</dt>
                    <dd className="font-mono tabular-nums">
                      {rMultiple != null ? `${rMultiple >= 0 ? "+" : ""}${formatNum(rMultiple)}R` : "—"}
                    </dd>
                  </div>
                </dl>
                <div className="mt-2 flex items-center justify-between gap-2">
                  <span className="text-xs">{closeAdvice.label}</span>
                  <InfoTip label="คำแนะนำปิดไม้" text={closeAdvice.tooltip} />
                </div>
              </li>
            );
          })}
        </ul>
        <div className="hidden overflow-x-auto sm:block -mx-1 sm:mx-0">
          <table className="w-full min-w-[560px] text-left text-sm">
            <thead className="text-[var(--color-muted)] border-b border-[var(--color-border)] text-xs">
              <tr>
                <th className="pb-2 font-medium">Symbol</th>
                <th className="pb-2 font-medium">Side</th>
                <th className="pb-2 font-medium">Entry</th>
                <th className="pb-2 font-medium">Stop / Protection</th>
                <th className="pb-2 font-medium">uPnL</th>
                <th className="pb-2 font-medium text-right">R-Multiple</th>
                <th className="pb-2 font-medium text-right">Close Advice</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--color-border)]/50">
              {positions.map((p, i) => {
                const side = String(p.side || "").toLowerCase();
                const uPnl = Number(p.unrealized_pnl) || 0;
                const stopSource = String(p.stop_source || "");
                const entry = Number(p.entry_price);
                const stop = p.stop_price != null ? Number(p.stop_price) : null;
                const closeAdvice = evaluateCloseRecommendation(p);
                const rMultiple = closeAdvice.rMultiple;

                return (
                  <tr key={i} className="hover:bg-[var(--color-card)]/50 transition-colors">
                    <td className="py-2.5 font-medium">{String(p.symbol || "")}</td>
                    <td className="py-2.5">
                      <span
                        className={cn(
                          "inline-block rounded px-1.5 py-0.5 text-xs font-mono uppercase font-semibold",
                          side === "long"
                            ? "bg-[var(--color-ok)]/15 text-[var(--color-ok)]"
                            : "bg-rose-500/15 text-rose-400",
                        )}
                      >
                        {side}
                      </span>
                    </td>
                    <td className="py-2.5 font-mono text-xs tabular-nums">{formatPrice(entry)}</td>
                    <td className="py-2.5 font-mono text-xs">
                      {stop != null ? (
                        <div className="flex items-center gap-1.5">
                          <span>{formatPrice(stop)}</span>
                          {stopSource === "breakeven" ? (
                            <Badge variant="ok" className="text-[10px] px-1 py-0">
                              BE +{ruleText(rules?.breakeven_buffer_bps)}bps
                            </Badge>
                          ) : stopSource === "trail" ? (
                            <Badge variant="default" className="text-[10px] px-1 py-0">
                              Trail {ruleText(rules?.trail_distance_r, "R")}
                            </Badge>
                          ) : (
                            <span className="text-[10px] text-[var(--color-muted)]">Initial</span>
                          )}
                        </div>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="py-2.5 font-mono text-xs tabular-nums">
                      <span
                        className={
                          uPnl >= 0 ? "text-[var(--color-ok)]" : "text-[var(--color-danger)]"
                        }
                      >
                        {uPnl >= 0 ? "+" : ""}
                        {formatNum(uPnl)}
                      </span>
                    </td>
                    <td className="py-2.5 font-mono text-xs tabular-nums text-right">
                      {rMultiple != null ? (
                        <span
                          className={
                            rMultiple >= 0
                              ? "text-[var(--color-ok)] font-medium"
                              : "text-[var(--color-danger)]"
                          }
                        >
                          {rMultiple >= 0 ? "+" : ""}
                          {formatNum(rMultiple)}R
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="py-2.5 font-mono text-xs tabular-nums text-right">
                      <span className="inline-flex items-center justify-end gap-1">
                        <span
                          className={cn(
                            "inline-flex items-center gap-1.5 text-[11px] font-medium px-2 py-0.5 rounded border",
                            closeAdvice.btnClass,
                          )}
                        >
                          <span className={cn("w-2 h-2 rounded-full", closeAdvice.dotClass)} />
                          {closeAdvice.label}
                        </span>
                        <InfoTip label="คำแนะนำปิดไม้" text={closeAdvice.tooltip} />
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

function NewsCard({ news }: { news: NewsHome | null | undefined }) {
  const lines = news?.lines?.length ? news.lines : ["ยังไม่มีรายงานข่าวรอบล่าสุด"];
  const when = news?.generated_at_ms != null ? relativeAge(news.generated_at_ms) : null;
  const disclaimer = lines[0] === "บริบทข่าว ไม่ใช่สัญญาณ" ? lines[0] : null;
  const contentLines = disclaimer ? lines.slice(1) : lines;

  return (
    <Card className="border-[var(--color-border)]">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-semibold">Intelligence &amp; News Digest</CardTitle>
            <Badge variant="muted">บริบทข่าว ไม่ใช่สัญญาณ</Badge>
          </div>
          <span className="text-xs text-[var(--color-muted)]">
            {news?.window_years != null ? `ย้อน ${news.window_years} ปี` : ""}
            {when ? ` · ${when}` : ""}
          </span>
        </div>
        <CardDescription>
          สรุปประเด็นข่าวเฝ้าระวังจาก OpenClaw พร้อมบริบทผลกระทบต่อตลาด (Context only, Not a trade signal)
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {contentLines.length === 0 ? (
          <p className="text-sm text-[var(--color-muted)]">ยังไม่มีรายงานข่าวรอบล่าสุด</p>
        ) : (
          <div className="space-y-2.5">
            {contentLines.map((line, idx) => {
              const colonIdx = line.indexOf(":");
              const arrowIdx = line.indexOf("→");
              if (colonIdx > 0 && arrowIdx > colonIdx) {
                const theme = line.slice(0, colonIdx).trim();
                const headline = line.slice(colonIdx + 1, arrowIdx).trim();
                const rest = line.slice(arrowIdx + 1).trim();
                const statMatch = rest.match(/^(.*?)\s*(\([^)]*(?:BTC|ETH|วันถัดไป)[^)]*\))$/);
                const impactText = statMatch ? statMatch[1] : rest;
                const statText = statMatch ? statMatch[2] : null;
                return (
                  <div
                    key={idx}
                    className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/60 p-2.5 space-y-1.5"
                  >
                    <div className="flex flex-wrap items-baseline gap-1.5">
                      <span className="font-semibold text-xs text-[var(--color-accent)] uppercase tracking-wide">
                        {theme}:
                      </span>
                      <span className="text-xs font-medium text-[var(--color-foreground)]">
                        {headline}
                      </span>
                    </div>
                    <div className="text-xs text-[var(--color-muted)] flex flex-wrap items-center justify-between gap-2 pl-2 border-l-2 border-[var(--color-border)]">
                      <div className="flex items-start gap-1.5 flex-1 min-w-[200px]">
                        <span className="text-[var(--color-accent)] font-bold">→</span>
                        <span>{impactText}</span>
                      </div>
                      {statText ? (
                        <span className="shrink-0 text-[10px] font-mono text-[var(--color-muted)] bg-[var(--color-border)]/50 px-1.5 py-0.5 rounded">
                          {statText}
                        </span>
                      ) : null}
                    </div>
                  </div>
                );
              }
              return (
                <div key={idx} className="text-xs text-[var(--color-foreground)]/90 flex items-start gap-2">
                  <span className="text-[var(--color-muted)]">•</span>
                  <span>{line}</span>
                </div>
              );
            })}
          </div>
        )}
        {news?.digest_note ? (
          <p className="text-xs text-[var(--color-muted)] italic pt-1">{news.digest_note}</p>
        ) : null}
        {Object.values(news?.headline_sources ?? {}).includes("google_news") ? (
          <p className="text-xs text-[var(--color-muted)] pt-1">
            หัวข้อข่าวบางหมวดมาจาก Google News เพราะ GDELT จำกัดการเรียกข้อมูลในรอบนี้
          </p>
        ) : null}
        {news?.errors && news.errors.length > 0 ? (
          <p className="text-xs text-amber-400 pt-1">
            ดึงข่าวไม่สำเร็จ {news.errors.length} หมวด (
            {news.errors.map((e) => e.split(":")[0]).join(", ")}) — รายงานนี้อาจไม่ครบ
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function LastAutoDecideCard({
  auto,
  enabled,
  latched,
  paperTimeframe,
}: {
  auto: PaperAutoStatus | null | undefined;
  enabled: boolean | undefined;
  latched: boolean | undefined;
  paperTimeframe: string | null | undefined;
}) {
  const chip = autoDecideChip(enabled, latched);
  if (!auto || typeof auto !== "object") {
    return (
      <Card className="border-[var(--color-border)]">
        <CardHeader className="pb-3">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-semibold">Latest Auto-Decide Review</CardTitle>
            <StateChip tone={chip.tone} label={chip.label} />
          </div>
        </CardHeader>
      </Card>
    );
  }
  const raw = auto as PaperAutoStatus & { raw?: unknown };
  // Redis can hand back {raw: "..."} when the job wrote non-JSON; say so instead of "Processed".
  const unreadable = raw.raw !== undefined && raw.ok === undefined;
  const entries = unreadable ? [] : collectAutoEntries(auto);
  const filteredCount = Array.isArray(raw.filtered_out) ? raw.filtered_out.length : 0;
  const state: { label: string; variant: "ok" | "muted" | "danger" | "outline" } = unreadable
    ? { label: "อ่านผลไม่ได้", variant: "outline" }
    : raw.ok === false
      ? { label: "ล้มเหลว", variant: "danger" }
      : raw.halted
        ? { label: "ระบบสั่งหยุด", variant: "danger" }
        : raw.skipped
          ? { label: "ข้ามรอบ", variant: "muted" }
          : { label: "Processed", variant: "ok" };

  return (
    <Card className="border-[var(--color-border)]">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-semibold">Latest Auto-Decide Review</CardTitle>
            <StateChip tone={chip.tone} label={chip.label} />
            <Badge variant={state.variant}>{state.label}</Badge>
          </div>
          <span className="text-xs text-[var(--color-muted)]">
            {auto.at_ms != null ? relativeAge(auto.at_ms) : ""}
            {auto.model ? ` · ${auto.model}` : ""}
          </span>
        </div>
        <CardDescription>
          Claude (OpenRouter) second-layer review after {paperTimeframeLabel(paperTimeframe)} candidate enqueue.{" "}
          <Link to="/ops" className="text-[var(--color-accent)] underline-offset-2 hover:underline">
            View details on Ops →
          </Link>
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* EV gate numbers live on Paper only; here we just say why the run was skipped. */}
        {raw.skipped && raw.reason ? (
          <p className="text-xs text-[var(--color-muted)]">
            เหตุผล: <span className="text-[var(--color-foreground)]">{resolveReasonLabel(raw.reason)}</span>
            {raw.reason === "auto_ev_gate" ? (
              <>
                {" "}
                <Link to="/paper" className="text-[var(--color-accent)] underline-offset-2 hover:underline">
                  ดู EV gate บนหน้า Paper →
                </Link>
              </>
            ) : null}
          </p>
        ) : null}
        {raw.doctrine_unavailable ? (
          <p className="text-xs text-amber-400">กฎการเทรด (Qdrant) ไม่พร้อมในรอบนี้ — Claude รีวิวโดยไม่มีบริบทกฎ</p>
        ) : null}
        {filteredCount > 0 ? (
          <p className="text-xs text-[var(--color-muted)]">
            ไม่ส่งรีวิว {filteredCount} การ์ด (เช่น สัญญาณ FLAT ที่ไม่มีไม้ให้ปิด)
          </p>
        ) : null}
        {entries.length > 0 ? (
          <ul className="space-y-2 text-sm">
            {entries.slice(0, 4).map((e) => (
              <li key={`${e.kind}-${e.id}`} className="flex items-center justify-between gap-2 rounded border border-[var(--color-border)]/50 px-3 py-1.5 text-xs">
                <div className="flex items-center gap-2">
                  <Badge variant={kindVariant(e.kind)}>{e.kind}</Badge>
                  <span className="font-medium font-mono">{e.symbol || e.id}</span>
                </div>
                <span className="text-[var(--color-muted)]">{resolveReasonLabel(e.reason)}</span>
              </li>
            ))}
          </ul>
        ) : null}
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

  const m = data.metrics;
  const queueEmpty = (data.approvals || []).length === 0;
  const positions = (data.paper?.positions as Array<Record<string, unknown>>) || [];
  const openSymbols = new Set(positions.map((p) => String(p.symbol || "")));

  return (
    <div className="space-y-6 max-w-7xl mx-auto">
      <div>
        <h1 className="text-xl font-bold tracking-tight text-[var(--color-foreground)]">Command Control</h1>
        <p className="text-sm text-[var(--color-muted)]">
          Decide pending candidates, monitor active paper positions, and verify portfolio safety.
        </p>
      </div>

      {error ? (
        // Data below is from the last good refresh; say so instead of hiding the failure.
        <p className="rounded-md border border-amber-500/40 bg-amber-950/20 px-3 py-2 text-xs text-amber-300">
          อัปเดตล่าสุดไม่สำเร็จ ({error}) — ข้อมูลด้านล่างอาจไม่ใช่ล่าสุด
        </p>
      ) : null}

      <ApprovalQueue
        rows={data.approvals}
        killSwitch={data.status.kill_switch}
        openSymbols={openSymbols}
        onChanged={onRefresh}
        emptyExtra={
          queueEmpty ? (
            <LastAutoDecideCard
              auto={data.status.paper_auto}
              enabled={data.status.auto_decide_config?.enabled}
              latched={data.status.auto_ev_gate?.latched}
              paperTimeframe={data.status.rules?.paper_timeframe}
            />
          ) : null
        }
      />

      <ActivePositionsCard
        positions={positions}
        autoDecideEnabled={data.status.auto_decide_config?.enabled === true}
        rules={data.status.rules}
      />

      <QueueJump pendingCount={queueEmpty ? 0 : data.approvals.length} />

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
            value: positions.length,
            hint: "Paper only",
          },
        ]}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <MarketContextCard
          status={data.status}
          news={data.news}
        />
        <NewsCard news={data.news} />
      </div>
    </div>
  );
}

function QueueJump({ pendingCount }: { pendingCount: number }) {
  const [scrolledPast, setScrolledPast] = useState(false);

  useEffect(() => {
    const node = document.getElementById("approval-queue");
    if (!node || pendingCount <= 0) {
      setScrolledPast(false);
      return;
    }
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry) return;
      setScrolledPast(!entry.isIntersecting && entry.boundingClientRect.top < 0);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [pendingCount]);

  if (!showQueueJump(pendingCount, scrolledPast)) return null;
  return (
    <button
      type="button"
      className="fixed bottom-4 right-4 z-40 inline-flex h-11 items-center rounded-full border border-border bg-card px-4 text-sm font-medium text-(--color-foreground) shadow-lg sm:hidden"
      onClick={() => {
        document.getElementById("approval-queue")?.scrollIntoView({ behavior: "smooth", block: "start" });
      }}
    >
      ไปที่ Approval Queue ↑
    </button>
  );
}

