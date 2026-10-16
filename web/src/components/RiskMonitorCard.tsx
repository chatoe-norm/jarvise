import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn, formatNum, relativeAge } from "@/lib/utils";

type Obj = Record<string, unknown>;

/** n8n runs the risk monitor every 15 minutes; two missed runs means it is not watching. */
const STALE_AFTER_MS = 35 * 60 * 1000;

const asObj = (v: unknown): Obj | null => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const len = (v: unknown): number => (Array.isArray(v) ? v.length : 0);

/** Paper risk monitor (MTM, stops, profit lock, daily-loss halt) from `status.risk_monitor`. */
export function RiskMonitorCard({ monitor }: { monitor: unknown }) {
  const m = asObj(monitor);
  const atMs = num(m?.at_ms);
  const stale = atMs == null || Date.now() - atMs > STALE_AFTER_MS;
  const halted = m?.halted === true;
  const equity = num(asObj(m?.mtm)?.equity);
  const dayPnl = num(m?.day_mtm_pnl);
  const stops = asObj(m?.stops);
  const moved = len(asObj(m?.profit_lock)?.moved);

  const state = !m
    ? { label: "ยังไม่มีข้อมูล", variant: "muted" as const }
    : m.ok === false
      ? { label: "ล้มเหลว", variant: "danger" as const }
      : halted
        ? { label: "หยุดระบบ (Halted)", variant: "danger" as const }
        : stale
          ? { label: "ข้อมูลเก่า", variant: "outline" as const }
          : { label: "เฝ้าระวังปกติ", variant: "ok" as const };

  return (
    <Card className="border-[var(--color-border)]">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="text-base font-semibold">Paper Risk Monitor</CardTitle>
            <CardDescription>
              Mark-to-market, stops, profit lock และเพดานขาดทุนรายวัน (n8n ทุก 15 นาที)
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant={state.variant}>{state.label}</Badge>
            <span className="text-xs text-[var(--color-muted)]">
              {atMs != null ? `รันล่าสุด ${relativeAge(atMs)}` : ""}
            </span>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {halted ? (
          <p className="rounded-md border border-rose-500/40 bg-rose-950/20 px-3 py-2 text-sm text-rose-300">
            ระบบหยุดเปิดไม้ใหม่: {String(m?.halt_reason || "daily loss / drawdown cap")}
            {num(m?.pending_cancelled) ? ` · ยกเลิกการ์ดที่รออนุมัติ ${num(m?.pending_cancelled)} ใบ` : ""}
          </p>
        ) : null}
        {m && stale && !halted ? (
          <p className="text-xs text-amber-400">
            ไม่มีผลรันใหม่เกิน 35 นาที — ตรวจ n8n workflow &quot;Jarvise risk monitor&quot;
          </p>
        ) : null}
        <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-4">
          <Stat label="Equity (MTM)" value={equity != null ? `$${formatNum(equity)}` : "—"} />
          <Stat
            label="PnL วันนี้"
            value={dayPnl != null ? formatNum(dayPnl) : "—"}
            tone={dayPnl == null ? undefined : dayPnl < 0 ? "bad" : dayPnl > 0 ? "good" : undefined}
          />
          <Stat label="Stop ปิดรอบนี้" value={String(num(stops?.closed) ?? 0)} />
          <Stat label="Profit lock ขยับ" value={String(moved)} />
        </div>
      </CardContent>
    </Card>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "good" | "bad" }) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/40 p-3">
      <div className="text-[11px] text-[var(--color-muted)]">{label}</div>
      <div
        className={cn(
          "mt-0.5 font-mono text-base font-semibold tabular-nums",
          tone === "bad" && "text-rose-400",
          tone === "good" && "text-emerald-400",
        )}
      >
        {value}
      </div>
    </div>
  );
}
