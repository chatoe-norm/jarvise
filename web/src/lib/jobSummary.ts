import { resolveReasonLabel } from "@/lib/copy";
import { formatNum } from "@/lib/utils";

type Obj = Record<string, unknown>;

const asObj = (v: unknown): Obj | null => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null);
const count = (v: unknown): number => (Array.isArray(v) ? v.length : asObj(v) ? Object.keys(v as Obj).length : 0);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/**
 * One owner-readable line for a job status payload (Redis `jarvise:*:last`) or a Run Now result.
 * Unknown shapes fall back to the first few scalar fields, never a JSON dump.
 */
export function summarizeJob(dataKey: string, payload: unknown): string {
  const o = asObj(payload);
  if (!o) return payload == null ? "" : String(payload);
  if (o.raw !== undefined && o.ok === undefined) return "อ่านผลไม่ได้ (payload ไม่ใช่ JSON)";
  const errors = count(o.errors);
  const errText = errors > 0 ? ` · ผิดพลาด ${errors}` : "";

  switch (dataKey) {
    case "ingest": {
      const tfs = asObj(o.timeframes);
      const symbols = tfs
        ? new Set(Object.values(tfs).flatMap((tf) => Object.keys(asObj(asObj(tf)?.market_technicals) ?? {})))
            .size
        : count(o.market_technicals);
      const dur = num(o.duration_s);
      return `อัปเดต ${symbols} คู่${tfs ? ` · ${Object.keys(tfs).join("/")}` : ""}${errText}${dur != null ? ` · ${formatNum(dur, 1)}s` : ""}`;
    }
    case "rag": {
      const chunks = num(asObj(o.index)?.chunks);
      return `${chunks != null ? `กฎการเทรด ${chunks} chunks` : "ซิงค์กฎการเทรด"}${errText}`;
    }
    case "paper": {
      const results = Array.isArray(o.results) ? (o.results as unknown[]) : [];
      const queued = results.filter((x) => asObj(x)?.queued).length;
      return `สแกน ${results.length} คู่ · เข้าคิว ${queued}${errText}`;
    }
    case "paper_expire":
      return `หมดอายุ ${num(o.expired) ?? 0} · ปิด FLAT ${num(o.flat_fills) ?? 0} · ถือต่อ ${num(o.held) ?? 0}${
        count(o.flat_errors) ? ` · ผิดพลาด ${count(o.flat_errors)}` : ""
      }`;
    case "risk_monitor": {
      const equity = num(asObj(o.mtm)?.equity);
      const stops = num(asObj(o.stops)?.closed) ?? 0;
      const halted = o.halted === true;
      return `${halted ? "หยุดระบบ" : "ปกติ"}${equity != null ? ` · equity $${formatNum(equity)}` : ""} · PnL วันนี้ ${formatNum(
        num(o.day_mtm_pnl) ?? 0,
      )} · stop ปิด ${stops}`;
    }
    case "ingest_health":
      return `ตรวจ ${count(o.symbols)} คู่ · แจ้งเตือน ${count(o.alerts)}`;
    case "paper_auto": {
      if (o.skipped) return `ข้ามรอบ: ${resolveReasonLabel(String(o.reason || "skipped"))}`;
      return `อนุมัติ ${count(o.approved)} · เลื่อน ${count(o.deferred)} · ปฏิเสธ ${count(o.rejected)}${
        count(o.apply_failed) ? ` · เติมไม่สำเร็จ ${count(o.apply_failed)}` : ""
      }`;
    }
    default:
      return Object.entries(o)
        .filter(([k, v]) => !["ok", "at_ms", "telegram_sent", "paper_only", "db"].includes(k) && typeof v !== "object")
        .slice(0, 3)
        .map(([k, v]) => `${k}=${String(v)}`)
        .join(" · ");
  }
}
