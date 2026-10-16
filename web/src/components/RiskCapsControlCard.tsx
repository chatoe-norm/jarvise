import { useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type RiskCapsPayload } from "@/lib/api";
import { capsToRaw, validateRiskCaps, type RiskCapField } from "@/lib/riskCaps";

interface RiskCapsControlCardProps {
  initialCaps?: Record<string, unknown> | null;
  onRefresh?: () => void;
}

const DEFAULT_CAPS: RiskCapsPayload = {
  max_notional_per_order: 2000.0,
  max_daily_loss_usd: 100.0,
  drawdown_lock_pct: 5.0,
  max_open_positions: 2,
  max_gross_notional_pct: 10.0,
  max_symbol_notional_pct: 6.0,
  max_correlated_bucket_pct: 10.0,
  max_risk_per_trade_pct: 1.0,
};

export function RiskCapsControlCard({ initialCaps, onRefresh }: RiskCapsControlCardProps) {
  // Inputs hold raw text so a cleared field is an error, never a silent 0.
  const [raw, setRaw] = useState<Record<RiskCapField, string>>(() => capsToRaw(DEFAULT_CAPS));
  const [savedRaw, setSavedRaw] = useState<Record<RiskCapField, string>>(() => capsToRaw(DEFAULT_CAPS));
  // Until the server caps arrive the card shows defaults; saving those would overwrite real caps.
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<{ type: "ok" | "err"; msg: string } | null>(null);
  const isDirty = JSON.stringify(raw) !== JSON.stringify(savedRaw);
  const dirtyRef = useRef(isDirty);
  dirtyRef.current = isDirty;
  const { caps: validCaps, errors } = validateRiskCaps(raw);
  const caps = raw;

  useEffect(() => {
    if (initialCaps) {
      const merged: RiskCapsPayload = {
        max_notional_per_order: Number(initialCaps.max_notional_per_order ?? DEFAULT_CAPS.max_notional_per_order),
        max_daily_loss_usd: Number(initialCaps.max_daily_loss_usd ?? DEFAULT_CAPS.max_daily_loss_usd),
        drawdown_lock_pct: Number(initialCaps.drawdown_lock_pct ?? DEFAULT_CAPS.drawdown_lock_pct),
        max_open_positions: Number(initialCaps.max_open_positions ?? DEFAULT_CAPS.max_open_positions),
        max_gross_notional_pct: Number(initialCaps.max_gross_notional_pct ?? DEFAULT_CAPS.max_gross_notional_pct),
        max_symbol_notional_pct: Number(initialCaps.max_symbol_notional_pct ?? DEFAULT_CAPS.max_symbol_notional_pct),
        max_correlated_bucket_pct: Number(initialCaps.max_correlated_bucket_pct ?? DEFAULT_CAPS.max_correlated_bucket_pct),
        max_risk_per_trade_pct: Number(initialCaps.max_risk_per_trade_pct ?? DEFAULT_CAPS.max_risk_per_trade_pct),
      };
      const next = capsToRaw(merged);
      setSavedRaw(next);
      // A background refresh must not wipe edits the owner has not saved yet.
      if (!dirtyRef.current) setRaw(next);
      setLoaded(true);
    }
  }, [initialCaps]);

  const handleFieldChange = (field: RiskCapField, value: string) => {
    setRaw((prev) => ({ ...prev, [field]: value }));
    setFeedback(null);
  };

  const setCaps = (next: RiskCapsPayload) => setRaw(capsToRaw(next));

  const applyPreset = (preset: "default" | "liquid" | "conservative") => {
    if (preset === "default") {
      setCaps(DEFAULT_CAPS);
    } else if (preset === "liquid") {
      setCaps({
        max_notional_per_order: 2500.0,
        max_daily_loss_usd: 150.0,
        drawdown_lock_pct: 5.0,
        max_open_positions: 4,
        max_gross_notional_pct: 25.0,
        max_symbol_notional_pct: 10.0,
        max_correlated_bucket_pct: 20.0,
        max_risk_per_trade_pct: 1.0,
      });
    } else if (preset === "conservative") {
      setCaps({
        max_notional_per_order: 1000.0,
        max_daily_loss_usd: 50.0,
        drawdown_lock_pct: 3.0,
        max_open_positions: 1,
        max_gross_notional_pct: 5.0,
        max_symbol_notional_pct: 5.0,
        max_correlated_bucket_pct: 5.0,
        max_risk_per_trade_pct: 0.5,
      });
    }
    setFeedback(null);
  };

  const handleSave = async () => {
    if (!validCaps || !loaded) return;
    setSaving(true);
    setFeedback(null);
    try {
      const res = await api.saveRiskCaps(validCaps);
      if (res.ok && res.risk_caps) {
        const next = capsToRaw(res.risk_caps);
        setSavedRaw(next);
        setRaw(next);
        setFeedback({ type: "ok", msg: "บันทึกการตั้งค่า Risk Caps สำเร็จ และซิงค์ลง Redis/Engine เรียบร้อย" });
        onRefresh?.();
      } else {
        setFeedback({ type: "err", msg: "บันทึกไม่สำเร็จ กรุณาลองใหม่อีกครั้ง" });
      }
    } catch (err) {
      setFeedback({ type: "err", msg: err instanceof Error ? err.message : String(err) });
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setRaw(savedRaw);
    setFeedback(null);
  };

  const fieldError = (field: RiskCapField) =>
    errors[field] ? <p className="text-[11px] font-semibold text-rose-400">{errors[field]}</p> : null;
  const hasErrors = Object.keys(errors).length > 0;

  return (
    <Card className="border-[var(--color-border)]">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2">
              <span>Risk Caps &amp; Guardrails Control</span>
              <span className="inline-block h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]" />
            </CardTitle>
            <CardDescription>
              Hard limits enforced by the engine across both paper and live execution (Fail-closed protected).
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            {isDirty && (
              <Badge variant="outline" className="animate-pulse text-amber-400 border-amber-500/40 bg-amber-950/20">
                Unsaved Changes
              </Badge>
            )}
            <Badge variant={loaded ? "ok" : "outline"}>{loaded ? "Guardrails Active" : "กำลังโหลดค่าจริง…"}</Badge>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-5">
        {/* Presets Bar */}
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/40 p-2.5 text-xs">
          <div className="flex items-center gap-1.5 text-[var(--color-muted)] font-medium">
            <span>⚡ โหลด Preset:</span>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => applyPreset("default")}
              className="h-11 sm:h-7 text-xs font-mono"
            >
              Paper Core (2 Pairs)
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => applyPreset("liquid")}
              className="h-11 sm:h-7 text-xs font-mono"
            >
              Liquid 4-Pair
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => applyPreset("conservative")}
              className="h-11 sm:h-7 text-xs font-mono"
            >
              Conservative Defense
            </Button>
          </div>
        </div>

        {/* Input Controls Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-sm">
          {/* Max Notional per Order */}
          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3 space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="cap_notional" className="text-xs font-mono text-[var(--color-muted)]">
                max_notional_per_order
              </label>
              <span className="text-[10px] text-[var(--color-muted)]">USD</span>
            </div>
            <input
              id="cap_notional"
              type="number"
              step="100"
              min="0.01"
              value={caps.max_notional_per_order}
              onChange={(e) => handleFieldChange("max_notional_per_order", e.target.value)}
              className="w-full rounded-md border border-[var(--color-border)] bg-[#11161f] px-2.5 py-1.5 font-mono text-sm text-[var(--color-foreground)] focus:outline-none focus:ring-1 focus:ring-emerald-500"
            />
            {fieldError("max_notional_per_order")}
            <p className="text-[11px] text-[var(--color-muted)]">เพดาน Notional สูงสุดต่อ 1 ไม้</p>
          </div>

          {/* Max Daily Loss USD */}
          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3 space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="cap_daily_loss" className="text-xs font-mono text-[var(--color-muted)]">
                max_daily_loss_usd
              </label>
              <span className="text-[10px] text-[var(--color-muted)]">USD</span>
            </div>
            <input
              id="cap_daily_loss"
              type="number"
              step="10"
              min="0.01"
              value={caps.max_daily_loss_usd}
              onChange={(e) => handleFieldChange("max_daily_loss_usd", e.target.value)}
              className="w-full rounded-md border border-[var(--color-border)] bg-[#11161f] px-2.5 py-1.5 font-mono text-sm text-[var(--color-foreground)] focus:outline-none focus:ring-1 focus:ring-emerald-500"
            />
            {fieldError("max_daily_loss_usd")}
            <p className="text-[11px] text-[var(--color-muted)]">ขาดทุนรายวันสูงสุดก่อนสั่งหยุด</p>
          </div>

          {/* Drawdown Lock Pct */}
          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3 space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="cap_drawdown" className="text-xs font-mono text-[var(--color-muted)]">
                drawdown_lock_pct
              </label>
              <span className="text-[10px] text-[var(--color-muted)]">%</span>
            </div>
            <input
              id="cap_drawdown"
              type="number"
              step="0.5"
              min="0.01"
              max="100"
              value={caps.drawdown_lock_pct}
              onChange={(e) => handleFieldChange("drawdown_lock_pct", e.target.value)}
              className="w-full rounded-md border border-[var(--color-border)] bg-[#11161f] px-2.5 py-1.5 font-mono text-sm text-[var(--color-foreground)] focus:outline-none focus:ring-1 focus:ring-emerald-500"
            />
            {fieldError("drawdown_lock_pct")}
            <p className="text-[11px] text-[var(--color-muted)]">Drawdown รวมเทียบทุนเริ่มต้น</p>
          </div>

          {/* Max Open Positions */}
          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3 space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="cap_positions" className="text-xs font-mono text-[var(--color-muted)]">
                max_open_positions
              </label>
              <span className="text-[10px] text-[var(--color-muted)]">จำนวน</span>
            </div>
            <input
              id="cap_positions"
              type="number"
              step="1"
              min="1"
              max="20"
              value={caps.max_open_positions}
              onChange={(e) => handleFieldChange("max_open_positions", e.target.value)}
              className="w-full rounded-md border border-[var(--color-border)] bg-[#11161f] px-2.5 py-1.5 font-mono text-sm text-[var(--color-foreground)] focus:outline-none focus:ring-1 focus:ring-emerald-500"
            />
            {fieldError("max_open_positions")}
            <p className="text-[11px] text-[var(--color-muted)]">จำนวน Position พร้อมกันสูงสุด</p>
          </div>

          {/* Max Gross Notional Pct */}
          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3 space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="cap_gross" className="text-xs font-mono text-[var(--color-muted)]">
                max_gross_notional_pct
              </label>
              <span className="text-[10px] text-[var(--color-muted)]">%</span>
            </div>
            <input
              id="cap_gross"
              type="number"
              step="1"
              min="0.01"
              max="100"
              value={caps.max_gross_notional_pct}
              onChange={(e) => handleFieldChange("max_gross_notional_pct", e.target.value)}
              className="w-full rounded-md border border-[var(--color-border)] bg-[#11161f] px-2.5 py-1.5 font-mono text-sm text-[var(--color-foreground)] focus:outline-none focus:ring-1 focus:ring-emerald-500"
            />
            {fieldError("max_gross_notional_pct")}
            <p className="text-[11px] text-[var(--color-muted)]">รวม Notional ทุกตัวเทียบทุน</p>
          </div>

          {/* Max Symbol Notional Pct */}
          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3 space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="cap_symbol" className="text-xs font-mono text-[var(--color-muted)]">
                max_symbol_notional_pct
              </label>
              <span className="text-[10px] text-[var(--color-muted)]">%</span>
            </div>
            <input
              id="cap_symbol"
              type="number"
              step="1"
              min="0.01"
              max="100"
              value={caps.max_symbol_notional_pct}
              onChange={(e) => handleFieldChange("max_symbol_notional_pct", e.target.value)}
              className="w-full rounded-md border border-[var(--color-border)] bg-[#11161f] px-2.5 py-1.5 font-mono text-sm text-[var(--color-foreground)] focus:outline-none focus:ring-1 focus:ring-emerald-500"
            />
            {fieldError("max_symbol_notional_pct")}
            <p className="text-[11px] text-[var(--color-muted)]">Notional ต่อ 1 เหรียญเทียบทุน</p>
          </div>

          {/* Max Correlated Bucket Pct */}
          <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3 space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="cap_bucket" className="text-xs font-mono text-[var(--color-muted)]">
                max_correlated_bucket_pct
              </label>
              <span className="text-[10px] text-[var(--color-muted)]">%</span>
            </div>
            <input
              id="cap_bucket"
              type="number"
              step="1"
              min="0.01"
              max="100"
              value={caps.max_correlated_bucket_pct}
              onChange={(e) => handleFieldChange("max_correlated_bucket_pct", e.target.value)}
              className="w-full rounded-md border border-[var(--color-border)] bg-[#11161f] px-2.5 py-1.5 font-mono text-sm text-[var(--color-foreground)] focus:outline-none focus:ring-1 focus:ring-emerald-500"
            />
            {fieldError("max_correlated_bucket_pct")}
            <p className="text-[11px] text-[var(--color-muted)]">กลุ่มเหรียญสัมพันธ์ (BTC+ETH)</p>
          </div>

          {/* Max Risk per Trade Pct */}
          <div className="rounded-lg border border-amber-500/30 bg-amber-950/10 p-3 space-y-1.5">
            <div className="flex items-center justify-between">
              <label htmlFor="cap_risk_trade" className="text-xs font-mono text-amber-300">
                max_risk_per_trade_pct
              </label>
              <span className="text-[10px] text-amber-400 font-bold">Ceiling 1.0%</span>
            </div>
            <input
              id="cap_risk_trade"
              type="number"
              step="0.05"
              min="0.01"
              max="1.0"
              value={caps.max_risk_per_trade_pct}
              onChange={(e) => handleFieldChange("max_risk_per_trade_pct", e.target.value)}
              className="w-full rounded-md border border-amber-500/40 bg-[#11161f] px-2.5 py-1.5 font-mono text-sm text-amber-200 focus:outline-none focus:ring-1 focus:ring-amber-500"
            />
            {fieldError("max_risk_per_trade_pct")}
            <p className="text-[11px] text-amber-400/80">ความเสี่ยงต่อไม้ (ล็อกเพดานที่ 1.0%)</p>
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

        {/* Action Controls */}
        <div className="flex flex-wrap items-center justify-between gap-3 pt-1 border-t border-[var(--color-border)]">
          <div className="text-xs text-[var(--color-muted)]">
            <span>⚙️ การเปลี่ยนแปลงจะมีผลทันทีต่อ Engine และการเปิดคำสั่งถัดไป</span>
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!isDirty || saving}
              onClick={handleReset}
              className="text-xs font-mono"
            >
              ↺ Reset
            </Button>
            <Button
              type="button"
              variant={isDirty && !hasErrors ? "ok" : "outline"}
              size="sm"
              disabled={!isDirty || saving || hasErrors || !loaded}
              onClick={() => void handleSave()}
              className="text-xs font-mono font-semibold h-11 sm:h-8"
            >
              {saving ? "กำลังบันทึก…" : hasErrors ? "แก้ช่องที่ผิดก่อนบันทึก" : "💾 บันทึก Risk Caps"}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
