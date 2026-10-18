export const OPS_TAB_IDS = [
  "risk",
  "pipeline",
  "health",
  "universe",
  "auto_decide",
  "models",
  "rules",
  "observability",
  "all",
] as const;

export type OpsTabId = (typeof OPS_TAB_IDS)[number];

/** A valid hash wins. Otherwise mobile opens View All and desktop opens Risk. */
export function defaultOpsTab(hash: string, isMobile: boolean): OpsTabId {
  const id = hash.replace(/^#/, "").toLowerCase();
  if ((OPS_TAB_IDS as readonly string[]).includes(id)) {
    return id as OpsTabId;
  }
  return isMobile ? "all" : "risk";
}
