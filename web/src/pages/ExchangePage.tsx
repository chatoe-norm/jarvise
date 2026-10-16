import { useEffect, useState } from "react";
import { UsdBars } from "@/components/Charts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type ExchangePayload } from "@/lib/api";
import { cn, formatNum, formatQty, relativeAge } from "@/lib/utils";

function UsdCell({
  usd,
  pricing,
}: {
  usd: number | null;
  pricing?: string | null;
}) {
  if (usd != null) {
    return (
      <span className="inline-flex flex-wrap items-center gap-2">
        <span className="tabular-nums">{formatNum(usd)}</span>
        {pricing?.startsWith("earn_") ? (
          <Badge variant="muted">Earn</Badge>
        ) : null}
      </span>
    );
  }
  return <Badge variant="muted">unpriced · no USDT market</Badge>;
}

function LiveTradingGuideCard() {
  const [activeTab, setActiveTab] = useState<"settings" | "steps" | "guardrails">("settings");
  const [copied, setCopied] = useState(false);

  const envTemplate = `# ==============================================================================
# JARVISE LIVE TRADING CONFIGURATION (/opt/jarvise/.env)
# ==============================================================================

# 1. Master Live Switch (เปิดโหมดส่งคำสั่งซื้อขายจริงบน Exchange)
JARVISE_LIVE_TRADING=true

# 2. Capital Sizing for Live (ทุนอ้างอิงสำหรับคำนวณ Position Sizing จริง - แยกจาก Paper)
JARVISE_LIVE_EQUITY_USD=10000

# 3. Binance Trade API Keys (ต้องเป็น Sub-Account เท่านั้น)
# สิทธิ์: ติ๊ก "Enable Spot & Margin Trading" เท่านั้น | ห้ามติ๊ก "Withdrawals" เด็ดขาด
BINANCE_TRADE_API_KEY=your_binance_subaccount_trade_api_key
BINANCE_TRADE_API_SECRET=your_binance_subaccount_trade_api_secret
# หากใช้ Ed25519 ให้ใช้ตัวแปรนี้แทน Secret:
# BINANCE_TRADE_PRIVATE_KEY_PATH=/opt/jarvise/keys/trade-ed25519.pem

# 4. Mandatory Web Basic Authentication (ระบบความปลอดภัยบังคับเมื่อเปิด Live Mode)
WEB_BASIC_AUTH_USER=admin
WEB_BASIC_AUTH_PASSWORD=your_super_strong_password`;

  function copyEnv() {
    void navigator.clipboard.writeText(envTemplate);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <Card className="border-[var(--color-border)] shadow-sm">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <span className="text-lg">📖</span>
              <CardTitle className="text-base font-bold text-[var(--color-foreground)]">
                คู่มือการปรับเป็น Live Trading (เทรดจริงบน Binance Spot)
              </CardTitle>
              <Badge variant="outline" className="font-mono text-[10px]">
                docs/ops/live-enable-checklist.md
              </Badge>
            </div>
            <CardDescription className="text-xs text-[var(--color-muted)]">
              รายละเอียดตัวแปร Settings ที่ต้องมีบน VPS ขั้นตอน Audit สิทธิ์ความปลอดภัย และเงื่อนไขการส่งคำสั่งจริง
            </CardDescription>
          </div>
          <div className="flex items-center gap-1.5 bg-[var(--color-card)]/60 border border-[var(--color-border)] rounded-lg p-1">
            <button
              onClick={() => setActiveTab("settings")}
              className={cn(
                "px-2.5 py-1 rounded text-xs font-medium transition-colors",
                activeTab === "settings"
                  ? "bg-emerald-950/60 text-emerald-300 border border-emerald-500/40"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              ⚙️ การตั้งค่า (.env)
            </button>
            <button
              onClick={() => setActiveTab("steps")}
              className={cn(
                "px-2.5 py-1 rounded text-xs font-medium transition-colors",
                activeTab === "steps"
                  ? "bg-emerald-950/60 text-emerald-300 border border-emerald-500/40"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              📋 ขั้นตอนปฏิบัติ 5 ข้อ
            </button>
            <button
              onClick={() => setActiveTab("guardrails")}
              className={cn(
                "px-2.5 py-1 rounded text-xs font-medium transition-colors",
                activeTab === "guardrails"
                  ? "bg-emerald-950/60 text-emerald-300 border border-emerald-500/40"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              🛡️ กลไกความปลอดภัย
            </button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-4 text-xs pt-1">
        {/* Tab 1: Settings (.env) */}
        {activeTab === "settings" ? (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="font-semibold text-slate-300">
                ตัวแปรที่ต้องตั้งค่าในไฟล์ <code className="bg-[#121923] text-emerald-400 px-1 py-0.5 rounded">/opt/jarvise/.env</code> บน VPS:
              </span>
              <Button
                size="sm"
                variant="outline"
                onClick={copyEnv}
                className="h-7 text-[11px] font-mono border-[var(--color-border)]"
              >
                {copied ? "✓ คัดลอกแล้ว" : "📋 คัดลอก .env"}
              </Button>
            </div>

            <pre className="overflow-x-auto rounded-lg bg-[#121923] p-3.5 text-xs text-slate-300 font-mono leading-relaxed border border-[var(--color-border)]">
              {envTemplate}
            </pre>

            <div className="grid gap-2 sm:grid-cols-2 pt-1 text-[11px] text-[var(--color-muted)]">
              <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/40 p-2.5 space-y-1">
                <span className="font-semibold text-slate-200">🔒 การแยกความเสี่ยง (Capital Isolation):</span>
                <p>ห้ามใช้ Main Account โดยเด็ดขาด ต้องใช้ Binance Sub-Account ที่โอนเงินเข้าไปเฉพาะจำนวนที่ยอมรับความเสี่ยงได้</p>
              </div>
              <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/40 p-2.5 space-y-1">
                <span className="font-semibold text-slate-200">🛑 สิทธิ์ถอนเงิน (Withdrawal Ban):</span>
                <p>ระบบตรวจจับสิทธิ์ถอนเงินแบบ Fail-Closed หากตรวจพบสิทธิ์ถอนเงิน Jarvise จะปฏิเสธการเริ่มทำงานทันที</p>
              </div>
            </div>
          </div>
        ) : null}

        {/* Tab 2: 5 Steps */}
        {activeTab === "steps" ? (
          <div className="space-y-3">
            <div className="flex items-start gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-500/20 text-[11px] font-bold text-emerald-400">1</span>
              <div className="space-y-1">
                <div className="font-medium text-slate-200">สร้าง Binance Sub-Account แยกสำหรับ Jarvise</div>
                <p className="text-[var(--color-muted)]">
                  ไปที่ Binance → Sub-accounts → สร้าง Sub-Account ชื่อเฉพาะ (เช่น Jarvise-Live) และโอนเงินเท่าที่ต้องการใช้เทรดจริงเท่านั้น (เช่น $500 - $2,000)
                </p>
              </div>
            </div>

            <div className="flex items-start gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-500/20 text-[11px] font-bold text-emerald-400">2</span>
              <div className="space-y-1">
                <div className="font-medium text-slate-200">สร้าง API Key และจำกัดสิทธิ์ความปลอดภัย</div>
                <p className="text-[var(--color-muted)]">
                  ใน Sub-Account ให้สร้าง API Key: ติ๊กเฉพาะ <strong>Enable Spot &amp; Margin Trading</strong> เท่านั้น!
                  <strong>ห้ามติ๊ก Withdrawals, Universal Transfer หรือ Futures</strong> เด็ดขาด และระบุ IP Access Restriction เป็น IP VPS: <code className="bg-[#121923] text-emerald-400 px-1 py-0.5 rounded">76.13.218.48</code>
                </p>
              </div>
            </div>

            <div className="flex items-start gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-500/20 text-[11px] font-bold text-emerald-400">3</span>
              <div className="space-y-1">
                <div className="font-medium text-slate-200">ใส่คีย์ลงในไฟล์ /opt/jarvise/.env บน VPS</div>
                <p className="text-[var(--color-muted)]">
                  SSH เข้า Host VPS และนำ API Key, Secret, พร้อมทั้งตั้งค่า <code className="bg-[#121923] text-emerald-400 px-1 py-0.5 rounded">JARVISE_LIVE_TRADING=true</code> ลงใน <code className="bg-[#121923] text-slate-300 px-1 py-0.5 rounded">/opt/jarvise/.env</code>
                </p>
              </div>
            </div>

            <div className="flex items-start gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-500/20 text-[11px] font-bold text-emerald-400">4</span>
              <div className="space-y-1">
                <div className="font-medium text-slate-200">รัน Pre-flight Audit บน VPS ตรวจสอบสิทธิ์คีย์</div>
                <p className="text-[var(--color-muted)]">
                  รันคำสั่งตรวจสอบ:
                </p>
                <code className="block rounded bg-[#121923] p-2 font-mono text-emerald-300 text-[11px]">
                  docker compose exec jobs jarvise exchange check-key --trade --json
                </code>
                <p className="text-[var(--color-muted)] text-[11px]">
                  ผลลัพธ์ต้องแสดง <span className="text-emerald-400">ok_for_trade: true</span> และ <span className="text-emerald-400">enableWithdrawals: false</span> (หาก Exit code 3 หมายถึงไม่ปลอดภัย ห้ามเปิดระบบ)
                </p>
              </div>
            </div>

            <div className="flex items-start gap-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-500/20 text-[11px] font-bold text-emerald-400">5</span>
              <div className="space-y-1">
                <div className="font-medium text-slate-200">รีสตาร์ทบริการเพื่อเริ่มใช้งาน Live</div>
                <code className="block rounded bg-[#121923] p-2 font-mono text-emerald-300 text-[11px]">
                  docker compose up -d --force-recreate jobs web
                </code>
                <p className="text-[var(--color-muted)]">
                  ตรวจสอบหน้า <code className="text-slate-300">/healthz</code> จะพบ <span className="text-emerald-400">live_trading: true</span> และระบบพร้อมส่งคำสั่งเมื่อได้รับการ Approve
                </p>
              </div>
            </div>
          </div>
        ) : null}

        {/* Tab 3: Guardrails */}
        {activeTab === "guardrails" ? (
          <div className="space-y-3">
            <div className="rounded-lg border border-emerald-500/30 bg-emerald-950/20 p-3.5 space-y-1.5">
              <div className="font-semibold text-emerald-300 flex items-center gap-1.5">
                <span>🛡️</span>
                <span>Human Approval Gate (Two-Man Rule)</span>
              </div>
              <p className="text-[var(--color-muted)] leading-relaxed">
                แม้เปิด Live Trading แล้ว ระบบจะไม่ยิงออเดอร์โดยพลการ (P5 Autonomy is gated off)
                ทุกสัญญาณที่ผ่านการวิเคราะห์จะถูกส่งมายังคิวรออนุมัติ และจะส่งคำสั่งเข้า Binance จริงต่อเมื่อผู้ใช้กด <strong>Approve</strong> บนหน้า Dashboard เท่านั้น
              </p>
            </div>

            <div className="rounded-lg border border-blue-500/30 bg-blue-950/20 p-3.5 space-y-1.5">
              <div className="font-semibold text-blue-300 flex items-center gap-1.5">
                <span>🛑</span>
                <span>Automatic Venue Stop-Loss Limit Orders</span>
              </div>
              <p className="text-[var(--color-muted)] leading-relaxed">
                ทุกคำสั่งซื้อ (BUY) ที่แมตช์สำเร็จบน Binance ระบบจะส่งคำสั่งตั้ง Stop Loss Limit ไปยัง Exchange ทันทีโดยอัตโนมัติที่ระดับ Invalidation Price เพื่อป้องกันความเสี่ยงแม้ในกรณีเซิร์ฟเวอร์หลุดการเชื่อมต่อ
              </p>
            </div>

            <div className="rounded-lg border border-rose-500/30 bg-rose-950/20 p-3.5 space-y-1.5">
              <div className="font-semibold text-rose-300 flex items-center gap-1.5">
                <span>⚡</span>
                <span>15-Minute Risk Monitor &amp; Emergency Kill-Switch Flatten</span>
              </div>
              <p className="text-[var(--color-muted)] leading-relaxed">
                งาน Risk Monitor ทำงานทุก 15 นาที หากการขาดทุนรายวันเกินเพดาน ($100 / Max Daily Loss)
                ระบบจะสั่ง Engage Kill-Switch ยกเลิกออเดอร์รออนุมัติทั้งหมด และสั่งขายตัดขาดทุนแบบ Market Sell ทันทีอัตโนมัติ (<code className="text-rose-400">jarvise trade flatten</code>)
              </p>
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function ExchangePage() {
  const [data, setData] = useState<ExchangePayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setData(await api.exchange());
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
  }, []);

  const bars =
    data?.balances
      ?.filter((b) => b.usd != null && b.usd > 0)
      .map((b) => ({ asset: b.asset, usd: Number(b.usd) })) || [];
  const unpriced = data?.unpriced_count ?? 0;
  const balanceCount = data?.balances?.length ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Exchange</h1>
        <p className="text-sm text-[var(--color-muted)]">
          Read-only spot balances. Keys missing → panel stays empty (soft-fail).
        </p>
      </div>

      {error ? <p className="text-sm text-[var(--color-danger)]">{error}</p> : null}

      {!data ? (
        <p className="text-sm text-[var(--color-muted)]">Loading…</p>
      ) : !data.available ? (
        <div className="space-y-6">
          <Card className="border-[var(--color-border)]">
            <CardHeader>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <div className="h-2.5 w-2.5 rounded-full bg-amber-400" />
                  <CardTitle>Venue Gateway: Binance Spot</CardTitle>
                </div>
                <Badge variant="ok">Paper Shield Active</Badge>
              </div>
              <CardDescription>
                Tier 2 Exchange Adapter · Direct read-only spot balance inspection &amp; gated live execution
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4 text-sm">
              <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/50 p-4">
                <div className="flex items-start gap-3">
                  <div className="mt-0.5 rounded-full bg-amber-400/15 p-1.5 text-amber-400">
                    <span className="block h-2 w-2 rounded-full bg-amber-400" />
                  </div>
                  <div className="space-y-1">
                    <p className="font-medium text-[var(--color-foreground)]">
                      Exchange Keys Not Configured (Paper-First Mode)
                    </p>
                    <p className="text-xs text-[var(--color-muted)] leading-relaxed">
                      Jarvise is running strictly in local simulated paper mode. Real exchange API keys have not been loaded into the host environment, protecting live capital from unauthorized execution.
                    </p>
                  </div>
                </div>
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <div className="rounded-md border border-[var(--color-border)] p-3">
                  <div className="text-xs text-[var(--color-muted)]">Live Trading Flag</div>
                  <div className="mt-1 font-mono font-medium text-rose-400">Gated (False)</div>
                  <div className="mt-0.5 text-[11px] text-[var(--color-muted)]">Live orders blocked</div>
                </div>
                <div className="rounded-md border border-[var(--color-border)] p-3">
                  <div className="text-xs text-[var(--color-muted)]">Host VPS IP</div>
                  <div className="mt-1 font-mono font-medium text-[var(--color-foreground)]">76.13.218.48</div>
                  <div className="mt-0.5 text-[11px] text-[var(--color-muted)]">Whitelisted static IPv4</div>
                </div>
                <div className="rounded-md border border-[var(--color-border)] p-3">
                  <div className="text-xs text-[var(--color-muted)]">Execution Guardrail</div>
                  <div className="mt-1 font-mono font-medium text-[var(--color-foreground)]">Spot Only · Max $2k</div>
                  <div className="mt-0.5 text-[11px] text-[var(--color-muted)]">No leverage / futures orders</div>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card className="border-[var(--color-border)]">
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle>Live Enable Readiness Checklist</CardTitle>
                <Badge variant="outline">docs/ops/live-enable-checklist.md</Badge>
              </div>
              <CardDescription>
                Requirements that must be satisfied before enabling live spot execution
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-3 text-sm">
                <div className="flex items-start gap-3 rounded-md border border-[var(--color-border)]/60 p-3">
                  <span className="mt-0.5 text-xs font-mono font-bold text-amber-400">01</span>
                  <div className="flex-1 space-y-1">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-[var(--color-foreground)]">Prove Paper Trade Expectancy (EV &gt; 0)</span>
                      <Badge variant="muted">In Progress (N=10)</Badge>
                    </div>
                    <p className="text-xs text-[var(--color-muted)]">
                      Closed sample must reach ~8+ auto trades with positive expectancy. Current EV: -$0.65 across 10 closed auto rounds.
                    </p>
                  </div>
                </div>

                <div className="flex items-start gap-3 rounded-md border border-[var(--color-border)]/60 p-3">
                  <span className="mt-0.5 text-xs font-mono font-bold text-[var(--color-muted)]">02</span>
                  <div className="flex-1 space-y-1">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-[var(--color-foreground)]">Read-Only Key Permission Probe</span>
                      <Badge variant="outline">Pending Setup</Badge>
                    </div>
                    <p className="text-xs text-[var(--color-muted)]">
                      Probe Binance API key scopes. System strictly refuses and alarms if withdrawal or transfer permissions are detected.
                    </p>
                  </div>
                </div>

                <div className="flex items-start gap-3 rounded-md border border-[var(--color-border)]/60 p-3">
                  <span className="mt-0.5 text-xs font-mono font-bold text-[var(--color-muted)]">03</span>
                  <div className="flex-1 space-y-1">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-[var(--color-foreground)]">Human Approval Gate (Two-Man Rule)</span>
                      <Badge variant="ok">Built &amp; Verified</Badge>
                    </div>
                    <p className="text-xs text-[var(--color-muted)]">
                      Even with live enabled, orders never place autonomously (P5). Each trade requires explicit manual Approve on Home.
                    </p>
                  </div>
                </div>

                <div className="flex items-start gap-3 rounded-md border border-[var(--color-border)]/60 p-3">
                  <span className="mt-0.5 text-xs font-mono font-bold text-[var(--color-muted)]">04</span>
                  <div className="flex-1 space-y-1">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-[var(--color-foreground)]">Emergency Kill-Switch Flatten</span>
                      <Badge variant="ok">Operational</Badge>
                    </div>
                    <p className="text-xs text-[var(--color-muted)]">
                      Daily loss cap ($100) automatically triggers Redis kill switch, cancels all stops, and closes live spot exposure.
                    </p>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>

          <LiveTradingGuideCard />
        </div>
      ) : (
        <>
          <LiveTradingGuideCard />
          <Card>
            <CardHeader>
              <CardTitle>Spot ~USD</CardTitle>
              <CardDescription>
                {data.venue} · fetched {relativeAge(data.fetched_at_ms)} · total{" "}
                {formatNum(data.total_usd)}
                {unpriced > 0 ? ` · ${unpriced} unpriced` : ""}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <UsdBars
                rows={bars}
                emptyMessage={
                  balanceCount > 0 && unpriced > 0
                    ? `All ${unpriced} balances unpriced (dust / no USDT market on ${data.venue || "binance"})`
                    : "No priced balances."
                }
              />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Balances</CardTitle>
            </CardHeader>
            <CardContent>
              {(data.balances || []).length === 0 ? (
                <p className="text-sm text-[var(--color-muted)]">No non-zero assets</p>
              ) : (
                <div className="overflow-x-auto -mx-1 sm:mx-0">
                  <table className="w-full min-w-[500px] text-left text-sm">
                    <thead className="text-[var(--color-muted)]">
                      <tr className="border-b border-[var(--color-border)]">
                        <th className="py-2 pr-3">Asset</th>
                        <th className="py-2 pr-3">Free</th>
                        <th className="py-2 pr-3">Locked</th>
                        <th className="py-2 pr-3">Total</th>
                        <th className="py-2">~USD</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(data.balances || []).map((b) => (
                        <tr key={b.asset} className="border-b border-[var(--color-border)]/60">
                          <td className="py-2 pr-3 font-medium">{b.asset}</td>
                          <td className="py-2 pr-3 tabular-nums">{formatQty(b.free)}</td>
                          <td className="py-2 pr-3 tabular-nums">{formatQty(b.locked)}</td>
                          <td className="py-2 pr-3 tabular-nums">{formatQty(b.total)}</td>
                          <td className="py-2">
                            <UsdCell usd={b.usd} pricing={b.pricing} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
