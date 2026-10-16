import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";

export type ConfirmOptions = {
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** danger = red confirm button for actions that halt, cancel or reset things. */
  tone?: "danger" | "default";
};

type Pending = { opts: ConfirmOptions; resolve: (ok: boolean) => void };

/**
 * Promise-based confirm for owner actions that change state (kill switch, job runs, resets).
 * Render `dialog` once in the component; `await confirm({...})` resolves true only on confirm.
 */
export function useConfirm() {
  const [pending, setPending] = useState<Pending | null>(null);

  const confirm = useCallback(
    (opts: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        setPending({ opts, resolve });
      }),
    [],
  );

  const settle = useCallback(
    (ok: boolean) => {
      pending?.resolve(ok);
      setPending(null);
    },
    [pending],
  );

  const dialog = pending ? <ConfirmDialog opts={pending.opts} onSettle={settle} /> : null;
  return { confirm, dialog };
}

function ConfirmDialog({ opts, onSettle }: { opts: ConfirmOptions; onSettle: (ok: boolean) => void }) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    // Focus Cancel, not Confirm: a stray Enter must not run the action.
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onSettle(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onSettle]);

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
      onClick={() => onSettle(false)}
    >
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        className="w-full max-w-md rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="confirm-dialog-title" className="text-base font-semibold text-[var(--color-foreground)]">
          {opts.title}
        </h2>
        {opts.body ? <div className="mt-2 text-sm text-[var(--color-muted)]">{opts.body}</div> : null}
        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button ref={cancelRef} variant="outline" className="h-11 sm:h-9" onClick={() => onSettle(false)}>
            {opts.cancelLabel ?? "ยกเลิก"}
          </Button>
          <Button
            variant={opts.tone === "danger" ? "danger" : "default"}
            className="h-11 sm:h-9"
            onClick={() => onSettle(true)}
          >
            {opts.confirmLabel ?? "ยืนยัน"}
          </Button>
        </div>
      </div>
    </div>
  );
}
