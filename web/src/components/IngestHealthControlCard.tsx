import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type BackfillPayload, type IngestHealthStatus } from "@/lib/api";
import { relativeAge } from "@/lib/utils";

interface IngestHealthControlCardProps {
  health: IngestHealthStatus | null | undefined;
  onRefresh?: () => void;
}

export function IngestHealthControlCard({ health, onRefresh }: IngestHealthControlCardProps) {
  const [checking, setChecking] = useState(false);
  const [backfilling, setBackfilling] = useState(false);
  const [selectedTf, setSelectedTf] = useState("4h");
  const [sinceDate, setSinceDate] = useState("2021-01-01");
  const [targetSymbol, setTargetSymbol] = useState("ALL");
  const [feedback, setFeedback] = useState<{ type: "ok" | "err"; msg: string } | null>(null);
  const [backfillResult, setBackfillResult] = useState<BackfillPayload | null>(null);

  const handleRunHealthCheck = async () => {
    setChecking(true);
    setFeedback(null);
    try {
      const res = await api.runIngestHealth();
      if (res.ok) {
        setFeedback({ type: "ok", msg: "ตรวจสอบสถานะความสมบูรณ์ของแท่งเทียนสำเร็จแล้ว" });
        onRefresh?.();
      } else {
        setFeedback({ type: "err", msg: "การตรวจ Ingest Health แจ้งเตือนข้อผิดพลาด" });
      }
    } catch (err) {
      setFeedback({ type: "err", msg: err instanceof Error ? err.message : String(err) });
    } finally {
      setChecking(false);
    }
  };

  const handleStartBackfill = async () => {
    setBackfilling(true);
    setFeedback(null);
    setBackfillResult(null);
    try {
      const symList = targetSymbol === "ALL" ? undefined : [targetSymbol];
      const res = await api.runIngestBackfill({
        symbols: symList,
        timeframe: selectedTf,
        since: sinceDate,
      });
      if (res.ok) {
        setBackfillResult(res);
        setFeedback({
          type: "ok",
          msg: `Backfill สำเร็จเรียบร้อย: ซิงค์ ${res.count} เหรียญ (${res.synced_symbols.join(", ")}) ย้อนหลังตั้งแต่ ${res.since}`,
        });
        onRefresh?.();
      } else {
        setFeedback({ type: "err", msg: "การ Backfill ไม่สำเร็จ กรุณาตรวจสอบ logs" });
      }
    } catch (err) {
      setFeedback({ type: "err", msg: err instanceof Error ? err.message : String(err) });
    } finally {
      setBackfilling(false);
    }
  };

  const symbolsList = health?.symbols ? Object.keys(health.symbols) : [];

  return (
    <Card className="border-[var(--color-border)]">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2">
              <span>Ingest Health &amp; Data Repair</span>
              <span
                className={`inline-block h-2 w-2 rounded-full ${
                  health?.ok
                    ? "bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]"
                    : "bg-rose-400 shadow-[0_0_8px_rgba(251,113,133,0.8)]"
                }`}
              />
            </CardTitle>
            <CardDescription>
              Continuous candle continuity, EMA200 warm-up verification, and interactive one-click data repair.
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant={health?.ok ? "ok" : "danger"}>
              {health?.ok ? "Data Nominal" : "Attention Required"}
            </Badge>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={checking || backfilling}
              onClick={() => void handleRunHealthCheck()}
              className="h-8 text-xs font-mono font-medium"
            >
              {checking ? "กำลังตรวจสอบ…" : "⚡ Run Health Check Now"}
            </Button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Status Meta */}
        {health && (
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[var(--color-muted)]">
            <div>
              กรอบเวลาหลัก: <span className="font-mono font-semibold text-[var(--color-foreground)]">{health.timeframe}</span> ·
              ตรวจสอบล่าสุด: {relativeAge(health.at_ms)}
            </div>
            {health.telegram_sent && (
              <span className="text-cyan-400 font-mono">✓ ส่งการแจ้งเตือนลง Telegram แล้ว</span>
            )}
          </div>
        )}

        {/* Per-Symbol Matrix */}
        {!health ? (
          <div className="rounded-lg border border-[var(--color-border)] p-4 text-center text-sm text-[var(--color-muted)]">
            ยังไม่มีข้อมูลผลการตรวจ Ingest Health กรุณากดปุ่ม <b>"⚡ Run Health Check Now"</b> ด้านบน
          </div>
        ) : (
          <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
            {Object.entries(health.symbols).map(([sym, s]) => {
              const emaReady = s.ema200_ready > 0;
              const isFresh = s.newest_age_min != null && s.newest_age_min <= 240;
              const hasGaps = s.gaps > 0;

              return (
                <div
                  key={sym}
                  className={`rounded-lg border p-3 space-y-1.5 transition-all ${
                    emaReady && isFresh && !hasGaps
                      ? "border-[var(--color-border)] bg-[var(--color-card)]/40"
                      : "border-amber-500/40 bg-amber-950/10"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-sm text-[var(--color-foreground)]">{sym}</span>
                    <Badge
                      variant={emaReady ? "ok" : "outline"}
                      className={
                        emaReady
                          ? "text-[10px] py-0 px-1 font-mono"
                          : "text-[10px] py-0 px-1 font-mono text-amber-400 border-amber-500/40 bg-amber-950/20"
                      }
                    >
                      {emaReady ? "EMA200 OK" : "EMA200 Null"}
                    </Badge>
                  </div>
                  <div className="grid grid-cols-2 gap-1 text-[11px] text-[var(--color-muted)] font-mono">
                    <div>
                      แท่งเทียน: <span className="text-[var(--color-foreground)]">{s.rows}</span>
                    </div>
                    <div>
                      EMA200: <span className={emaReady ? "text-emerald-400" : "text-amber-400"}>{s.ema200_ready}</span>
                    </div>
                    <div>
                      อายุบาร์: <span className={isFresh ? "text-[var(--color-foreground)]" : "text-amber-400"}>{s.newest_age_min ?? "—"}m</span>
                    </div>
                    <div>
                      ช่องว่าง (gaps): <span className={hasGaps ? "text-rose-400 font-bold" : "text-emerald-400"}>{s.gaps}</span>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Active Alerts */}
        {health?.alerts && health.alerts.length > 0 && (
          <div className="rounded-lg border border-amber-500/40 bg-amber-950/20 p-3 space-y-1 text-xs">
            <div className="font-semibold text-amber-300">⚠️ รายการที่ต้องดำเนินการแก้ไข (Alerts):</div>
            <ul className="list-disc space-y-0.5 pl-5 text-amber-200">
              {health.alerts.map((alert, i) => (
                <li key={i}>{alert}</li>
              ))}
            </ul>
          </div>
        )}

        {/* One-Click Backfill & Repair Control Tool */}
        <div className="rounded-lg border border-[var(--color-border)] bg-[#11161f] p-4 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--color-border)]/60 pb-2">
            <div>
              <div className="text-sm font-semibold text-[var(--color-foreground)] flex items-center gap-1.5">
                <span>🔄 One-Click Backfill &amp; Data Repair</span>
              </div>
              <p className="text-xs text-[var(--color-muted)]">
                ซ่อมแซมแท่งเทียนที่ขาดหายและดึงประวัติเพื่อ Warm-up EMA200 ให้พร้อมใช้งาน
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {/* Target Symbols */}
            <div className="space-y-1">
              <label htmlFor="target_symbol" className="text-xs text-[var(--color-muted)] font-mono">เหรียญเป้าหมาย:</label>
              <select
                id="target_symbol"
                value={targetSymbol}
                onChange={(e) => setTargetSymbol(e.target.value)}
                className="w-full rounded-md border border-[var(--color-border)] bg-[#0c1017] px-2.5 py-1.5 text-xs text-[var(--color-foreground)] focus:outline-none focus:ring-1 focus:ring-emerald-500 font-mono"
              >
                <option value="ALL">ทุกลำดับใน Active Universe</option>
                {symbolsList.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>

            {/* Timeframe */}
            <div className="space-y-1">
              <label htmlFor="target_tf" className="text-xs text-[var(--color-muted)] font-mono">กรอบเวลา (Timeframe):</label>
              <select
                id="target_tf"
                value={selectedTf}
                onChange={(e) => setSelectedTf(e.target.value)}
                className="w-full rounded-md border border-[var(--color-border)] bg-[#0c1017] px-2.5 py-1.5 text-xs text-[var(--color-foreground)] focus:outline-none focus:ring-1 focus:ring-emerald-500 font-mono"
              >
                <option value="4h">4h (Paper Trade Timeframe)</option>
                <option value="1h">1h (Microstructure Context)</option>
                <option value="1d">1d (Higher Timeframe Trend)</option>
              </select>
            </div>

            {/* Since Date */}
            <div className="space-y-1">
              <label htmlFor="target_since" className="text-xs text-[var(--color-muted)] font-mono">ดึงย้อนหลังตั้งแต่ (Since):</label>
              <input
                id="target_since"
                type="text"
                value={sinceDate}
                onChange={(e) => setSinceDate(e.target.value)}
                placeholder="2021-01-01"
                className="w-full rounded-md border border-[var(--color-border)] bg-[#0c1017] px-2.5 py-1.5 text-xs text-[var(--color-foreground)] focus:outline-none focus:ring-1 focus:ring-emerald-500 font-mono"
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 pt-2">
            <span className="text-[11px] text-[var(--color-muted)]">
              💡 ระบบจะดึง Binance klines, เติม Gap, คำนวณอินดิเคเตอร์ใหม่ และบันทึกลง SQLite
            </span>
            <Button
              type="button"
              variant="ok"
              size="sm"
              disabled={backfilling || checking}
              onClick={() => void handleStartBackfill()}
              className="text-xs font-mono font-semibold"
            >
              {backfilling ? "กำลังดำเนินการ Backfill…" : "🚀 เริ่มดำเนินการ Backfill & Repair"}
            </Button>
          </div>
        </div>

        {/* Feedback message */}
        {feedback && (
          <div
            className={`rounded-md px-3 py-2 text-xs font-mono ${
              feedback.type === "ok"
                ? "border border-emerald-500/30 bg-emerald-950/20 text-emerald-300"
                : "border border-rose-500/30 bg-rose-950/20 text-rose-300"
            }`}
          >
            {feedback.msg}
          </div>
        )}

        {/* Backfill result detail */}
        {backfillResult && (
          <div className="rounded-md border border-[var(--color-border)] bg-[#0c1017] p-2.5 text-xs font-mono text-[var(--color-muted)]">
            <div className="text-emerald-400 font-bold mb-1">ผลการ Backfill:</div>
            <div>Timeframe: {backfillResult.timeframe} · ตั้งแต่: {backfillResult.since}</div>
            <div>เหรียญที่ซิงค์สำเร็จ ({backfillResult.count}): {backfillResult.synced_symbols.join(", ")}</div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
