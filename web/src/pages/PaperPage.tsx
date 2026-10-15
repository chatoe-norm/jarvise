import { useEffect, useState } from "react";
import { EquityChart, WinLossChart } from "@/components/Charts";
import { KpiStrip } from "@/components/KpiStrip";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type LossLesson, type MetricsPayload, type PaperPayload } from "@/lib/api";
import { buildEquityPoints } from "@/lib/equity";
import { decisionSourceLabel } from "@/lib/copy";
import { cn, formatNum, formatQty } from "@/lib/utils";

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

export function PaperPage() {
  const [paper, setPaper] = useState<PaperPayload | null>(null);
  const [metrics, setMetrics] = useState<MetricsPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [closing, setClosing] = useState<string | null>(null);

  async function load() {
    setError(null);
    try {
      const [p, m] = await Promise.all([api.paper(), api.metrics()]);
      setPaper(p);
      setMetrics(m);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      void api
        .paper()
        .then((next) => {
          setPaper(next);
          setError(null);
        })
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : String(err));
        });
    }, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  async function persist() {
    setBusy(true);
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
    const ok = window.confirm(
      `ปิด ${symbol} ตอนนี้? กำไรลอยประมาณ ${formatNum(unrealized)} ดอลลาร์ จะกลายเป็นผลที่ปิดแล้ว`,
    );
    if (!ok) return;
    setClosing(symbol);
    setError(null);
    try {
      await api.closePosition(symbol);
      await load();
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
          <p className="text-sm text-[var(--color-muted)]">
            Simulated portfolio — no exchange orders from this page.
          </p>
        </div>
        <Button variant="outline" disabled={busy} onClick={() => void persist()}>
          Persist metrics snapshot
        </Button>
      </div>

      {error ? <p className="text-sm text-[var(--color-danger)]">{error}</p> : null}

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
            const keys = Object.keys(src);
            if (keys.length === 0) {
              return (
                <p className="text-[var(--color-muted)]">
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
                    <thead className="text-[var(--color-muted)]">
                      <tr className="border-b border-[var(--color-border)]">
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
                          <tr key={k} className="border-b border-[var(--color-border)]/60">
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
                {gate?.enabled ? (
                  <p className="text-xs text-[var(--color-muted)]">
                    Soft EV gate:{" "}
                    {gate.blocked
                      ? `blocking auto-decide (EV ${formatNum(gate.ev)} < ${formatNum(gate.min_ev)}, N=${gate.n})`
                      : `armed (min EV ${formatNum(gate.min_ev)}, need N≥${gate.min_n}; now N=${gate.n}, EV ${formatNum(gate.ev)})`}
                  </p>
                ) : (
                  <p className="text-xs text-[var(--color-muted)]">
                    Soft EV gate off — set <code>JARVISE_AUTO_DECIDE_MIN_EV</code> to enable.
                  </p>
                )}
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
              {formatNum(metrics.loss_lesson.by_exit.stop_hit.pnl_usd)} ดอลลาร์ และปิดตาม FLAT{" "}
              {metrics.loss_lesson.by_exit.flat.n} ไม้ รวม{" "}
              {formatNum(metrics.loss_lesson.by_exit.flat.pnl_usd)} ดอลลาร์
            </p>
            <p>{worstSymbol(metrics.loss_lesson)}</p>
            <p>{floorLine(metrics.loss_lesson)}</p>
            <p className="text-[var(--color-muted)]">
              รีเพลย์ 2026-10-14 ของกฎชุดเดิมติดลบทั้งช่วงในตัวอย่างและนอกตัวอย่าง
              หน้านี้ไม่ใช่เหตุให้เปลี่ยนสูตร
            </p>
          </CardContent>
        </Card>
      ) : null}

      <Card className="border-[var(--color-border)]">
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
                <span className="text-[var(--color-muted)]">
                  Total uPnL:{" "}
                  <span
                    className={cn(
                      "font-mono font-medium",
                      positions.reduce((s, p) => s + (Number(p.unrealized_pnl) || 0), 0) >= 0
                        ? "text-[var(--color-ok)]"
                        : "text-[var(--color-danger)]",
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
            <p className="text-sm text-[var(--color-muted)]">
              No open paper positions. Run <code>jarvise paper run</code> then Approve.
            </p>
          ) : (
            <div className="overflow-x-auto -mx-1 sm:mx-0">
              <table className="w-full min-w-[700px] text-left text-sm">
                <thead className="text-[var(--color-muted)] border-b border-[var(--color-border)] text-xs">
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
                <tbody className="divide-y divide-[var(--color-border)]/50">
                  {positions.map((p, i) => {
                    const entry = Number(p.entry_price);
                    const stop = p.stop_price != null ? Number(p.stop_price) : null;
                    const dist =
                      stop != null && Number.isFinite(entry) && entry !== 0
                        ? (Math.abs(entry - stop) / Math.abs(entry)) * 100
                        : null;
                    const qty = Number(p.qty);
                    const unrealized = Number(p.unrealized_pnl) || 0;
                    const initialStop =
                      p.initial_stop_price != null ? Number(p.initial_stop_price) : stop;
                    const risk =
                      initialStop != null && Number.isFinite(entry)
                        ? Math.abs(entry - initialStop)
                        : null;
                    const rMultiple =
                      risk != null && risk > 0 && Number.isFinite(qty) && qty !== 0
                        ? unrealized / (qty * risk)
                        : null;
                    const stopSource = String(p.stop_source ?? "").toLowerCase();
                    const symbol = String(p.symbol ?? "");
                    const side = String(p.side ?? "").toLowerCase();

                    return (
                      <tr key={i} className="hover:bg-[var(--color-card)]/50 transition-colors">
                        <td className="py-3 pr-3 font-semibold">{symbol}</td>
                        <td className="py-3 pr-3">
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
                        <td className="py-3 pr-3 tabular-nums font-mono text-xs text-[var(--color-muted)]">
                          {dist != null ? `${formatNum(dist)}%` : "—"}
                        </td>
                        <td className="py-3 pr-3 tabular-nums font-mono text-xs font-medium">
                          <span
                            className={
                              unrealized >= 0
                                ? "text-[var(--color-ok)]"
                                : "text-[var(--color-danger)]"
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
                                    ? "text-[var(--color-ok)]"
                                    : "text-[var(--color-danger)]",
                                )}
                              >
                                {rMultiple >= 0 ? "+" : ""}
                                {formatNum(rMultiple)}R
                              </span>
                              <div className="h-1.5 w-16 rounded-full bg-[var(--color-border)] overflow-hidden">
                                <div
                                  className={cn(
                                    "h-full rounded-full transition-all",
                                    rMultiple >= 1.0
                                      ? "bg-[var(--color-accent)]"
                                      : rMultiple >= 0.5
                                        ? "bg-[var(--color-ok)]"
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
                          <Button
                            variant="outline"
                            size="sm"
                            className="text-xs text-[var(--color-danger)] hover:bg-[var(--color-danger)]/10 border-[var(--color-border)]"
                            disabled={closing === symbol}
                            onClick={() => void closePosition(symbol, unrealized)}
                          >
                            {closing === symbol ? "กำลังปิด…" : "ปิดไม้ (FLAT)"}
                          </Button>
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
        <Card className="border-[var(--color-border)]">
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle>Closed Trades Ledger</CardTitle>
              <Badge variant="outline">{metrics.trades.length} Closed Rounds</Badge>
            </div>
            <CardDescription>
              ประวัติผลการเทรดที่ปิดรอบแล้วตามลำดับ FIFO พร้อมผลกำไร/ขาดทุนสุทธิ
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto -mx-1 sm:mx-0 max-h-80">
              <table className="w-full min-w-[420px] text-left text-sm">
                <thead className="text-[var(--color-muted)] border-b border-[var(--color-border)] text-xs sticky top-0 bg-[var(--color-card)]">
                  <tr>
                    <th className="py-2 pr-3">Closed Date</th>
                    <th className="py-2 pr-3">Symbol</th>
                    <th className="py-2 pr-3 text-right">PnL (USD)</th>
                    <th className="py-2 text-right">Outcome</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--color-border)]/50">
                  {metrics.trades.map((t, idx) => {
                    const pnl = Number(t.pnl_usd) || 0;
                    const dateStr =
                      t.close_ts != null
                        ? new Date(t.close_ts).toLocaleString("th-TH", {
                            dateStyle: "short",
                            timeStyle: "short",
                          })
                        : "—";
                    return (
                      <tr key={idx} className="hover:bg-[var(--color-card)]/50 transition-colors">
                        <td className="py-2.5 pr-3 text-xs text-[var(--color-muted)] font-mono">
                          {dateStr}
                        </td>
                        <td className="py-2.5 pr-3 font-medium font-mono text-xs">
                          {t.symbol || "—"}
                        </td>
                        <td className="py-2.5 pr-3 tabular-nums font-mono text-xs text-right font-medium">
                          <span
                            className={
                              pnl > 0
                                ? "text-[var(--color-ok)]"
                                : pnl < 0
                                  ? "text-[var(--color-danger)]"
                                  : "text-[var(--color-muted)]"
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
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
