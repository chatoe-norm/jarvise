import { Info } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { infoTipAriaExpanded, infoTipPanel, toggleInfoTip } from "@/lib/infoTip";

const POPOVER_WIDTH = 256;

/** Tap target for a sentence that used to live in a hover-only title. */
export function InfoTip({ text, label = "รายละเอียด" }: { text: string; label?: string }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"inline" | "popover">("inline");
  const [box, setBox] = useState<{ top: number; left: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    function place() {
      const nextMode = infoTipPanel(window.innerWidth);
      setMode(nextMode);
      const rect = buttonRef.current?.getBoundingClientRect();
      if (!rect || nextMode !== "popover") return;
      const left = Math.max(8, Math.min(rect.left, window.innerWidth - POPOVER_WIDTH - 8));
      setBox({ top: rect.bottom + 4, left });
    }
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  if (!text) return null;
  const panel = (
    <span id={panelId} role="note" className="block text-xs leading-relaxed text-[var(--color-muted)]">
      {text}
    </span>
  );
  return (
    <span className="relative inline-flex max-w-full items-start align-middle">
      <button
        ref={buttonRef}
        type="button"
        className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-[var(--color-muted)] hover:text-[var(--color-foreground)] pointer-coarse:min-h-11 pointer-coarse:min-w-11"
        aria-expanded={infoTipAriaExpanded(open)}
        aria-controls={panelId}
        aria-label={label}
        onClick={() => {
          setOpen((current) => {
            const next = toggleInfoTip(current);
            if (next) {
              const nextMode = infoTipPanel(window.innerWidth);
              setMode(nextMode);
              const rect = buttonRef.current?.getBoundingClientRect();
              if (rect && nextMode === "popover") {
                const left = Math.max(8, Math.min(rect.left, window.innerWidth - POPOVER_WIDTH - 8));
                setBox({ top: rect.bottom + 4, left });
              }
            }
            return next;
          });
        }}
      >
        <Info className="h-4 w-4" aria-hidden />
      </button>
      {open && mode === "inline" ? (
        <span className="mt-1 block w-full basis-full">{panel}</span>
      ) : null}
      {open && mode === "popover" && box
        ? createPortal(
            <span
              className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)] p-2 shadow-lg"
              style={{
                position: "fixed",
                top: box.top,
                left: box.left,
                width: POPOVER_WIDTH,
                zIndex: 80,
              }}
            >
              {panel}
            </span>,
            document.body,
          )
        : null}
    </span>
  );
}
