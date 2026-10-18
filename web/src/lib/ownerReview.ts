export const OWNER_REASON_MIN = 10;

/** True when a written review is long enough to enable the confirm button. */
export function ownerReasonReady(text: string): boolean {
  return text.trim().length >= OWNER_REASON_MIN;
}

/** The Home shortcut appears only after the owner has scrolled past a non-empty queue. */
export function showQueueJump(pendingCount: number, scrolledPast: boolean): boolean {
  return pendingCount > 0 && scrolledPast;
}
