import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type PipelineRunResult } from "@/lib/api";
import { relativeAge } from "@/lib/utils";

interface PipelineControlCardProps {
  statusPayload: Record<string, unknown> | null;
  onRefresh?: () => void;
}

interface JobDef {
  key: string;
  title: string;
  schedule: string;
  description: string;
  dataKey: string;
}

const PIPELINE_JOBS: JobDef[] = [
  {
    key: "ingest",
    title: "Market Ingest & Indicators",
    schedule: "ทุก 1 ชม. & ทุกแท่ง 4h",
    description: "ดึงราคา Binance klines (1h, 4h, 1d) และคำนวณ EMA20/EMA200, RSI, ATR",
    dataKey: "ingest",
  },
  {
    key: "rag",
    title: "Trading Rules & RAG Sync",
    schedule: "ทุกวัน 07:00 / 19:00",
    description: "ซิงค์บันทึกกฎการเทรด NotebookLM และสร้าง Vector Index ลง Qdrant",
    dataKey: "rag",
  },
  {
    key: "paper_run",
    title: "Signal Scan & Enqueue",
    schedule: "ทุกแท่ง 4h ปิด (Bar Close)",
    description: "สแกนสัญญาณเทรดตามกฎ Pullback/Breakout แล้วเข้าคิวรออนุมัติ (Approval Queue)",
    dataKey: "paper",
  },
  {
    key: "paper_expire",
    title: "Paper Timeout & Expire",
    schedule: "ทุก 5 นาที",
    description: "ตรวจเช็กและยกเลิกคำสั่งที่หมดอายุ (Expires At) หากไม่ได้รับการอนุมัติ",
    dataKey: "paper_expire",
  },
  {
    key: "risk_monitor",
    title: "Risk Monitor (MTM & Stops)",
    schedule: "ทุก 30 วินาที",
    description: "คำนวณ Mark-to-Market, ตรวจสอบ Stop Loss 1R, และบังคับใช้ Daily Loss Cap",
    dataKey: "risk_monitor",
  },
  {
    key: "ingest_health",
    title: "Ingest Health Probe",
    schedule: "ทุก 15 นาที",
    description: "ตรวจความต่อเนื่องของแท่งเทียน, ความสดใหม่, และความพร้อมของ EMA200",
    dataKey: "ingest_health",
  },
  {
    key: "paper_auto_decide",
    title: "Paper Auto-Decide (Claude)",
    schedule: "หลัง Ingest/Signal สำเร็จ",
    description: "Claude Sonnet ช่วยวิเคราะห์และตัดสินใจอนุมัติ/ปฏิเสธคำสั่งใน Paper Queue",
    dataKey: "paper_auto",
  },
];

function parseJobStatus(payload: unknown): {
  ok: boolean | null;
  label: string;
  ageMs: number | null;
  detail: string;
} {
  if (payload == null) {
    return { ok: null, label: "ยังไม่เคยรัน", ageMs: null, detail: "" };
  }
  if (typeof payload !== "object") {
    return { ok: null, label: String(payload), ageMs: null, detail: "" };
  }
  const obj = payload as Record<string, unknown>;
  const ok = typeof obj.ok === "boolean" ? obj.ok : null;
  const ageMs = typeof obj.at_ms === "number" ? obj.at_ms : null;

  const bits = Object.entries(obj)
    .filter(([k]) => !["ok", "at_ms", "telegram_sent", "raw"].includes(k))
    .slice(0, 3)
    .map(([k, v]) => `${k}=${typeof v === "object" ? "…" : String(v)}`)
    .join(" · ");

  return {
    ok,
    label: ok === true ? "สำเร็จ (OK)" : ok === false ? "ล้มเหลว (Failed)" : "สถานะ",
    ageMs,
    detail: bits,
  };
}

