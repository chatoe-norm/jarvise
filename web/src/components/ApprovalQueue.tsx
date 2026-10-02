import { useState } from "react";
import { api, type ApprovalRow, type RecommendationPayload } from "@/lib/api";
import { RecommendationCard } from "@/components/RecommendationCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { expiresIn, formatNum } from "@/lib/utils";

function approveLabel(card: RecommendationPayload | undefined): string {
  if (!card) return "Approve";
  switch (card.recommendation) {
    case "approve":
      return "Approve (แนะนำ)";
    case "approve_with_caution":
      return "Approve (ระวัง)";
    case "reject":
      return "Approve";
    default: {
      const exhaustive: never = card.recommendation;
      return exhaustive;
    }
  }
}

function rejectLabel(card: RecommendationPayload | undefined): string {
  return card?.recommendation === "reject" ? "Reject (แนะนำ)" : "Reject";
}

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
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [cards, setCards] = useState<Record<string, RecommendationPayload>>({});
  const [cardError, setCardError] = useState<Record<string, string>>({});

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

  async function toggle(id: string) {
    const next = !open[id];
    setOpen((prev) => ({ ...prev, [id]: next }));
    if (!next || cards[id]) return;
    try {
      const card = await api.recommendation(id);
      setCards((prev) => ({ ...prev, [id]: card }));
    } catch (err) {
      setCardError((prev) => ({
        ...prev,
        [id]: err instanceof Error ? err.message : String(err),
      }));
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Approval queue</CardTitle>
        <CardDescription>
          Paper candidates awaiting your decision. Approve runs a simulated fill
          (live only when the LIVE banner is on). กด "ดูคำแนะนำ" เพื่ออ่านสรุปภาษาไทยก่อนตัดสินใจ
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
                {rows.map((r) => {
                  const card = cards[r.id];
                  return [
                    <tr key={r.id} className="border-b border-[var(--color-border)]/60">
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
                        <div className="flex flex-wrap gap-2">
                          <Button size="sm" variant="ghost" onClick={() => void toggle(r.id)}>
                            {open[r.id] ? "ซ่อนคำแนะนำ" : "ดูคำแนะนำ"}
                          </Button>
                          <Button
                            size="sm"
                            disabled={killSwitch || busy !== null}
                            onClick={() => act(r.id, "approve")}
                          >
                            {busy === r.id + "approve" ? "…" : approveLabel(card)}
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy !== null}
                            onClick={() => act(r.id, "reject")}
                          >
                            {busy === r.id + "reject" ? "…" : rejectLabel(card)}
                          </Button>
                        </div>
                      </td>
                    </tr>,
                    open[r.id] ? (
                      <tr key={r.id + ":card"} className="border-b border-[var(--color-border)]/60">
                        <td colSpan={7} className="pb-4 pt-1">
                          {card ? (
                            <RecommendationCard data={card} />
                          ) : cardError[r.id] ? (
                            <p className="text-sm text-[var(--color-danger)]">
                              โหลดคำแนะนำไม่สำเร็จ: {cardError[r.id]}
                            </p>
                          ) : (
                            <p className="text-sm text-[var(--color-muted)]">กำลังโหลดคำแนะนำ…</p>
                          )}
                        </td>
                      </tr>
                    ) : null,
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
