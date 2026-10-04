import type { ConfidenceBreakdown } from "@/lib/api";
import { formatNum } from "@/lib/utils";

function fmtDelta(delta: number): string {
  if (delta === 0) return "±0";
  const sign = delta > 0 ? "+" : "";
  return `${sign}${delta.toFixed(2)}`;
}

export function ConfidenceLadder({
  breakdown,
}: {
  breakdown: ConfidenceBreakdown;
}) {
  const flat = breakdown.gates.flat_below;
  const approve = breakdown.gates.doctrine_free_approve;
  return (
    <div className="space-y-2 text-sm">
      {breakdown.insufficient ? (
        <p className="text-[var(--color-muted)]">
          Indicators incomplete — ladder shows warm-up flat score.
        </p>
      ) : null}
      <ul className="space-y-1">
        {breakdown.steps.map((step, i) => (
          <li
            key={`${i}-${step.label}`}
            className="flex items-start justify-between gap-3"
          >
            <span className="text-[var(--color-muted)]">{step.label}</span>
            <span
              className={
                step.delta < 0
                  ? "tabular-nums text-[var(--color-danger)]"
                  : step.delta > 0 && i > 0
                    ? "tabular-nums text-[var(--color-ok)]"
                    : "tabular-nums"
              }
            >
              {i === 0 ? formatNum(step.delta) : fmtDelta(step.delta)}
            </span>
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-t border-[var(--color-border)] pt-2">
        <span className="font-medium">Total</span>
        <span className="font-semibold tabular-nums">
          {formatNum(breakdown.total)}
        </span>
      </div>
      <p className="text-xs text-[var(--color-muted)]">
        {formatNum(flat)} = FLAT gate · {formatNum(approve)} = auto-approve
        without trading rules
        {breakdown.total < flat
          ? " · below FLAT → analyzer forces flat"
          : breakdown.total < approve
            ? " · mid band → caution / may defer without trading rules"
            : " · above trading-rules-free gate"}
      </p>
    </div>
  );
}
