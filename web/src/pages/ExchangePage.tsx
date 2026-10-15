import { useEffect, useState } from "react";
import { UsdBars } from "@/components/Charts";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type ExchangePayload } from "@/lib/api";
import { formatNum, formatQty, relativeAge } from "@/lib/utils";

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

          <Card className="border-[var(--color-border)]">
            <CardHeader>
              <CardTitle>Connecting Exchange Keys (When Ready)</CardTitle>
              <CardDescription>Follow these instructions to safely load read-only spot credentials</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-xs text-[var(--color-muted)] leading-relaxed">
              <p>
                1. On Binance Global or Binance TH, generate an API key restricted to IP <code className="bg-[#161d27] px-1 py-0.5 rounded text-[var(--color-foreground)]">76.13.218.48</code>.
              </p>
              <p>
                2. Check <strong>Enable Reading</strong> only. Ensure <strong>Enable Spot &amp; Margin Trading</strong>, <strong>Enable Withdrawals</strong>, and <strong>Enable Futures</strong> remain unchecked.
              </p>
              <p>
                3. Place credentials in <code className="bg-[#161d27] px-1 py-0.5 rounded text-[var(--color-foreground)]">/opt/jarvise/.env</code> on the VPS (never commit to git repository):
              </p>
              <pre className="overflow-x-auto rounded-md bg-[#161d27] p-3 text-xs text-[var(--color-foreground)] font-mono">
                BINANCE_API_KEY=your_read_only_key_here&#10;BINANCE_API_SECRET=your_secret_here
              </pre>
            </CardContent>
          </Card>
        </div>
      ) : (
        <>
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
