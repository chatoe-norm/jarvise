import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export function Badge({
  className,
  variant = "default",
  ...props
}: HTMLAttributes<HTMLSpanElement> & {
  variant?: "default" | "ok" | "danger" | "outline" | "muted";
}) {
  const styles = {
    default: "bg-[var(--color-accent)]/15 text-[var(--color-accent)] border-transparent",
    ok: "bg-[var(--color-ok)]/15 text-[var(--color-ok)] border-transparent",
    danger: "bg-[var(--color-danger)]/15 text-[var(--color-danger)] border-transparent",
    outline: "border-[var(--color-border)] text-[var(--color-muted)]",
    muted: "bg-[#1a222d] text-[var(--color-muted)] border-transparent",
  } as const;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium",
        styles[variant],
        className,
      )}
      {...props}
    />
  );
}
