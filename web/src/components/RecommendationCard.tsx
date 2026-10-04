// web/src/components/RecommendationCard.tsx
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { CandleChart } from "@/components/CandleChart";
import { ConfidenceLadder } from "@/components/ConfidenceLadder";
import type { OhlcvPayload, RecommendationPayload } from "@/lib/api";
import { formatNum } from "@/lib/utils";

const REC_LABEL: Record<
  RecommendationPayload["recommendation"],
  { text: string; variant: "ok" | "default" | "danger" }
> = {
  approve: { text: "แนะนำ APPROVE", variant: "ok" },
  approve_with_caution: { text: "APPROVE ได้ แต่ระวัง", variant: "default" },
  reject: { text: "แนะนำ REJECT", variant: "danger" },
};

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-muted)]">
        {title}
      </div>
      {children}
    </div>
  );
}

function List({ items }: { items: string[] }) {
  return (
    <ul className="list-disc space-y-1 pl-5">
      {items.map((line, i) => (
        <li key={i}>{line}</li>
      ))}
    </ul>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-[var(--color-muted)]">{label}</dt>
      <dd className="font-medium tabular-nums">{value}</dd>
    </div>
  );
}

export function RecommendationCard({
  data,
  ohlcv,
}: {
  data: RecommendationPayload;
  ohlcv?: OhlcvPayload | null;
}) {
  const rec = REC_LABEL[data.recommendation];
  const tfLabel = ohlcv
    ? `${ohlcv.symbol} ${ohlcv.timeframe}`
    : `${data.symbol} ${data.timeframe}`;
  return (
    <div className="space-y-4 rounded-md border border-[var(--color-border)] bg-[#121922] p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={rec.variant}>{rec.text}</Badge>
        <span className="font-medium">{data.headline}</span>
        <span className="text-xs text-[var(--color-muted)]">
          ความมั่นใจ: {data.confidence_label}
        </span>
      </div>

      {data.recommendation_source === "claude" && data.claude ? (
        <Section title={`Claude (${data.claude.model}) ตัดสิน: ${data.claude.decision}`}>
          <p>{data.claude.reason || "—"}</p>
        </Section>
      ) : null}

      {data.confidence_breakdown ? (
        <Section title="ทำไมได้คะแนนนี้">
          <ConfidenceLadder breakdown={data.confidence_breakdown} />
        </Section>
      ) : null}

      <Section title="ราคา (แท่งปิด)">
        <CandleChart
          bars={ohlcv?.bars ?? []}
          invalidation={ohlcv?.invalidation_price ?? data.risk.invalidation_price}
          label={tfLabel}
        />
      </Section>

      <Section title="เกิดอะไรขึ้น">
        <List items={data.what_happened} />
      </Section>

      <Section title="ความเสี่ยง (paper)">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
          <Stat label="ขนาด (% พอร์ต)" value={formatNum(data.risk.size_pct_equity, 3)} />
          <Stat label="มูลค่า (USD)" value={formatNum(data.risk.notional_usd, 2)} />
          <Stat label="ราคาตัดขาดทุน" value={formatNum(data.risk.invalidation_price, 2)} />
          <Stat
            label="ขาดทุนโดยประมาณ (USD)"
            value={data.risk.est_loss_usd == null ? "—" : formatNum(data.risk.est_loss_usd, 2)}
          />
          <Stat label="พอร์ต paper (USD)" value={formatNum(data.risk.equity_usd, 2)} />
          <Stat label="ระยะ stop (x ATR)" value={formatNum(data.risk.stop_atr_multiple, 1)} />
        </dl>
      </Section>

      <Section title="กฎการเทรด">
        {data.doctrine.length ? (
          <List items={data.doctrine} />
        ) : (
          <p className="text-[var(--color-muted)]">
            ไม่พบกฎการเทรดที่ตรงสัญญาณ
          </p>
        )}
      </Section>

      <Section title="เช็คก่อนกด">
        <List items={data.checklist} />
      </Section>
    </div>
  );
}
