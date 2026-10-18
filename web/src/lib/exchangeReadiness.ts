import type { EvGateStatus } from "@/lib/api";

export type ExchangeEvBadge = {
  text: string;
  variant: "ok" | "muted" | "outline";
};

/** Pass only with a real non-negative sample and a clear latch. n=0 is missing data. */
export function exchangeEvBadge(gate: EvGateStatus | null | undefined): ExchangeEvBadge {
  const n = gate?.n;
  if (n == null || n === 0) {
    return { text: "ยังไม่มีข้อมูล", variant: "outline" };
  }
  const minN = gate?.min_n ?? 8;
  const ev = gate?.ev;
  const passed = n >= minN && ev != null && ev >= 0 && gate?.latched !== true;
  if (passed) {
    return { text: `ผ่านเกณฑ์ (N=${n})`, variant: "ok" };
  }
  return { text: `ยังไม่ผ่าน (N=${n})`, variant: "muted" };
}
