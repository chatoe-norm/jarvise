import { useEffect, useMemo, useState } from "react";
import { EquityChart, WinLossChart } from "@/components/Charts";
import { KpiStrip } from "@/components/KpiStrip";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { api, type LossLesson, type MetricsPayload, type PaperPayload } from "@/lib/api";
import { buildEquityPoints } from "@/lib/equity";
import { decisionSourceLabel, evGateOwnerCopy } from "@/lib/copy";
import { evaluateCloseRecommendation } from "@/lib/positionAdvice";
import { cn, formatDecisionStamp, formatNum, formatQty } from "@/lib/utils";

const PAGE_SIZES = [10, 15, 20, 50, 100] as const;


function evSign(ev: number | null | undefined): string {
  if (ev == null) return "ยังไม่มี EV";
  if (ev < 0) return "EV ติดลบ";
  if (ev > 0) return "EV เป็นบวก";
  return "EV เท่ากับศูนย์";
}

function worstSymbol(lesson: LossLesson): string {
  const rows = Object.entries(lesson.by_symbol);
  if (rows.length === 0) return "ยังไม่มีสัญลักษณ์ที่ปิดไม้";
  const [symbol, row] = rows.reduce((worst, next) =>
    next[1].pnl_usd < worst[1].pnl_usd ? next : worst,
  );
  return `${symbol} กินผลขาดทุนมากสุด ${formatNum(row.pnl_usd)} ดอลลาร์ จาก ${row.n} ไม้`;
}

function floorLine(lesson: LossLesson): string {
  const ev =
    lesson.auto_ev == null ? "ยังไม่มี" : lesson.auto_ev < 0 ? "ติดลบ" : "ไม่ติดลบ";
  if (lesson.floors_unchanged) {
    return `auto_* ปิดแล้ว ${lesson.auto_closed} ไม้ จากเกณฑ์ประมาณ 8 และ EV ของ auto_* ${ev} พื้นความมั่นใจ 0.55/0.70 จึงยังอยู่`;
  }
  return `auto_* ปิดแล้ว ${lesson.auto_closed} ไม้ และ EV ของ auto_* ${ev} หน้านี้ยังไม่สั่งให้เปลี่ยนสูตร`;
}

type ExitKind = "stop_loss" | "stop_profit" | "flat" | "manual" | "other";

/**
 * Backend exit_reason values: stop_hit (initial, breakeven and trailing stops alike),
 * close_long / close_short (FLAT signal) and close_manual (Close button).
 * A stop that closed in profit is the trailing/breakeven lock doing its job.
 */
export function exitKind(reason: string | undefined, pnlUsd: number): ExitKind {
  const r = String(reason || "").toLowerCase();
  if (r === "stop_hit") return pnlUsd > 0 ? "stop_profit" : "stop_loss";
  if (r === "close_manual") return "manual";
  if (r === "close_long" || r === "close_short") return "flat";
  return "other";
}

function exitReasonBadge(reason: string | undefined, pnlUsd: number) {
  if (!reason) return <span className="text-(--color-muted) text-xs font-mono">—</span>;
  switch (exitKind(reason, pnlUsd)) {
    case "stop_loss":
      return (
        <Badge variant="outline" className="border-rose-500/40 text-rose-400 text-[10px] font-mono">
          Stop Hit
        </Badge>
      );
    case "stop_profit":
      return (
        <Badge variant="outline" className="border-emerald-500/40 text-emerald-400 text-[10px] font-mono">
          Stop ล็อกกำไร
        </Badge>
      );
    case "flat":
      return (
        <Badge variant="outline" className="border-amber-500/40 text-amber-400 text-[10px] font-mono">
          FLAT Signal
        </Badge>
      );
    case "manual":
      return (
        <Badge variant="outline" className="border-sky-500/40 text-sky-300 text-[10px] font-mono">
          ปิดเอง
        </Badge>
      );
    default:
      return (
        <Badge variant="outline" className="border-border text-(--color-muted) text-[10px] font-mono">
          {reason}
        </Badge>
      );
  }
}

