import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { api, type AutoDecideConfigPayload, type EvGateStatus, type PaperAutoStatus } from "@/lib/api";
import { evGateOwnerCopy, resolveReasonLabel } from "@/lib/copy";
import { cn, relativeAge } from "@/lib/utils";

interface PaperAutoDecideCardProps {
  auto: PaperAutoStatus | null | undefined;
  gate?: EvGateStatus | null;
  initialConfig?: AutoDecideConfigPayload | null;
  onRefresh?: () => void;
}

export function PaperAutoDecideCard({ auto, gate, initialConfig, onRefresh }: PaperAutoDecideCardProps) {
  // Config state
  const [enabled, setEnabled] = useState<boolean>(initialConfig?.enabled ?? false);
  const [riskPct, setRiskPct] = useState<string>(
    initialConfig?.max_risk_per_trade_pct != null ? String(initialConfig.max_risk_per_trade_pct) : "1.0",
  );
  const [model, setModel] = useState<string>(initialConfig?.model || "anthropic/claude-sonnet-5.5");

  // UX state
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: "ok" | "err" } | null>(null);
  // Last values the backend confirmed; a failed save rolls the toggle back to these.
  const [savedEnabled, setSavedEnabled] = useState<boolean>(Boolean(initialConfig?.enabled));
  const [savedRiskPct, setSavedRiskPct] = useState<string>(
    initialConfig?.max_risk_per_trade_pct != null ? String(initialConfig.max_risk_per_trade_pct) : "1.0",
  );
  const { confirm, dialog } = useConfirm();

  useEffect(() => {
    if (initialConfig) {
      setEnabled(Boolean(initialConfig.enabled));
      setSavedEnabled(Boolean(initialConfig.enabled));
      if (initialConfig.max_risk_per_trade_pct != null) {
        setRiskPct(String(initialConfig.max_risk_per_trade_pct));
        setSavedRiskPct(String(initialConfig.max_risk_per_trade_pct));
      }
      if (initialConfig.model) {
        setModel(initialConfig.model);
      }
    }
  }, [initialConfig]);

  async function handleSave(newEnabled?: boolean, newRiskPct?: string) {
    setSaving(true);
    setMessage(null);
    const targetEnabled = typeof newEnabled === "boolean" ? newEnabled : enabled;
    const targetRisk = parseFloat(newRiskPct ?? riskPct);
    const clampedRisk = isNaN(targetRisk) ? 1.0 : Math.min(1.0, Math.max(0.05, targetRisk));

    try {
      const res = await api.saveAutoDecideConfig({
        enabled: targetEnabled,
        max_risk_per_trade_pct: clampedRisk,
      });
      setEnabled(res.enabled);
      setSavedEnabled(res.enabled);
      setRiskPct(String(res.max_risk_per_trade_pct));
      setSavedRiskPct(String(res.max_risk_per_trade_pct));
      setMessage({
        text: `บันทึกการตั้งค่าเรียบร้อย: ${targetEnabled ? "เปิด AI Autotrade" : "ปิด AI (ผู้ใช้เทรดเอง + AI Guardrail)"} · ความเสี่ยง ${clampedRisk}% ของทุน`,
        type: "ok",
      });
      onRefresh?.();
    } catch (err) {
      // The backend kept its old state; show that, not the optimistic toggle.
      setEnabled(savedEnabled);
      setMessage({
        text: `เกิดข้อผิดพลาดในการบันทึก: ${err instanceof Error ? err.message : String(err)}`,
        type: "err",
      });
    } finally {
      setSaving(false);
    }
  }

  /** Mode switch saves only the mode; a half-typed risk % is not sent along with it. */
  async function selectMode(next: boolean) {
    if (saving || next === savedEnabled) {
      setEnabled(savedEnabled);
      return;
    }
    if (next) {
      const ok = await confirm({
        title: "เปิด AI Autotrade?",
        body: "Claude จะอนุมัติ/ปฏิเสธการ์ดใน Approval Queue และปิดไม้ตามสัญญาณ FLAT หรือ Stop โดยไม่รอคุณกด (paper เท่านั้น)",
        confirmLabel: "เปิด AI Autotrade",
      });
      if (!ok) return;
    }
    setEnabled(next);
    await handleSave(next, savedRiskPct);
  }

  async function handleRunNow() {
    setRunning(true);
    setMessage(null);
    try {
      const res = await api.runAutoDecide();
      if (res.skipped) {
        setMessage({
          text: `Auto-Decide ข้ามรอบนี้: ${resolveReasonLabel(res.reason || "skipped")}`,
          type: "ok",
        });
      } else if (res.ok) {
        setMessage({
          text: `รัน Auto-Decide สำเร็จ: อนุมัติ ${res.approved?.length ?? 0} · เลื่อน ${res.deferred?.length ?? 0} · ปฏิเสธ ${res.rejected?.length ?? 0}`,
          type: "ok",
        });
      } else {
        setMessage({
          text: `ผลการรัน: ${res.error || res.reason || "เสร็จสิ้น"}`,
          type: res.error ? "err" : "ok",
        });
      }
      onRefresh?.();
    } catch (err) {
      setMessage({
        text: `เกิดข้อผิดพลาดในการรัน: ${err instanceof Error ? err.message : String(err)}`,
        type: "err",
      });
    } finally {
      setRunning(false);
    }
  }

  async function handleResetLatch() {
    const ok = await confirm({
      title: "ปลดล็อกเกต EV?",
      body: "เกตจะกลับไปตรวจตัวอย่าง 30 วันตามกฎการเทรด ตัวอย่างที่บางจะไม่ปลดล็อกเอง และจะล็อกใหม่เมื่อ EV ต่ำกว่าเกณฑ์อีกครั้ง",
      confirmLabel: "ปลดล็อก",
      tone: "danger",
      reasonLabel: "เหตุผลที่ปลดล็อก (บันทึกไว้)",
    });
    if (typeof ok !== "string") return;
    setSaving(true);
    setMessage(null);
    try {
      await api.resetEvGate(ok);
      setMessage({ text: "ปลดล็อกเกต EV แล้ว", type: "ok" });
      onRefresh?.();
    } catch (err) {
      setMessage({
        text: `ปลดล็อกไม่สำเร็จ: ${err instanceof Error ? err.message : String(err)}`,
        type: "err",
      });
    } finally {
      setSaving(false);
    }
  }

  const counts = auto
    ? [
        ["อนุมัติ (Approved)", auto.approved?.length ?? 0, "text-emerald-400"],
        ["ปฏิเสธ (Rejected)", auto.rejected?.length ?? 0, "text-rose-400"],
        ["เลื่อน (Deferred)", auto.deferred?.length ?? 0, "text-amber-400"],
        ["กรองออก (Filtered)", auto.filtered_out?.length ?? 0, "text-slate-400"],
        ["ล้มเหลว (Failed)", auto.apply_failed?.length ?? 0, "text-rose-500"],
      ]
    : [];

  return (
    <Card className="border-border shadow-sm">
      {dialog}
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <span className="text-lg">🤖</span>
              <CardTitle className="text-base font-bold text-(--color-foreground)">
                Paper Auto-Decide &amp; Risk Control
              </CardTitle>
              <Badge variant={enabled ? "ok" : "muted"}>
                {enabled ? "⚡ AI Autotrade (ON)" : "🛡️ User Trade + Guardrail (OFF)"}
              </Badge>
            </div>
            <CardDescription className="text-xs text-(--color-muted)">
              ควบคุมระบบตัดสินใจอัตโนมัติ (Claude Sonnet 5.5) และเพดานความเสี่ยงที่จุดตัด ไม่ใช่ขนาดไม้ที่ใช้ (Paper Engine Only)
            </CardDescription>
          </div>
          <Button
            size="sm"
            variant="outline"
            disabled={running}
            onClick={() => void handleRunNow()}
            className="border-border text-xs font-mono h-8"
          >
            {running ? "กำลังรัน…" : "⚡ รัน Auto-Decide ตอนนี้"}
          </Button>
        </div>
      </CardHeader>

      <CardContent className="space-y-5 text-sm pt-2">
        {message ? (
          <div
            className={cn(
              "rounded-lg border px-3 py-2 text-xs flex items-center justify-between gap-2 animate-in fade-in duration-200",
              message.type === "ok"
                ? "border-emerald-500/40 bg-emerald-950/20 text-emerald-300"
                : "border-rose-500/40 bg-rose-950/20 text-rose-300",
            )}
          >
            <span>{message.text}</span>
            <button
              onClick={() => setMessage(null)}
              className="text-xs opacity-60 hover:opacity-100 ml-2"
            >
              ✕
            </button>
          </div>
        ) : null}

        {(() => {
          const copy = evGateOwnerCopy(gate);
          return (
            <div className="rounded-lg border border-border px-3 py-2 text-xs space-y-2">
              <div className="font-medium text-(--color-foreground)">{copy.title}</div>
              <p className="text-(--color-muted)">{copy.detail}</p>
              {gate?.latched ? (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={saving}
                  onClick={() => void handleResetLatch()}
                  className="h-8 text-xs"
                >
                  ปลดล็อกเกต EV
                </Button>
              ) : null}
            </div>
          );
        })()}

        {/* Mode Selector Toggle Cards */}
        <div className="grid gap-3 sm:grid-cols-2">
          {/* Option 1: On (AI Autotrade) */}
          <div
            role="radio"
            aria-checked={enabled}
            tabIndex={0}
            onClick={() => void selectMode(true)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                void selectMode(true);
              }
            }}
            className={cn(
              "relative flex flex-col justify-between rounded-xl border p-4 cursor-pointer transition-all duration-200",
              enabled
                ? "border-emerald-500 bg-emerald-950/20 shadow-md ring-1 ring-emerald-500/40"
                : "border-border bg-card/40 hover:border-slate-600 hover:bg-card/70 opacity-75 hover:opacity-100",
            )}
          >
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-base">⚡</span>
                  <span className="font-semibold text-sm text-(--color-foreground)">
                    On — AI Autotrade &amp; ปิดไม้อัตโนมัติ
                  </span>
                </div>
                <div
                  className={cn(
                    "h-4 w-4 rounded-full border flex items-center justify-center transition-colors",
                    enabled ? "border-emerald-500 bg-emerald-500" : "border-slate-600 bg-transparent",
                  )}
                >
                  {enabled ? <div className="h-1.5 w-1.5 rounded-full bg-black" /> : null}
                </div>
              </div>
              <p className="text-xs text-(--color-muted) leading-relaxed">
                เปิดระบบ AI สแกนสัญญาณและอนุมัติเปิดไม้จำลองอัตโนมัติ (<code className="text-emerald-400">auto:claude:approve</code>)
                พร้อมสั่งปิดไม้ทันทีเมื่อมีสัญญาณ FLAT (<code className="text-emerald-400">auto:rule:flat_exit</code>)
                หรือถึงจุด Stop / Profit Lock โดยไม่ต้องรอผู้ใช้กด
              </p>
            </div>
            {enabled ? (
              <div className="mt-3 flex items-center gap-1.5 text-[11px] font-mono text-emerald-400">
                <span>●</span>
                <span>สถานะ: ทำงานอัตโนมัติเต็มรูปแบบ (Fully Autonomous)</span>
              </div>
            ) : null}
          </div>

          {/* Option 2: Off (User Trade + AI Guardrail) */}
          <div
            role="radio"
            aria-checked={!enabled}
            tabIndex={0}
            onClick={() => void selectMode(false)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                void selectMode(false);
              }
            }}
            className={cn(
              "relative flex flex-col justify-between rounded-xl border p-4 cursor-pointer transition-all duration-200",
              !enabled
                ? "border-amber-500 bg-amber-950/20 shadow-md ring-1 ring-amber-500/40"
                : "border-border bg-card/40 hover:border-slate-600 hover:bg-card/70 opacity-75 hover:opacity-100",
            )}
          >
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-base">🛡️</span>
                  <span className="font-semibold text-sm text-(--color-foreground)">
                    Off — User Trade / ปิดไม้เอง (AI Guardrail Active)
                  </span>
                </div>
                <div
                  className={cn(
                    "h-4 w-4 rounded-full border flex items-center justify-center transition-colors",
                    !enabled ? "border-amber-500 bg-amber-500" : "border-slate-600 bg-transparent",
                  )}
                >
                  {!enabled ? <div className="h-1.5 w-1.5 rounded-full bg-black" /> : null}
                </div>
              </div>
              <p className="text-xs text-(--color-muted) leading-relaxed">
                ผู้ใช้เป็นผู้ตัดสินใจเปิดและปิดไม้เองด้วยมือ แต่ <strong className="text-amber-300">AI Risk Monitor จะคอยตรวจสอบความปลอดภัยตลอดเวลา</strong>:
                ช่วยตัดขาดทุน (<code className="text-amber-300">cut loss / stop_hit</code>) อัตโนมัติทันทีหากลืมทำ,
                ขยับจุดล็อคกำไร (Profit Lock), และตัดไฟฉุกเฉินหากขาดทุนเกินเพดาน
              </p>
            </div>
            <div className="mt-3 flex items-center gap-1.5 text-[11px] font-mono text-amber-400">
              <span>●</span>
              <span>สถานะ: ผู้ใช้เทรดเอง + AI ช่วยป้องกันความผิดพลาดและ Cut Loss</span>
            </div>
          </div>
        </div>

        {/* Risk Percentage Input Field */}
        <div className="rounded-xl border border-border bg-card/30 p-4 space-y-3">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div>
              <label className="text-xs font-semibold text-(--color-foreground) flex items-center gap-1.5">
                <span>🎯</span>
                <span>เพดานความเสี่ยงที่จุดตัด (% ของทุน) ไม่ใช่ขนาดไม้ที่ใช้</span>
              </label>
              <p className="text-xs text-(--color-muted) mt-0.5">
                ค่านี้คือเพดาน risk-at-stop ขนาดไม้มาจากสัดส่วน notional ของสัญญาณ ไม่ได้เสี่ยงเท่าเพดานทุกไม้
              </p>
            </div>
            <div className="flex items-center gap-2">
              <div className="relative w-28">
                <input
                  type="number"
                  step="0.05"
                  min="0.05"
                  max="1.0"
                  value={riskPct}
                  onChange={(e) => setRiskPct(e.target.value)}
                  className="w-full rounded-md border border-border bg-[#121923] px-3 py-1.5 pr-7 text-sm font-mono text-(--color-foreground) focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
                  placeholder="1.0"
                />
                <span className="absolute right-2.5 top-1/2 -translate-y-1/2 text-xs font-mono text-(--color-muted)">
                  %
                </span>
              </div>
              <Button
                size="sm"
                variant="ok"
                disabled={saving}
                onClick={() => void handleSave()}
                className="text-xs font-medium h-8 px-4"
              >
                {saving ? "บันทึก…" : "💾 บันทึก"}
              </Button>
            </div>
          </div>

          {/* Quick Preset Buttons & Safety Note */}
          <div className="flex flex-wrap items-center justify-between gap-2 pt-1 border-t border-border/50">
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] text-(--color-muted)">ค่าแนะนำ:</span>
              {[0.25, 0.5, 0.75, 1.0].map((val) => (
                <button
                  key={val}
                  type="button"
                  onClick={() => {
                    setRiskPct(String(val));
                    void handleSave(undefined, String(val));
                  }}
                  className={cn(
                    "rounded px-2 py-0.5 text-[11px] font-mono border transition-colors",
                    parseFloat(riskPct) === val
                      ? "border-emerald-500 bg-emerald-950/40 text-emerald-300 font-bold"
                      : "border-border bg-card/50 text-(--color-muted) hover:text-(--color-foreground) hover:border-slate-500",
                  )}
                >
                  {val.toFixed(2)}%
                </button>
              ))}
            </div>
            <div className="text-[11px] text-(--color-muted) flex items-center gap-1">
              <span>🛡️</span>
              <span>เพดานความปลอดภัยตามกฎการเทรด: จำกัดสูงสุดไม่เกิน <strong>1.0%</strong> เพื่อรักษาเงินทุน</span>
            </div>
          </div>
        </div>

        {/* Telemetry and Recent Auto-Decide Runs */}
        <div className="space-y-3 pt-2">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="font-semibold text-(--color-foreground)">สถิติรอบการตรวจสอบล่าสุด (Last Run Telemetry)</span>
            {auto ? (
              <span className="text-(--color-muted) font-mono">
                Model: {auto.model || model} · ตรวจสอบเมื่อ {relativeAge(auto.at_ms)}
              </span>
            ) : null}
          </div>

          {!auto ? (
            <p className="text-xs text-(--color-muted) italic">ยังไม่มีประวัติการรัน Auto-Decide บันทึกในระบบ</p>
          ) : auto.skipped ? (
            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card/30 p-3">
              <Badge variant={auto.reason === "auto_decide_disabled" ? "muted" : "danger"}>
                {auto.reason === "auto_decide_disabled" ? "ปิดใช้งาน (Off)" : "ข้าม (Skipped)"}
              </Badge>
              <span className="text-xs text-(--color-muted) font-mono">
                เหตุผล: {resolveReasonLabel(auto.reason)}
              </span>
            </div>
          ) : (
            <div className="space-y-3">
              {/* Counters Grid */}
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
                {counts.map(([label, n, colorClass]) => (
                  <div
                    key={String(label)}
                    className="rounded-lg border border-border bg-card/40 p-2.5 text-center"
                  >
                    <div className="text-[11px] text-(--color-muted) truncate">{label}</div>
                    <div className={cn("text-lg font-bold font-mono tabular-nums mt-0.5", colorClass)}>{n}</div>
                  </div>
                ))}
              </div>

              {/* Deferred List */}
              {auto.deferred?.length ? (
                <div className="rounded-lg border border-border bg-card/20 p-3 space-y-2">
                  <div className="text-xs font-medium text-(--color-muted)">
                    รายการที่เลื่อนการตัดสินใจ ({auto.deferred.length} รายการ):
                  </div>
                  <ul className="space-y-1.5 text-xs font-mono">
                    {auto.deferred.slice(0, 5).map((d) => (
                      <li key={d.id} className="flex items-center justify-between text-slate-300">
                        <span className="font-bold text-amber-400">{d.symbol}</span>
                        <span className="text-(--color-muted) text-[11px]">{resolveReasonLabel(d.reason)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {/* Apply Failed List */}
              {auto.apply_failed?.length ? (
                <div className="rounded-lg border border-rose-500/30 bg-rose-950/20 p-3 space-y-2">
                  <div className="text-xs font-medium text-rose-400">รายการที่ดำเนินการล้มเหลว:</div>
                  <ul className="space-y-1 text-xs font-mono text-rose-300">
                    {auto.apply_failed.slice(0, 5).map((f) => (
                      <li key={f.id} className="flex items-center justify-between">
                        <span>{f.symbol}</span>
                        <span className="text-rose-400">{f.error}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
