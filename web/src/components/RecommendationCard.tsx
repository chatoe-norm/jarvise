// web/src/components/RecommendationCard.tsx
import type { ReactNode } from "react";
import { CandleChart } from "@/components/CandleChart";
import { ConfidenceLadder } from "@/components/ConfidenceLadder";
import { Badge } from "@/components/ui/badge";
import type { OhlcvPayload, RecommendationPayload } from "@/lib/api";
import { ownerReasonCopy } from "@/lib/copy";
import { formatNum, formatPrice } from "@/lib/utils";

const REC_LABEL: Record<
  RecommendationPayload["recommendation"],
  { text: string; variant: "ok" | "default" | "danger" }
> = {
  approve: { text: "ผ่านเกณฑ์สัญญาณ", variant: "ok" },
  approve_with_caution: { text: "ผ่านเกณฑ์แบบระวัง", variant: "default" },
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
        {data.evidence ? (
          <p className="w-full text-xs text-[var(--color-muted)]">{data.evidence}</p>
        ) : null}
        <span className="text-xs text-[var(--color-muted)]">
          ความมั่นใจ: {data.confidence_label}
        </span>
      </div>

      {data.recommendation_source === "claude" && data.claude ? (
        <Section title={`Claude (${data.claude.model}) ตัดสิน: ${data.claude.decision}`}>
          <p>{ownerReasonCopy(data.claude.reason)}</p>
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
          signalTs={ohlcv?.signal_ts}
        />
      </Section>

      <Section title="เกิดอะไรขึ้น">
        <List items={data.what_happened} />
      </Section>

      <Section title="ความเสี่ยง (paper)">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
          <Stat label="ขนาด (% พอร์ต)" value={formatNum(data.risk.size_pct_equity, 3)} />
          <Stat label="มูลค่า (USD)" value={formatNum(data.risk.notional_usd, 2)} />
          <Stat label="ราคาตัดขาดทุน" value={formatPrice(data.risk.invalidation_price)} />
          <Stat
            label="ขาดทุนโดยประมาณ (USD)"
            value={data.risk.est_loss_usd == null ? "—" : formatNum(data.risk.est_loss_usd, 2)}
          />
          <Stat label="พอร์ต paper (USD)" value={formatNum(data.risk.equity_usd, 2)} />
          <Stat label="ระยะ stop (x ATR)" value={formatNum(data.risk.stop_atr_multiple, 1)} />
        </dl>
      </Section>

      {data.scenarios?.length ? (
        <Section title="สถานการณ์ที่เป็นไปได้ (ไม่ใช่การทำนาย)">
          <ul className="space-y-3">
            {data.scenarios.map((item) => (
              <li key={item.id} className="rounded-md border border-[var(--color-border)] px-3 py-2">
                <div className="text-sm font-medium text-[var(--color-foreground)]">{item.title}</div>
                <p className="mt-1 text-xs text-[var(--color-muted)]">{item.detail}</p>
                {item.levels.length ? (
                  <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1">
                    {item.levels.map((level) => (
                      <Stat key={level.label} label={level.label} value={formatPrice(level.price)} />
                    ))}
                  </dl>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

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

      {data.news_context?.lines?.length ? (
        <Section title="บริบทข่าว ไม่ใช่สัญญาณ">
          <List items={data.news_context.lines} />
        </Section>
      ) : null}
    </div>
  );
}
