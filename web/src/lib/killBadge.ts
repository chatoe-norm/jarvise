import type { StatusPayload } from "@/lib/api";

export const KILL_UNKNOWN_TEXT = "ไม่ทราบสถานะ · บล็อกการอนุมัติไว้ก่อน";

export type KillBadge =
  | { kind: "unknown"; text: typeof KILL_UNKNOWN_TEXT; variant: "danger" }
  | { kind: "engaged"; text: "Kill Switch: ENGAGED"; variant: "danger" }
  | { kind: "clear"; text: "Kill Switch: CLEAR"; variant: "muted" };

/** known:false is fail-closed and must not look clear or engaged. */
export function killBadge(status: StatusPayload | null | undefined): KillBadge {
  if (status?.kill_switch_state?.known === false) {
    return { kind: "unknown", text: KILL_UNKNOWN_TEXT, variant: "danger" };
  }
  if (status?.kill_switch) {
    return { kind: "engaged", text: "Kill Switch: ENGAGED", variant: "danger" };
  }
  return { kind: "clear", text: "Kill Switch: CLEAR", variant: "muted" };
}
