import { AlertTriangle, Check, CircleOff, Clock } from "lucide-react";
import { cn } from "@/lib/utils";

export type StateChipTone = "ok" | "warn" | "danger" | "stale" | "off";

const TONE: Record<StateChipTone, { icon: typeof Check; className: string }> = {
  ok: { icon: Check, className: "bg-[var(--color-ok)]/15 text-[var(--color-ok)]" },
  warn: { icon: AlertTriangle, className: "bg-[var(--color-warn)]/15 text-[var(--color-warn)]" },
  danger: { icon: AlertTriangle, className: "bg-[var(--color-danger)]/15 text-[var(--color-danger)]" },
  stale: { icon: Clock, className: "bg-[var(--color-warn)]/15 text-[var(--color-warn)]" },
  off: { icon: CircleOff, className: "bg-[var(--color-muted)]/15 text-[var(--color-muted)]" },
};

export function StateChip({
  tone,
  label,
  className,
}: {
  tone: StateChipTone;
  label: string;
  className?: string;
}) {
  const spec = TONE[tone];
  const Icon = spec.icon;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium",
        spec.className,
        className,
      )}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {label}
    </span>
  );
}
