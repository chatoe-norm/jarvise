import type { OhlcvBar } from "@/lib/api";
import { formatNum } from "@/lib/utils";

const W = 640;
const H = 160;
const PAD = { top: 10, right: 52, bottom: 22, left: 8 };

export function CandleChart({
  bars,
  invalidation,
  label,
}: {
  bars: OhlcvBar[];
  invalidation: number | null;
  label: string;
}) {
  if (bars.length === 0) {
    return (
      <p className="text-sm text-[var(--color-muted)]">
        ยังไม่มีแท่งปิดในฐานข้อมูลสำหรับ {label} — ingest คู่และ timeframe นี้ก่อน
      </p>
    );
  }

  const highs = bars.map((b) => b.h);
  const lows = bars.map((b) => b.l);
  let lo = Math.min(...lows);
  let hi = Math.max(...highs);
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
  const last = bars[bars.length - 1];

  return (
    <div>
      <div className="mb-1 text-xs text-[var(--color-muted)]">
        {label} · ปิดล่าสุด {formatNum(last.c, 2)}
        {invalidation != null ? ` · ตัดขาดทุน ${formatNum(invalidation, 2)}` : ""}
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
        {bars.map((b, i) => {
          const cx = PAD.left + slot * (i + 0.5);
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
        <text
          x={W - PAD.right + 4}
          y={y(hi) + 4}
          fill="var(--color-muted)"
          fontSize={10}
        >
          {formatNum(hi, 0)}
        </text>
        <text
          x={W - PAD.right + 4}
          y={y(lo)}
          fill="var(--color-muted)"
          fontSize={10}
        >
          {formatNum(lo, 0)}
        </text>
      </svg>
    </div>
  );
}
