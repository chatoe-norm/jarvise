import { useEffect, useState } from "react";
import { UsdBars } from "@/components/Charts";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type ExchangePayload } from "@/lib/api";
import { formatNum, relativeAge } from "@/lib/utils";

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
        <Card>
          <CardContent className="pt-4 text-sm text-[var(--color-muted)]">
            Exchange panel unavailable — spot API keys not configured or sync failed.
          </CardContent>
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Spot ~USD</CardTitle>
              <CardDescription>
                {data.venue} · fetched {relativeAge(data.fetched_at_ms)} · total{" "}
                {formatNum(data.total_usd)}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <UsdBars rows={bars} />
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
                <table className="w-full text-left text-sm">
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
                        <td className="py-2 pr-3 tabular-nums">{b.free}</td>
                        <td className="py-2 pr-3 tabular-nums">{b.locked}</td>
                        <td className="py-2 pr-3 tabular-nums">{b.total}</td>
                        <td className="py-2 tabular-nums">
                          {b.usd != null ? formatNum(b.usd) : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
