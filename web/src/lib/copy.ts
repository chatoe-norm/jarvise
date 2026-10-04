/** Owner-facing wording. Stored Claude reasons may still say หลักคำสอน. */

import type { AnalysisOutcome, AnalysisOutcomeKind } from "@/lib/api";

export type OutcomeBadgeVariant = "default" | "ok" | "danger" | "outline" | "muted";

export type OutcomeCopy = {
  label: string;
  detail: string;
  variant: OutcomeBadgeVariant;
};

export function ownerReasonCopy(reason: string | null | undefined): string {
  if (reason == null || reason === "") return "—";
  return reason.replaceAll("หลักคำสอน", "กฎการเทรด");
}

export function parseAnalysisOutcome(raw: unknown): AnalysisOutcome | null {
  if (raw == null || typeof raw !== "object") return null;
  const rec = raw as Record<string, unknown>;
  const kind = rec.kind;
  if (
    kind !== "filled" &&
    kind !== "hold" &&
    kind !== "rejected" &&
    kind !== "timed_out" &&
    kind !== "failed" &&
    kind !== "pending" &&
    kind !== "not_queued"
  ) {
    return null;
  }
  const fillsRaw = rec.fills;
  const fills = typeof fillsRaw === "number" && Number.isFinite(fillsRaw) ? fillsRaw : 0;
  const reasonsRaw = rec.fill_reasons;
  const fill_reasons = Array.isArray(reasonsRaw)
    ? reasonsRaw.filter((item): item is string => typeof item === "string")
    : [];
  const resolved = rec.resolved_at_ms;
  return {
    kind,
    approval_id: rec.approval_id == null ? null : String(rec.approval_id),
    approval_status: rec.approval_status == null ? null : String(rec.approval_status),
    resolve_reason: rec.resolve_reason == null ? null : String(rec.resolve_reason),
    resolved_at_ms:
      typeof resolved === "number" && Number.isFinite(resolved) ? resolved : null,
    fills,
    fill_reasons,
  };
}

export function outcomeCopy(
  outcome: AnalysisOutcome | null | undefined,
  action?: string | null,
): OutcomeCopy {
  const kind: AnalysisOutcomeKind = outcome?.kind ?? "not_queued";
  const reason = outcome?.resolve_reason ?? "";
  const actor = decisionActor(reason);
  const remapped = ownerReasonCopy(reason === "" ? null : reason);
  switch (kind) {
    case "filled": {
      const closed = (outcome?.fill_reasons || []).some((item) =>
        item.toLowerCase().startsWith("close"),
      );
      return {
        label: closed ? "ปิดไม้" : "เปิดไม้",
        detail: actorDetail(actor, outcome?.fills),
        variant: "ok",
      };
    }
    case "hold":
      return {
        label: "ถือไม้เดิม (ฝั่งเดียวกัน)",
        detail: actorDetail(actor, outcome?.fills),
        variant: "muted",
      };
    case "rejected":
      return {
        label: rejectLabel(reason, actor),
        detail: remapped === "—" ? actor : remapped,
        variant: "danger",
      };
    case "timed_out":
      return {
        label: reason === "timeout_flat" ? "หมดเวลา → FLAT" : "หมดเวลา",
        detail: remapped === "—" ? "" : remapped,
        variant: "outline",
      };
    case "failed":
      return {
        label: "ล้มเหลว",
        detail: remapped === "—" ? "" : remapped,
        variant: "danger",
      };
    case "pending":
      return {
        label: "รออนุมัติ",
        detail: "",
        variant: "default",
      };
    case "not_queued": {
      const isFlat = String(action || "").toLowerCase() === "flat";
      return {
        label: isFlat ? "ไม่มีไม้ให้ปิด" : "ไม่เข้าคิว / ถูกสัญญาณใหม่ทับ",
        detail: "",
        variant: "muted",
      };
    }
    default: {
      const _exhaustive: never = kind;
      return _exhaustive;
    }
  }
}

function decisionActor(reason: string): string {
  if (reason.startsWith("auto:claude:")) return "Claude";
  if (reason.startsWith("auto:rule:")) return "กฎ";
  if (reason === "ui" || reason === "paper_fill") return "เจ้าของ";
  return "";
}

function actorDetail(actor: string, fills: number | undefined): string {
  const parts: string[] = [];
  if (actor) parts.push(actor);
  if (fills != null && fills > 0) parts.push(`${fills} fill`);
  return parts.join(" · ");
}

function rejectLabel(reason: string, actor: string): string {
  if (actor === "เจ้าของ" || reason === "ui") return "ปฏิเสธ — เจ้าของ";
  if (actor === "Claude" || reason.startsWith("auto:claude:reject")) return "ปฏิเสธ — Claude";
  return "ปฏิเสธ";
}
