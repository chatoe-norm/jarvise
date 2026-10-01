import { useEffect, useState, type FormEvent } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import { formatNum } from "@/lib/utils";

export function DecisionsPage() {
  const [symbol, setSymbol] = useState("");
  const [timeframe, setTimeframe] = useState("");
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function load(sym = symbol, tf = timeframe) {
    setLoading(true);
    setError(null);
    try {
      const data = await api.analysis(sym, tf);
      if (!data.ok) setError(data.error || "Failed");
      setRows(data.rows || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load("", "");
  }, []);

  function onFilter(e: FormEvent) {
    e.preventDefault();
    void load(symbol, timeframe);
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Decisions</h1>
        <p className="text-sm text-[var(--color-muted)]">
          Latest analysis output — regime, action, and thesis.
        </p>
      </div>

      <Card>
        <CardContent className="pt-4">
          <form className="flex flex-wrap items-end gap-3" onSubmit={onFilter}>
            <label className="text-xs text-[var(--color-muted)]">
              Symbol
              <input
                className="mt-1 block rounded-md border border-[var(--color-border)] bg-[#161d27] px-2 py-1.5 text-sm text-[var(--color-foreground)]"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                placeholder="BTCUSDT"
              />
            </label>
            <label className="text-xs text-[var(--color-muted)]">
              Timeframe
              <input
                className="mt-1 block rounded-md border border-[var(--color-border)] bg-[#161d27] px-2 py-1.5 text-sm text-[var(--color-foreground)]"
                value={timeframe}
                onChange={(e) => setTimeframe(e.target.value)}
                placeholder="4h"
              />
            </label>
            <Button type="submit" size="sm">
              Filter
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                setSymbol("");
                setTimeframe("");
                void load("", "");
              }}
            >
              Clear
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Analysis</CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-sm text-[var(--color-muted)]">Loading…</p>
          ) : null}
          {error ? <p className="text-sm text-[var(--color-danger)]">{error}</p> : null}
          {!loading && !error && rows.length === 0 ? (
            <p className="text-sm text-[var(--color-muted)]">
              No analysis rows. Run <code>jarvise analyze</code> first.
            </p>
          ) : null}
          {rows.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-[var(--color-muted)]">
                  <tr className="border-b border-[var(--color-border)]">
                    <th className="py-2 pr-2">Symbol</th>
                    <th className="py-2 pr-2">TF</th>
                    <th className="py-2 pr-2">Regime</th>
                    <th className="py-2 pr-2">Action</th>
                    <th className="py-2 pr-2">Conf</th>
                    <th className="py-2 pr-2">Size%</th>
                    <th className="py-2">Thesis</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={i} className="border-b border-[var(--color-border)]/60 align-top">
                      <td className="py-2 pr-2 font-medium">{String(r.symbol ?? "")}</td>
                      <td className="py-2 pr-2">{String(r.timeframe ?? "")}</td>
                      <td className="py-2 pr-2">
                        <Badge variant="muted">{String(r.regime_state ?? "—")}</Badge>
                      </td>
                      <td className="py-2 pr-2">
                        <Badge variant="outline">{String(r.action ?? "—")}</Badge>
                      </td>
                      <td className="py-2 pr-2 tabular-nums">
                        {formatNum(r.confidence_score)}
                      </td>
                      <td className="py-2 pr-2 tabular-nums">
                        {formatNum(r.size_pct_equity)}
                      </td>
                      <td className="py-2 max-w-md text-[var(--color-muted)]">
                        {String(r.thesis ?? "")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
