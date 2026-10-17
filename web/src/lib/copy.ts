/** Owner-facing wording. Stored Claude reasons may still say หลักคำสอน or "doctrine". */

import type { AnalysisOutcome, AnalysisOutcomeKind, EvGateStatus } from "@/lib/api";

export type OutcomeBadgeVariant = "default" | "ok" | "danger" | "outline" | "muted";

export type OutcomeCopy = {
  label: string;
  detail: string;
  variant: OutcomeBadgeVariant;
};

export function ownerReasonCopy(reason: string | null | undefined): string {
  if (reason == null || reason === "") return "—";
  // Whole word only: identifiers such as doctrine_query / jarvise_doctrine stay as written.
  return reason.replaceAll("หลักคำสอน", "กฎการเทรด").replace(/\bdoctrines?\b/gi, "กฎการเทรด");
}

/** Paper and Ops copy for the auto EV latch. A thin sample does not clear a latch. */
export function evGateOwnerCopy(gate: EvGateStatus | null | undefined): { title: string; detail: string } {
  if (!gate || (!gate.enabled && !gate.latched)) {
    return {
      title: "เกต EV ยังไม่เปิด",
      detail: "ตั้งค่าเกณฑ์ EV เพื่อเปิดเกตตามกฎการเทรด",
    };
  }
  const since =
    gate.latched_at_ms != null
      ? new Date(gate.latched_at_ms).toLocaleString("th-TH", { timeZone: "Asia/Bangkok" })
      : null;
  const why = resolveReasonLabel(gate.latch_reason || gate.reason || "auto_ev_gate");
  const sample = `ตอนนี้ N=${gate.n ?? 0}, EV ${gate.ev ?? "—"}`;
  if (gate.latched) {
    return {
      title: "เกต EV ล็อกตามกฎการเทรด",
      detail: `ล็อกตั้งแต่ ${since ?? "—"} — ${why} (${sample})`,
    };
  }
  if (gate.blocked) {
    return { title: "เกต EV กำลังบล็อกตามกฎการเทรด", detail: `${why} (${sample})` };
  }
  return {
    title: "เกต EV ยังไม่ล็อก",
    detail: `ต้องมีไม้ auto อย่างน้อย ${gate.min_n ?? "—"} ไม้ และ EV ต่ำกว่า ${gate.min_ev ?? "—"} จึงจะล็อก (${sample})`,
  };
}

/** Fixed auto-decide / approval codes (auto_decide.py, approval.py) → owner copy. */
const REASON_LABELS: Record<string, string> = {
  "auto:rule:same_side_hold": "ถือไม้เดิม (ฝั่งเดียวกัน)",
  "auto:rule:opposite_side_open": "เลื่อน — มีไม้ฝั่งตรงข้าม",
  "auto:rule:no_doctrine_low_conf": "เลื่อน — กฎการเทรดไม่พร้อม / ความเชื่อมั่นต่ำ",
  "auto:rule:flat_exit": "ปิดไม้ตามสัญญาณ FLAT",
  "auto:claude:approve": "อนุมัติ — Claude",
  "auto:claude:unparseable": "เลื่อน — Claude อ่านผลไม่ได้",
  deferred_cap: "เลื่อน — ถึงเพดานรอบนี้",
  missing_api_key: "เลื่อน — ไม่มี API key",
  auto_decide_disabled: "ปิด auto-decide",
  auto_decide_running: "ข้าม — มีรอบ auto-decide กำลังรันอยู่",
  auto_ev_gate: "เลื่อน — EV ของไม้ auto ยังไม่ผ่านเกณฑ์ (ดูหน้า Paper)",
  already_resolved: "ข้าม — การ์ดถูกตัดสินไปแล้ว",
  candidate_changed: "ข้าม — สัญญาณเปลี่ยนระหว่างรีวิว",
  run_halted: "หยุดรอบนี้ — ระบบสั่งหยุด",
  "kill_switch engaged": "ข้าม — Kill-Switch เปิดอยู่",
  "kill_switch unreadable": "ข้าม — อ่านสถานะ Kill-Switch ไม่ได้",
  live_trading_enabled: "ข้าม — โหมด live เปิดอยู่",
  no_database: "ข้าม — ไม่พบฐานข้อมูล",
  action_flat: "ไม่ส่งรีวิว — สัญญาณ FLAT",
  skipped: "ข้ามรอบ",
  timeout_flat: "หมดเวลา → FLAT",
  timeout_hold: "หมดเวลา → ถือไม้เดิม",
  "approval not pending": "การ์ดไม่ได้รออนุมัติแล้ว",
  "approval expired": "การ์ดหมดอายุ",
};

/** Owner-facing auto-decide / rule codes. Stored strings stay unchanged. */
export function resolveReasonLabel(reason: string | null | undefined): string {
  if (reason == null || reason === "") return "—";
  const fixed = REASON_LABELS[reason];
  if (fixed) return fixed;
  if (reason.startsWith("auto:claude:reject:")) {
    const rest = ownerReasonCopy(reason.slice("auto:claude:reject:".length));
    return rest === "—" ? "ปฏิเสธ — Claude" : `ปฏิเสธ — Claude: ${rest}`;
  }
  if (reason.startsWith("auto:claude:error:")) {
    return `เลื่อน — Claude ผิดพลาด: ${ownerReasonCopy(reason.slice("auto:claude:error:".length))}`;
  }
  if (reason.startsWith("auto:apply_failed:")) {
    return `เติมคำสั่งไม่สำเร็จ: ${ownerReasonCopy(reason.slice("auto:apply_failed:".length))}`;
  }
  if (reason.startsWith("auto:error:")) {
    return `เลื่อน — ระบบผิดพลาด: ${ownerReasonCopy(reason.slice("auto:error:".length))}`;
  }
  if (reason.startsWith("market_safety:")) {
    return `ตลาดไม่ปลอดภัย: ${reason.slice("market_safety:".length).trim()}`;
  }
  return ownerReasonCopy(reason);
}

export function decisionSourceLabel(source: string | null | undefined): string {
  if (source == null || source === "" || source === "unknown") return "ไม่ระบุแหล่ง";
  return source;
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
  const remapped = resolveReasonLabel(reason === "" ? null : reason);
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
