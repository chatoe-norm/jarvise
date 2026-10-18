import { Info } from "lucide-react";
import { useId, useState } from "react";
import { infoTipAriaExpanded, toggleInfoTip } from "@/lib/infoTip";

/** Tap target for a sentence that used to live in a hover-only title. */
export function InfoTip({ text, label = "รายละเอียด" }: { text: string; label?: string }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  if (!text) return null;
  return (
    <span className="relative inline-flex max-w-full flex-col items-start align-middle">
      <button
        type="button"
        className="inline-flex h-8 w-8 items-center justify-center rounded-md text-[var(--color-muted)] hover:text-[var(--color-foreground)] pointer-coarse:min-h-11 pointer-coarse:min-w-11"
        aria-expanded={infoTipAriaExpanded(open)}
        aria-controls={panelId}
        aria-label={label}
        onClick={() => setOpen((current) => toggleInfoTip(current))}
      >
        <Info className="h-4 w-4" aria-hidden />
      </button>
      {open ? (
        <span
          id={panelId}
          role="note"
          className="mt-1 block max-w-full text-xs leading-relaxed text-[var(--color-muted)] sm:absolute sm:left-0 sm:top-full sm:z-30 sm:mt-1 sm:w-64 sm:rounded-md sm:border sm:border-[var(--color-border)] sm:bg-[var(--color-card)] sm:p-2 sm:shadow-lg"
        >
          {text}
        </span>
      ) : null}
    </span>
  );
}
