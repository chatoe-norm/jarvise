import { useState } from "react";
import { api, type ApprovalRow, type OhlcvPayload, type RecommendationPayload } from "@/lib/api";
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

/** The backend re-queues new signals under the same pending id, so cache per analysis too. */
function cardKey(r: ApprovalRow): string {
  return `${r.id}:${r.analysis_id ?? ""}`;
}

function isExpired(r: ApprovalRow): boolean {
  return r.expires_at_ms != null && r.expires_at_ms <= Date.now();
}

/** FLAT with no open position on that symbol: approving or rejecting changes nothing. */
function isNoopFlat(r: ApprovalRow, openSymbols: ReadonlySet<string>): boolean {
  return String(r.action || "").toLowerCase() === "flat" && !openSymbols.has(String(r.symbol || ""));
}

export function ApprovalQueue({
  rows,
  killSwitch,
  openSymbols,
  onChanged,
}: {
  rows: ApprovalRow[];
  killSwitch: boolean;
  /** Symbols with an open paper position; FLAT cards for other symbols are no-ops. */
  openSymbols: ReadonlySet<string>;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [cards, setCards] = useState<Record<string, RecommendationPayload>>({});
  const [ohlcv, setOhlcv] = useState<Record<string, OhlcvPayload>>({});
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

  async function toggle(row: ApprovalRow) {
    const id = row.id;
    const key = cardKey(row);
    const next = !open[id];
    setOpen((prev) => ({ ...prev, [id]: next }));
    if (!next || cards[key]) return;
    setCardError((prev) => {
      const cleared = { ...prev };
      delete cleared[key];
      return cleared;
    });
    try {
      const [card, series] = await Promise.all([
        api.recommendation(id),
        api.ohlcv(id).catch(() => null),
      ]);
      setCards((prev) => ({ ...prev, [key]: card }));
      if (series) setOhlcv((prev) => ({ ...prev, [key]: series }));
    } catch (err) {
      setCardError((prev) => ({
        ...prev,
        [key]: err instanceof Error ? err.message : String(err),
      }));
    }
  }

  const actionable = rows.filter((r) => !isNoopFlat(r, openSymbols));
  const noop = rows.filter((r) => isNoopFlat(r, openSymbols));

  function rowActions(r: ApprovalRow, card: RecommendationPayload | undefined, wide: boolean) {
    const btn = wide ? "flex-1" : undefined;
    return (
      <>
        <Button size="sm" variant="ghost" className={btn} onClick={() => void toggle(r)}>
          {open[r.id] ? "ซ่อนคำแนะนำ" : "ดูคำแนะนำ"}
        </Button>
        <Button
          size="sm"
          className={btn}
          // Expired cards are refused server-side; don't offer the click.
          disabled={killSwitch || busy !== null || isExpired(r)}
          title={isExpired(r) ? "การ์ดหมดอายุแล้ว — รอรอบสแกนถัดไป" : undefined}
          onClick={() => act(r.id, "approve")}
        >
          {busy === r.id + "approve" ? "…" : approveLabel(card)}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className={btn}
          disabled={busy !== null}
          onClick={() => act(r.id, "reject")}
        >
          {busy === r.id + "reject" ? "…" : rejectLabel(card)}
        </Button>
      </>
    );
  }

  function recommendationBody(r: ApprovalRow) {
    const key = cardKey(r);
    const card = cards[key];
    if (card) return <RecommendationCard data={card} ohlcv={ohlcv[key]} />;
    if (cardError[key]) {
      return <p className="text-sm text-[var(--color-danger)]">โหลดคำแนะนำไม่สำเร็จ: {cardError[key]}</p>;
    }
    return <p className="text-sm text-[var(--color-muted)]">กำลังโหลดคำแนะนำ…</p>;
  }

  /** Phones: one card per candidate so Approve/Reject never need a sideways scroll. */
  function renderCards(list: ApprovalRow[]) {
    return (
      <ul className="space-y-3 md:hidden">
        {list.map((r) => (
          <li key={r.id} className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/40 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="font-semibold">{r.symbol}</span>
              <Badge variant="outline">{String(r.action || "—")}</Badge>
            </div>
            <dl className="mt-2 grid grid-cols-4 gap-2 text-xs">
              <div>
                <dt className="text-[var(--color-muted)]">TF</dt>
                <dd>{r.timeframe}</dd>
              </div>
              <div>
                <dt className="text-[var(--color-muted)]">Conf</dt>
                <dd className="tabular-nums">{formatNum(r.confidence_score, 2)}</dd>
              </div>
              <div>
                <dt className="text-[var(--color-muted)]">Size%</dt>
                <dd className="tabular-nums">{formatNum(r.size_pct_equity, 2)}</dd>
              </div>
              <div>
                <dt className="text-[var(--color-muted)]">Expires</dt>
                <dd>{expiresIn(r.expires_at_ms)}</dd>
              </div>
            </dl>
            <div className="mt-3 flex gap-2">{rowActions(r, cards[cardKey(r)], true)}</div>
            {open[r.id] ? <div className="mt-3">{recommendationBody(r)}</div> : null}
          </li>
        ))}
      </ul>
    );
  }

  function renderTable(list: ApprovalRow[]) {
    return (
      <>
      {renderCards(list)}
      <div className="hidden md:block overflow-x-auto -mx-1 sm:mx-0">
        <table className="w-full min-w-[620px] text-left text-sm">
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
            {list.map((r) => {
              const key = cardKey(r);
              const card = cards[key];
              return [
                <tr key={r.id} className="border-b border-[var(--color-border)]/60">
                  <td className="py-3 pr-3 font-medium">{r.symbol}</td>
                  <td className="py-3 pr-3">{r.timeframe}</td>
                  <td className="py-3 pr-3">
                    <Badge variant="outline">{String(r.action || "—")}</Badge>
                  </td>
                  <td className="py-3 pr-3 tabular-nums">{formatNum(r.confidence_score, 2)}</td>
                  <td className="py-3 pr-3 tabular-nums">{formatNum(r.size_pct_equity, 2)}</td>
                  <td className="py-3 pr-3 text-[var(--color-muted)]">{expiresIn(r.expires_at_ms)}</td>
                  <td className="py-3">
                    <div className="flex flex-wrap gap-2">{rowActions(r, card, false)}</div>
                  </td>
                </tr>,
                open[r.id] ? (
                  <tr key={r.id + ":card"} className="border-b border-[var(--color-border)]/60">
                    <td colSpan={7} className="pb-4 pt-1">
                      {recommendationBody(r)}
                    </td>
                  </tr>
                ) : null,
              ];
            })}
          </tbody>
        </table>
      </div>
      </>
    );
  }

  return (
    <Card id="approval-queue">
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
        ) : actionable.length > 0 ? (
          renderTable(actionable)
        ) : (
          <p className="text-sm text-[var(--color-muted)]">
            ไม่มีการ์ดที่ต้องตัดสินใจ — เหลือเฉพาะสัญญาณ FLAT ที่ไม่มีไม้ให้ปิด
          </p>
        )}
        {noop.length > 0 ? (
          <details className="mt-4 rounded-lg border border-[var(--color-border)]/70 bg-[var(--color-card)]/40 px-3 py-2">
            <summary className="cursor-pointer select-none py-1 text-sm text-[var(--color-muted)]">
              FLAT ไม่มีโพสิชัน ({noop.length}) — ไม่ต้องทำอะไร ·{" "}
              <span className="font-mono text-xs">{noop.map((r) => r.symbol).join(", ")}</span>
            </summary>
            <p className="mt-1 mb-2 text-xs text-[var(--color-muted)]">
              ไม่มีไม้เปิดอยู่ในเหรียญเหล่านี้ การ์ดจะหมดอายุเองตามเวลา
            </p>
            {renderTable(noop)}
          </details>
        ) : null}
      </CardContent>
    </Card>
  );
}
