import type { OhlcvBar } from "@/lib/api";
import { formatDecisionStamp, formatPrice } from "@/lib/utils";

const W = 640;
const H = 160;
const PAD = { top: 10, right: 52, bottom: 22, left: 8 };

function linePath(
  bars: OhlcvBar[],
  key: "ema_20" | "ema_200",
  xAt: (index: number) => number,
  y: (price: number) => number,
): string {
  const parts: string[] = [];
  let drawing = false;
  bars.forEach((bar, index) => {
    const value = bar[key];
    if (value == null || !Number.isFinite(value)) {
      drawing = false;
      return;
    }
    const cmd = drawing ? "L" : "M";
    parts.push(`${cmd}${xAt(index).toFixed(1)},${y(value).toFixed(1)}`);
    drawing = true;
  });
  return parts.join(" ");
}

export function CandleChart({
  bars,
  invalidation,
  label,
  signalTs,
}: {
  bars: OhlcvBar[];
  invalidation: number | null;
  label: string;
  signalTs?: number | null;
}) {
  if (bars.length === 0) {
    return (
      <p className="text-sm text-[var(--color-muted)]">
        ยังไม่มีแท่งปิดในฐานข้อมูลสำหรับ {label} — ingest คู่และ timeframe นี้ก่อน
      </p>
    );
  }

  const prices = bars.flatMap((b) => [b.h, b.l, b.ema_20, b.ema_200]);
  const finite = prices.filter((p): p is number => p != null && Number.isFinite(p));
  let lo = Math.min(...finite);
  let hi = Math.max(...finite);
  if (invalidation != null && Number.isFinite(invalidation)) {
    lo = Math.min(lo, invalidation);
    hi = Math.max(hi, invalidation);
  }
  const span = hi - lo || 1;
  const pad = span * 0.04;
  lo -= pad;
  hi += pad;
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const slot = innerW / bars.length;
  const y = (price: number) => PAD.top + ((hi - price) / (hi - lo)) * innerH;
  const xAt = (index: number) => PAD.left + slot * (index + 0.5);
  const last = bars[bars.length - 1];
  const firstStamp = formatDecisionStamp(bars[0].t);
  const lastStamp = formatDecisionStamp(last.t);
  const signalIndex =
    signalTs == null ? -1 : bars.findIndex((bar) => bar.t === signalTs);
  const ema20 = linePath(bars, "ema_20", xAt, y);
  const ema200 = linePath(bars, "ema_200", xAt, y);

  return (
    <div>
      <div className="mb-1 text-xs text-[var(--color-muted)]">
        {label} · ปิดล่าสุด {formatPrice(last.c)}
        {invalidation != null ? ` · ตัดขาดทุน ${formatPrice(invalidation)}` : ""}
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-40 w-full"
        role="img"
        aria-label={`Closed candles ${label}`}
      >
        {invalidation != null && Number.isFinite(invalidation) ? (
          <line
            x1={PAD.left}
            x2={W - PAD.right}
            y1={y(invalidation)}
            y2={y(invalidation)}
            stroke="var(--color-danger)"
            strokeDasharray="4 3"
            strokeWidth={1}
          />
        ) : null}
        {ema200 ? (
          <path d={ema200} fill="none" stroke="#eab308" strokeWidth={1.25} />
        ) : null}
        {ema20 ? (
          <path d={ema20} fill="none" stroke="#38bdf8" strokeWidth={1.25} />
        ) : null}
        {bars.map((b, i) => {
          const cx = xAt(i);
          const up = b.c >= b.o;
          const color = up ? "var(--color-ok)" : "var(--color-danger)";
          const bodyTop = y(Math.max(b.o, b.c));
          const bodyBot = y(Math.min(b.o, b.c));
          const bw = Math.max(2, Math.min(slot * 0.65, 8));
          const bh = Math.max(1, bodyBot - bodyTop);
          return (
            <g key={b.t}>
              <line
                x1={cx}
                x2={cx}
                y1={y(b.h)}
                y2={y(b.l)}
                stroke={color}
                strokeWidth={1}
              />
              <rect
                x={cx - bw / 2}
                y={bodyTop}
                width={bw}
                height={bh}
                fill={color}
              />
            </g>
          );
        })}
        {signalIndex >= 0 ? (
          <line
            x1={xAt(signalIndex)}
            x2={xAt(signalIndex)}
            y1={PAD.top + innerH - 8}
            y2={PAD.top + innerH}
            stroke="var(--color-accent)"
            strokeWidth={2}
          />
        ) : null}
        <text
          x={W - PAD.right + 4}
          y={y(hi) + 4}
          fill="var(--color-muted)"
          fontSize={10}
        >
          {formatPrice(hi)}
        </text>
        <text
          x={W - PAD.right + 4}
          y={y(lo)}
          fill="var(--color-muted)"
          fontSize={10}
        >
          {formatPrice(lo)}
        </text>
        <text x={PAD.left} y={H - 4} fill="var(--color-muted)" fontSize={10}>
          {firstStamp.date} {firstStamp.time}
        </text>
        <text
          x={W - PAD.right}
          y={H - 4}
          fill="var(--color-muted)"
          fontSize={10}
          textAnchor="end"
        >
          {lastStamp.date} {lastStamp.time}
        </text>
      </svg>
      <p className="mt-1 text-[11px] text-[var(--color-muted)]">
        เส้นฟ้า EMA20 · เส้นเหลือง EMA200 · เส้นประแดง ราคาตัดขาดทุน
      </p>
    </div>
  );
}
