import { useState } from "react";
import { api, type ApprovalRow } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { expiresIn, formatNum } from "@/lib/utils";

export function ApprovalQueue({
  rows,
  killSwitch,
  onChanged,
}: {
  rows: ApprovalRow[];
  killSwitch: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(id: string, kind: "approve" | "reject") {
    setBusy(id + kind);
    setError(null);
    try {
      if (kind === "approve") await api.approve(id);
      else await api.reject(id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Approval queue</CardTitle>
        <CardDescription>
          Paper candidates awaiting your decision. Approve runs a simulated fill
          (live only when the LIVE banner is on).
        </CardDescription>
      </CardHeader>
      <CardContent>
        {error ? (
          <p className="mb-3 text-sm text-[var(--color-danger)]">{error}</p>
        ) : null}
        {rows.length === 0 ? (
          <p className="text-sm text-[var(--color-muted)]">
            No pending approvals — pipeline idle. Paper jobs enqueue here on schedule.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-[var(--color-muted)]">
                <tr className="border-b border-[var(--color-border)]">
                  <th className="py-2 pr-3 font-medium">Symbol</th>
                  <th className="py-2 pr-3 font-medium">TF</th>
                  <th className="py-2 pr-3 font-medium">Action</th>
                  <th className="py-2 pr-3 font-medium">Conf</th>
                  <th className="py-2 pr-3 font-medium">Size%</th>
                  <th className="py-2 pr-3 font-medium">Expires</th>
                  <th className="py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr
                    key={r.id}
                    className="border-b border-[var(--color-border)]/60"
                  >
                    <td className="py-3 pr-3 font-medium">{r.symbol}</td>
                    <td className="py-3 pr-3">{r.timeframe}</td>
                    <td className="py-3 pr-3">
                      <Badge variant="outline">{String(r.action || "—")}</Badge>
                    </td>
                    <td className="py-3 pr-3 tabular-nums">
                      {formatNum(r.confidence_score, 2)}
                    </td>
                    <td className="py-3 pr-3 tabular-nums">
                      {formatNum(r.size_pct_equity, 2)}
                    </td>
                    <td className="py-3 pr-3 text-[var(--color-muted)]">
                      {expiresIn(r.expires_at_ms)}
                    </td>
                    <td className="py-3">
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          disabled={killSwitch || busy !== null}
                          onClick={() => act(r.id, "approve")}
                        >
                          {busy === r.id + "approve" ? "…" : "Approve"}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy !== null}
                          onClick={() => act(r.id, "reject")}
                        >
                          {busy === r.id + "reject" ? "…" : "Reject"}
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