/** `auto` is the rollup of every auto_* source; with a single auto_* source it is a duplicate row. */
function visibleSourceKeys(src: Record<string, unknown>): string[] {
  const keys = Object.keys(src);
  const autoSources = keys.filter((k) => k.startsWith("auto_"));
  return autoSources.length <= 1 ? keys.filter((k) => k !== "auto") : keys;
}

export function PaperPage({ onPositionsChanged }: { onPositionsChanged?: () => void } = {}) {
  const [paper, setPaper] = useState<PaperPayload | null>(null);
  const [metrics, setMetrics] = useState<MetricsPayload | null>(null);
  // Action errors (close, persist) stay until the next action; poll errors are separate
  // so a background refresh can't wipe a failed close message.
  const [error, setError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [closing, setClosing] = useState<string | null>(null);
  const { confirm, dialog } = useConfirm();

  const sortedTrades = useMemo(() => {
    if (!metrics?.trades) return [];
    return [...metrics.trades].sort((a, b) => {
      const tsA = Number(a.close_ts) || 0;
      const tsB = Number(b.close_ts) || 0;
      return tsB - tsA;
    });
  }, [metrics?.trades]);

  const [filterSymbol, setFilterSymbol] = useState("");
  const [filterOutcome, setFilterOutcome] = useState<"ALL" | "WIN" | "LOSS" | "SCRATCH">("ALL");
  const [filterExitReason, setFilterExitReason] = useState<string>("ALL");
  const [tradePage, setTradePage] = useState(1);
  const [tradePageSize, setTradePageSize] = useState<number>(10);

  const filteredTrades = useMemo(() => {
    return sortedTrades.filter((t) => {
      if (filterSymbol) {
        const sym = String(t.symbol || "").toLowerCase();
        if (!sym.includes(filterSymbol.toLowerCase().trim())) return false;
      }
      if (filterOutcome !== "ALL") {
        const pnl = Number(t.pnl_usd) || 0;
        if (filterOutcome === "WIN" && pnl <= 0) return false;
        if (filterOutcome === "LOSS" && pnl >= 0) return false;
        if (filterOutcome === "SCRATCH" && pnl !== 0) return false;
      }
      if (filterExitReason !== "ALL") {
        const kind = exitKind(t.exit_reason, Number(t.pnl_usd) || 0);
        if (filterExitReason === "stop" && kind !== "stop_loss") return false;
        if (filterExitReason === "profit_lock" && kind !== "stop_profit") return false;
        if (filterExitReason === "flat" && kind !== "flat") return false;
        if (filterExitReason === "manual" && kind !== "manual") return false;
      }
      return true;
    });
  }, [sortedTrades, filterSymbol, filterOutcome, filterExitReason]);

  const totalTradesCount = filteredTrades.length;
  const tradePageCount = Math.max(1, Math.ceil(totalTradesCount / tradePageSize) || 1);
  const safeTradePage = Math.min(tradePage, tradePageCount);
  const tradeFrom = totalTradesCount === 0 ? 0 : (safeTradePage - 1) * tradePageSize + 1;
  const tradeTo = Math.min(safeTradePage * tradePageSize, totalTradesCount);

  const paginatedTrades = useMemo(() => {
    const start = (safeTradePage - 1) * tradePageSize;
    return filteredTrades.slice(start, start + tradePageSize);
  }, [filteredTrades, safeTradePage, tradePageSize]);


  async function load() {
    try {
      // Metrics too: a stop hit changes EV, the ledger and the equity curve, not only positions.
      const [p, m] = await Promise.all([api.paper(), api.metrics()]);
      setPaper(p);
      setMetrics(m);
      setPollError(null);
    } catch (err) {
      setPollError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  async function persist() {
    setBusy(true);
    setError(null);
    try {
      await api.persistMetrics();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function closePosition(symbol: string, unrealized: number) {
    const pnlWord = unrealized >= 0 ? "กำไรลอย" : "ขาดทุนลอย";
    const ok = await confirm({
      title: `ปิด ${symbol} ตอนนี้?`,
      body: `${pnlWord}ประมาณ ${formatNum(unrealized)} ดอลลาร์ จะกลายเป็นผลที่ปิดแล้ว (paper)`,
      confirmLabel: "ปิดโพสิชัน",
      tone: "danger",
    });
    if (!ok) return;
    setClosing(symbol);
    setError(null);
    try {
      await api.closePosition(symbol);
      await load();
      // Home and the shell badges read the dashboard payload, not this page's state.
      onPositionsChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setClosing(null);
    }
  }

  const points = buildEquityPoints(metrics?.trades || [], metrics?.starting_equity);
  const positions = paper?.positions || [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Paper ledger</h1>
          <p className="text-sm text-(--color-muted)">
            Simulated portfolio — no exchange orders from this page.
          </p>
        </div>
        <Button variant="outline" disabled={busy} onClick={() => void persist()}>
          Persist metrics snapshot
        </Button>
      </div>

      {dialog}
      {error ? <p className="text-sm text-danger">{error}</p> : null}
      {pollError ? (
        <p className="rounded-md border border-amber-500/40 bg-amber-950/20 px-3 py-2 text-xs text-amber-300">
          อัปเดตล่าสุดไม่สำเร็จ ({pollError}){paper ? " — ข้อมูลด้านล่างอาจไม่ใช่ล่าสุด" : ""}
        </p>
      ) : null}
      {metrics && metrics.ok === false ? (
        <p className="text-sm text-danger">
          คำนวณ metrics ไม่สำเร็จ: {String((metrics as { error?: unknown }).error ?? "unknown error")}
        </p>
      ) : null}
      {!paper && !pollError ? (
        <p className="text-sm text-(--color-muted)">กำลังโหลดสมุด paper…</p>
      ) : null}

      <KpiStrip
        items={[
          { label: "Equity", value: paper?.equity },
          { label: "Cash", value: paper?.cash },
          { label: "EV", value: metrics?.expected_value_ev },
          {
            label: "Win rate",
            value: metrics?.win_rate != null ? Number(metrics.win_rate) * 100 : null,
            hint: "percent",
          },
        ]}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Equity curve</CardTitle>
            <CardDescription>แต่ละจุดคือไม้ที่ปิดหนึ่งไม้</CardDescription>
          </CardHeader>
          <CardContent>
            <EquityChart points={points} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Win / loss count</CardTitle>
            <CardDescription>
              MDD {formatNum(metrics?.max_drawdown_pct)}%
              {metrics?.ratios_ready
                ? ` · Sharpe ${formatNum(metrics.sharpe_ratio)}`
                : ` · ratios need ≥${metrics?.need_trades_for_ratios ?? "—"} trades`}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <WinLossChart
              wins={Number(metrics?.wins || 0)}
              losses={Number(metrics?.losses || 0)}
              scratches={Number(metrics?.scratches || 0)}
            />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Auto-decide quality</CardTitle>
          <CardDescription>
            Closed-trade EV by decision source (30d). Soft gate never loosens risk.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          {(() => {
            const src = metrics?.by_decision_source_30d || {};
            const keys = visibleSourceKeys(src);
            if (keys.length === 0) {
              return (
                <p className="text-(--color-muted)">
                  No attributed closed trades yet. Approve fills stamp{" "}
                  <code>decision_source</code> for later EV splits.
                </p>
              );
            }
            const gate = metrics?.auto_ev_gate;
            return (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="text-(--color-muted)">
                      <tr className="border-b border-border">
                        <th className="py-2 pr-3">Source</th>
                        <th className="py-2 pr-3">N</th>
                        <th className="py-2 pr-3">EV</th>
                        <th className="py-2 pr-3">Win rate</th>
                      </tr>
                    </thead>
                    <tbody>
                      {keys.map((k) => {
                        const row = src[k];
                        return (
                          <tr key={k} className="border-b border-border/60">
                            <td className="py-2 pr-3 font-medium">{decisionSourceLabel(k)}</td>
                            <td className="py-2 pr-3 tabular-nums">{row?.closed_trades ?? 0}</td>
                            <td className="py-2 pr-3 tabular-nums">
                              {formatNum(row?.expected_value_ev)}
                            </td>
                            <td className="py-2 pr-3 tabular-nums">
                              {row?.win_rate != null
                                ? `${formatNum(Number(row.win_rate) * 100)}%`
                                : "—"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                {(() => {
                  const copy = evGateOwnerCopy(gate);
                  return (
                    <p className="text-xs text-(--color-muted)">
                      <span className="font-medium text-(--color-foreground)">{copy.title}</span>
                      {" — "}
                      {copy.detail}
                    </p>
                  );
                })()}
              </>
            );
          })()}
        </CardContent>
      </Card>

      {metrics?.loss_lesson ? (
        <Card>
          <CardHeader>
            <CardTitle>บทเรียนจากไม้ที่ปิดแล้ว</CardTitle>
            <CardDescription>ตัวเลขจากสมุด paper ปัจจุบัน อัปเดตเมื่อมีไม้ปิดเพิ่ม</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <p>
              ปิดแล้ว {metrics.loss_lesson.closed_trades} ไม้ ชนะ {metrics.loss_lesson.wins} ไม้{" "}
              {evSign(metrics.expected_value_ev)}
            </p>
            <p>
              ไม้ที่แพ้ปิดที่จุดตัด {metrics.loss_lesson.by_exit.stop_hit.n} ไม้ รวม{" "}
              {formatNum(metrics.loss_lesson.by_exit.stop_hit.pnl_usd)} ดอลลาร์ ปิดตาม FLAT{" "}
              {metrics.loss_lesson.by_exit.flat.n} ไม้ รวม{" "}
              {formatNum(metrics.loss_lesson.by_exit.flat.pnl_usd)} ดอลลาร์
              {metrics.loss_lesson.by_exit.manual ? (
                <>
                  {" "}
                  และปิดเอง {metrics.loss_lesson.by_exit.manual.n} ไม้ รวม{" "}
                  {formatNum(metrics.loss_lesson.by_exit.manual.pnl_usd)} ดอลลาร์
                </>
              ) : null}
            </p>
            <p>{worstSymbol(metrics.loss_lesson)}</p>
            <p>{floorLine(metrics.loss_lesson)}</p>
            <p className="text-(--color-muted)">
              รีเพลย์ 2026-10-14 ของกฎชุดเดิมติดลบทั้งช่วงในตัวอย่างและนอกตัวอย่าง
              หน้านี้ไม่ใช่เหตุให้เปลี่ยนสูตร
            </p>
          </CardContent>
        </Card>
      ) : null}

      <Card className="border-border">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <CardTitle>Open positions</CardTitle>
              <CardDescription>
                อัปเดตทุก 30 วินาที จากแท่งปิดล่าสุด · คุ้มครองด้วยจุดตัด 1.5× ATR, บังทุน 0.5R และ Trailing 1.0R
              </CardDescription>
            </div>
            {positions.length > 0 ? (
              <div className="flex items-center gap-3 text-xs">
                <span className="text-(--color-muted)">
                  Total uPnL:{" "}
                  <span
                    className={cn(
                      "font-mono font-medium",
                      positions.reduce((s, p) => s + (Number(p.unrealized_pnl) || 0), 0) >= 0
                        ? "text-ok"
                        : "text-danger",
                    )}
                  >
                    {positions.reduce((s, p) => s + (Number(p.unrealized_pnl) || 0), 0) >= 0
                      ? "+"
                      : ""}
                    {formatNum(positions.reduce((s, p) => s + (Number(p.unrealized_pnl) || 0), 0))}{" "}
                    USD
                  </span>
                </span>
                <Badge variant="ok">{positions.length} Active</Badge>
              </div>
            ) : null}
          </div>
        </CardHeader>
        <CardContent>
          {positions.length === 0 ? (
            <p className="text-sm text-(--color-muted)">
              No open paper positions. Run <code>jarvise paper run</code> then Approve.
            </p>
          ) : (
            <div className="overflow-x-auto -mx-1 sm:mx-0">
              <table className="w-full min-w-175 text-left text-sm">
                <thead className="text-(--color-muted) border-b border-border text-xs">
                  <tr>
                    <th className="py-2.5 pr-3 font-medium">Symbol</th>
                    <th className="py-2.5 pr-3 font-medium">Side</th>
                    <th className="py-2.5 pr-3 font-medium">Qty</th>
                    <th className="py-2.5 pr-3 font-medium">Entry</th>
                    <th className="py-2.5 pr-3 font-medium">Stop / Protection</th>
                    <th className="py-2.5 pr-3 font-medium">Stop dist</th>
                    <th className="py-2.5 pr-3 font-medium">uPnL</th>
                    <th className="py-2.5 pr-3 font-medium">R-Multiple</th>
                    <th className="py-2.5 text-right font-medium">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/50">
                  {positions.map((p, i) => {
                    const entry = Number(p.entry_price);
                    const stop = p.stop_price != null ? Number(p.stop_price) : null;
                    const dist =
                      stop != null && Number.isFinite(entry) && entry !== 0
                        ? (Math.abs(entry - stop) / Math.abs(entry)) * 100
                        : null;
                    const unrealized = Number(p.unrealized_pnl) || 0;
                    const stopSource = String(p.stop_source ?? "").toLowerCase();
                    const symbol = String(p.symbol ?? "");
                    const side = String(p.side ?? "").toLowerCase();

                    const closeAdvice = evaluateCloseRecommendation(p);
                    const rMultiple = closeAdvice.rMultiple;

                    return (
                      <tr key={symbol || i} className="hover:bg-card/50 transition-colors">
                            <td className="py-3 pr-3 font-semibold">{symbol}</td>
                            <td className="py-3 pr-3">
                              <span
                                className={cn(
                                  "inline-block rounded px-1.5 py-0.5 text-xs font-mono uppercase font-semibold",
                                  side === "long"
                                    ? "bg-ok/15 text-ok"
                                    : "bg-rose-500/15 text-rose-400",
                                )}
                              >
                                {side}
                              </span>
                            </td>
                            <td className="py-3 pr-3 tabular-nums font-mono text-xs">
                              {formatQty(p.qty)}
                            </td>
                            <td className="py-3 pr-3 tabular-nums font-mono text-xs">
                              {formatNum(p.entry_price)}
                            </td>
                            <td className="py-3 pr-3 font-mono text-xs">
                              <div className="flex flex-wrap items-center gap-1.5">
                                <span className="tabular-nums">
                                  {stop != null ? formatNum(stop) : "—"}
                                </span>
                                {stopSource === "breakeven" ? (
                                  <Badge variant="ok" className="text-[10px] px-1 py-0">
                                    บังทุน (BE +20bps)
                                  </Badge>
                                ) : stopSource === "trail" ? (
                                  <Badge variant="default" className="text-[10px] px-1 py-0">
                                    ล็อกกำไร (Trail 1R)
                                  </Badge>
                                ) : (
                                  <Badge variant="outline" className="text-[10px] px-1 py-0">
                                    จุดตัด 1.5× ATR
                                  </Badge>
                                )}
                              </div>
                            </td>
                            <td className="py-3 pr-3 tabular-nums font-mono text-xs text-(--color-muted)">
                              {dist != null ? `${formatNum(dist)}%` : "—"}
                            </td>
                            <td className="py-3 pr-3 tabular-nums font-mono text-xs font-medium">
                              <span
                                className={
                                  unrealized >= 0
                                    ? "text-ok"
                                    : "text-danger"
                                }
                              >
                                {unrealized >= 0 ? "+" : ""}
                                {formatNum(unrealized)}
                              </span>
                            </td>
                            <td className="py-3 pr-3 font-mono text-xs">
                              {rMultiple != null ? (
                                <div className="space-y-1">
                                  <span
                                    className={cn(
                                      "font-medium",
                                      rMultiple >= 0
                                        ? "text-ok"
                                        : "text-danger",
                                    )}
                                  >
                                    {rMultiple >= 0 ? "+" : ""}
                                    {formatNum(rMultiple)}R
                                  </span>
                                  <div className="h-1.5 w-16 rounded-full bg-border overflow-hidden">
                                    <div
                                      className={cn(
                                        "h-full rounded-full transition-all",
                                        rMultiple >= 1.0
                                          ? "bg-(--color-accent)"
                                          : rMultiple >= 0.5
                                            ? "bg-ok"
                                            : rMultiple > 0
                                              ? "bg-amber-400"
                                              : "bg-rose-500",
                                      )}
                                      style={{
                                        width: `${Math.min(100, Math.max(10, Math.abs(rMultiple) * 100))}%`,
                                      }}
                                    />
                                  </div>
                                </div>
                              ) : (
                                "—"
                              )}
                            </td>
                            <td className="py-3 text-right">
                              <div className="inline-flex flex-col items-end gap-1">
                                <Button
                                  variant="outline"
                                  size="sm"
                                  className={cn(
                                    "text-xs transition-all duration-200 border flex items-center gap-1.5 font-medium shadow-sm",
                                    closeAdvice.btnClass,
                                  )}
                                  disabled={closing === symbol}
                                  onClick={() => void closePosition(symbol, unrealized)}
                                  title={closeAdvice.tooltip}
                                >
                                  <span
                                    className={cn(
                                      "inline-block w-2.5 h-2.5 rounded-full shrink-0",
                                      closeAdvice.dotClass,
                                    )}
                                  />
                                  <span>{closing === symbol ? "กำลังปิด…" : "ปิดไม้ (FLAT)"}</span>
                                </Button>
                                <span
                                  className={cn(
                                    "text-[10px] font-mono font-medium flex items-center gap-1",
                                    closeAdvice.textClass,
                                  )}
                                  title={closeAdvice.tooltip}
                                >
                                  {closeAdvice.label}
                                </span>
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>

          {metrics?.trades && metrics.trades.length > 0 ? (
            <Card className="border-border">
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div>
                    <CardTitle>Closed Trades Ledger</CardTitle>
                    <CardDescription>
                      ประวัติผลการเทรดที่ปิดรอบแล้ว เรียงจากรายการล่าสุดอยู่แถวบนสุด พร้อมตัวกรองและระบบแบ่งหน้า
                    </CardDescription>
                  </div>
                  <Badge variant="outline">{metrics.trades.length} Closed Rounds</Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                {/* Filter Controls matching DecisionsPage style */}
                <div className="p-3 rounded-lg border border-border/60 bg-card/40 space-y-3">
                  <form
                    className="flex flex-wrap items-end gap-3"
                    onSubmit={(e) => {
                      e.preventDefault();
                      setTradePage(1);
                    }}
                  >
                    <label className="text-xs text-(--color-muted)">
                      Symbol
                      <input
                        className="mt-1 block rounded-md border border-border bg-[#161d27] px-2 py-1.5 text-sm text-(--color-foreground) font-mono"
                        value={filterSymbol}
                        onChange={(e) => {
                          setFilterSymbol(e.target.value);
                          setTradePage(1);
                        }}
                        placeholder="BTCUSDT"
                      />
                    </label>
                    <label className="text-xs text-(--color-muted)">
                      Outcome
                      <select
                        className="mt-1 block rounded-md border border-border bg-[#161d27] px-2 py-1.5 text-sm text-(--color-foreground)"
                        value={filterOutcome}
                        onChange={(e) => {
                          setFilterOutcome(e.target.value as "ALL" | "WIN" | "LOSS" | "SCRATCH");
                          setTradePage(1);
                        }}
                      >
                        <option value="ALL">All Outcomes</option>
                        <option value="WIN">Wins (+PnL)</option>
                        <option value="LOSS">Losses (-PnL)</option>
                        <option value="SCRATCH">Scratch (0 PnL)</option>
                      </select>
                    </label>
                    <label className="text-xs text-(--color-muted)">
                      Exit Reason
                      <select
                        className="mt-1 block rounded-md border border-border bg-[#161d27] px-2 py-1.5 text-sm text-(--color-foreground)"
                        value={filterExitReason}
                        onChange={(e) => {
                          setFilterExitReason(e.target.value);
                          setTradePage(1);
                        }}
                      >
                        <option value="ALL">All Reasons</option>
                        <option value="stop">Stop Hit (ขาดทุน)</option>
                        <option value="profit_lock">Stop ล็อกกำไร</option>
                        <option value="flat">FLAT Signal</option>
                        <option value="manual">Manual Close</option>
                      </select>
                    </label>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        setFilterSymbol("");
                        setFilterOutcome("ALL");
                        setFilterExitReason("ALL");
                        setTradePage(1);
                      }}
                    >
                      Clear Filters
                    </Button>
                  </form>

                  {/* Quick Filter Chips */}
                  <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-border/50 text-xs text-(--color-muted)">
                    <span>Quick:</span>
                    {["ALL", "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"].map((sym) => {
                      const active =
                        (sym === "ALL" && !filterSymbol) ||
                        filterSymbol.toUpperCase() === sym;
                      return (
                        <button
                          key={sym}
                          type="button"
                          onClick={() => {
                            setFilterSymbol(sym === "ALL" ? "" : sym);
                            setTradePage(1);
                          }}
                          className={cn(
                            "rounded min-h-11 px-3 sm:min-h-0 sm:px-2 py-0.5 font-mono text-[11px] transition-colors",
                            active
                              ? "bg-(--color-accent) text-white font-medium"
                              : "bg-[#161d27] hover:bg-border text-(--color-foreground)",
                          )}
                        >
                          {sym}
                        </button>
                      );
                    })}
                    <span className="ml-2">Outcome:</span>
                    {[
                      { label: "ALL", val: "ALL" },
                      { label: "Wins 🟢", val: "WIN" },
                      { label: "Losses 🔴", val: "LOSS" },
                    ].map((item) => {
                      const active = filterOutcome === item.val;
                      return (
                        <button
                          key={item.val}
                          type="button"
                          onClick={() => {
                            setFilterOutcome(
                              item.val as "ALL" | "WIN" | "LOSS" | "SCRATCH",
                            );
                            setTradePage(1);
                          }}
                          className={cn(
                            "rounded min-h-11 px-3 sm:min-h-0 sm:px-2 py-0.5 text-[11px] transition-colors",
                            active
                              ? "bg-(--color-accent) text-white font-medium"
                              : "bg-[#161d27] hover:bg-border text-(--color-foreground)",
                          )}
                        >
                          {item.label}
                        </button>
                      );
                    })}
                  </div>
                </div>

                {paginatedTrades.length === 0 ? (
                  <div className="py-8 text-center text-sm text-(--color-muted)">
                    ไม่พบประวัติการเทรดที่ตรงกับเงื่อนไขการค้นหา
                  </div>
                ) : (
                  <>
                    <div className="overflow-x-auto -mx-1 sm:mx-0">
                      <table className="w-full min-w-155 text-left text-sm">
                        <thead className="text-(--color-muted) border-b border-border text-xs sticky top-0 bg-card z-10">
                          <tr>
                            <th className="py-2.5 pr-2 w-12">No</th>
                            <th className="py-2.5 pr-3">Closed Date</th>
                            <th className="py-2.5 pr-3">Symbol</th>
                            <th className="py-2.5 pr-3">Exit Reason</th>
                            <th className="py-2.5 pr-3 text-right">PnL (USD)</th>
                            <th className="py-2.5 text-right">Outcome</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-border/50">
                          {paginatedTrades.map((t, idx) => {
                            const rowNo = tradeFrom + idx;
                            const pnl = Number(t.pnl_usd) || 0;
                            // Same format as Decisions: Gregorian date, Asia/Bangkok time.
                            const stamp = formatDecisionStamp(t.close_ts);
                            const dateStr =
                              stamp.date === "—" ? "—" : `${stamp.date} ${stamp.time.slice(0, 5)}`;
                            return (
                              <tr
                                key={idx}
                                className="hover:bg-card/50 transition-colors"
                              >
                                <td className="py-2.5 pr-2 text-xs font-mono text-(--color-muted)">
                                  {rowNo}
                                </td>
                                <td className="py-2.5 pr-3 text-xs text-(--color-muted) font-mono">
                                  {dateStr}
                                </td>
                                <td className="py-2.5 pr-3 font-medium font-mono text-xs">
                                  {t.symbol || "—"}
                                </td>
                                <td className="py-2.5 pr-3">
                                  {exitReasonBadge(t.exit_reason, Number(t.pnl_usd) || 0)}
                                </td>
                                <td className="py-2.5 pr-3 tabular-nums font-mono text-xs text-right font-medium">
                                  <span
                                    className={
                                      pnl > 0
                                        ? "text-ok"
                                        : pnl < 0
                                          ? "text-danger"
                                          : "text-(--color-muted)"
                                    }
                                  >
                                    {pnl > 0 ? "+" : ""}
                                    {formatNum(pnl)}
                                  </span>
                                </td>
                                <td className="py-2.5 text-right">
                                  <Badge
                                    variant={pnl > 0 ? "ok" : pnl < 0 ? "danger" : "muted"}
                                    className="text-[10px] uppercase font-mono"
                                  >
                                    {pnl > 0 ? "WIN" : pnl < 0 ? "LOSS" : "SCRATCH"}
                                  </Badge>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>

                    {/* Pagination Controls */}
                    <div className="mt-4 flex flex-wrap items-center justify-between gap-3 pt-2 border-t border-border/50">
                      <p className="text-xs text-(--color-muted)">
                        Showing {tradeFrom}–{tradeTo} of {totalTradesCount}
                        {totalTradesCount !== metrics.trades.length
                          ? ` (filtered from ${metrics.trades.length})`
                          : ""}
                      </p>
                      <div className="flex flex-wrap items-center gap-3">
                        <label className="flex items-center gap-2 text-xs text-(--color-muted)">
                          Rows
                          <select
                            className="rounded-md border border-border bg-[#161d27] px-2 py-1.5 text-sm text-(--color-foreground)"
                            value={tradePageSize}
                            onChange={(e) => {
                              setTradePageSize(Number(e.target.value));
                              setTradePage(1);
                            }}
                          >
                            {PAGE_SIZES.map((n) => (
                              <option key={n} value={n}>
                                {n}
                              </option>
                            ))}
                          </select>
                        </label>
                        <Pagination className="mx-0 w-auto justify-end">
                          <PaginationContent>
                            <PaginationItem>
                              <PaginationPrevious
                                disabled={safeTradePage <= 1}
                                onClick={() => setTradePage((p) => Math.max(1, p - 1))}
                              />
                            </PaginationItem>
                            <PaginationItem>
                              <span className="px-2 text-xs text-(--color-muted)">
                                Page {safeTradePage} / {tradePageCount}
                              </span>
                            </PaginationItem>
                            <PaginationItem>
                              <PaginationNext
                                disabled={safeTradePage >= tradePageCount}
                                onClick={() =>
                                  setTradePage((p) =>
                                    Math.min(tradePageCount, p + 1),
                                  )
                                }
                              />
                            </PaginationItem>
                          </PaginationContent>
                        </Pagination>
                      </div>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>
          ) : null}
    </div>
  );
}