export function PipelineControlCard({ statusPayload, onRefresh }: PipelineControlCardProps) {
  const [runningJob, setRunningJob] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<PipelineRunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rawOpen, setRawOpen] = useState(false);

  const handleRunJob = async (jobKey: string) => {
    setRunningJob(jobKey);
    setError(null);
    try {
      const res = await api.runPipelineJob(jobKey);
      setLastResult(res);
      onRefresh?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunningJob(null);
    }
  };

  return (
    <Card className="border-[var(--color-border)]">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2">
              <span>Pipeline &amp; Job Controllers</span>
              <span className="inline-block h-2 w-2 rounded-full bg-violet-400 shadow-[0_0_8px_rgba(167,139,250,0.8)]" />
            </CardTitle>
            <CardDescription>
              Direct triggers for scheduled pipeline jobs, background workers, and automated trade executors.
            </CardDescription>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onRefresh?.()}
            className="h-8 text-xs font-mono"
          >
            ↻ ซิงค์สถานะ Job
          </Button>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Latest Run Notification Banner */}
        {lastResult && (
          <div
            className={`rounded-lg p-3 text-xs font-mono border ${
              lastResult.ok
                ? "border-emerald-500/40 bg-emerald-950/20 text-emerald-200"
                : "border-rose-500/40 bg-rose-950/20 text-rose-200"
            }`}
          >
            <div className="flex items-center justify-between font-semibold mb-1">
              <span>
                {lastResult.ok ? "✓ รันสำเร็จ:" : "✗ รันไม่สำเร็จ:"} {lastResult.job} ({lastResult.duration_s}s)
              </span>
              <span className="text-[10px] opacity-75">{new Date(lastResult.executed_at).toLocaleTimeString()}</span>
            </div>
            <pre className="overflow-x-auto text-[11px] opacity-90 max-h-36">
              {JSON.stringify(lastResult.result, null, 2)}
            </pre>
          </div>
        )}

        {error && (
          <div className="rounded-md border border-rose-500/40 bg-rose-950/20 p-2.5 text-xs text-rose-300 font-mono">
            {error}
          </div>
        )}

        {/* Jobs List */}
        <div className="space-y-2.5">
          {PIPELINE_JOBS.map((job) => {
            const rawData = statusPayload ? statusPayload[job.dataKey] : null;
            const parsed = parseJobStatus(rawData);
            const isRunning = runningJob === job.key;

            return (
              <div
                key={job.key}
                className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/40 p-3 hover:border-[var(--color-border)]/80 transition-colors"
              >
                <div className="space-y-1 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold text-[var(--color-foreground)]">{job.title}</span>
                    <Badge variant="outline" className="text-[10px] py-0 px-1.5 font-mono text-[var(--color-muted)]">
                      {job.schedule}
                    </Badge>
                    <Badge
                      variant={parsed.ok === true ? "ok" : parsed.ok === false ? "danger" : "muted"}
                      className="text-[10px] py-0 px-1.5"
                    >
                      {parsed.ok === true ? "OK" : parsed.ok === false ? "Fail" : "Idle"}
                    </Badge>
                    {parsed.ageMs && (
                      <span className="text-[11px] text-[var(--color-muted)]">
                        · รันล่าสุด {relativeAge(parsed.ageMs)}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-[var(--color-muted)]">{job.description}</div>
                  {parsed.detail && (
                    <div className="text-[11px] font-mono text-[var(--color-muted)]/80 bg-[#11161f] px-2 py-0.5 rounded border border-[var(--color-border)]/40 inline-block">
                      {parsed.detail}
                    </div>
                  )}
                </div>

                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant={isRunning ? "outline" : "ok"}
                    size="sm"
                    disabled={isRunning || runningJob !== null}
                    onClick={() => void handleRunJob(job.key)}
                    className="h-8 text-xs font-mono font-semibold min-w-[100px]"
                  >
                    {isRunning ? "กำลังรัน…" : "▶ Run Now"}
                  </Button>
                </div>
              </div>
            );
          })}
        </div>

        {/* Raw View Toggle */}
        <div className="pt-2 flex items-center justify-between">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => setRawOpen((v) => !v)}
            className="text-xs font-mono text-[var(--color-muted)]"
          >
            {rawOpen ? "ซ่อน Raw Jobs Payload" : "ดู Raw Jobs Payload JSON"}
          </Button>
        </div>

        {rawOpen && (
          <pre className="overflow-x-auto rounded-md bg-[#0c1017] border border-[var(--color-border)] p-3 text-xs text-[var(--color-muted)] font-mono max-h-64">
            {JSON.stringify(
              {
                ingest: statusPayload?.ingest,
                rag: statusPayload?.rag,
                paper: statusPayload?.paper,
                paper_expire: statusPayload?.paper_expire,
                risk_monitor: statusPayload?.risk_monitor,
                ingest_health: statusPayload?.ingest_health,
                paper_auto: statusPayload?.paper_auto,
              },
              null,
              2,
            )}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}
