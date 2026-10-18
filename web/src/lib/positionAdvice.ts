export type CloseAdviceLevel = 1 | 2 | 3 | 4 | 5;

export interface CloseRecommendation {
  level: CloseAdviceLevel;
  label: string;
  actionText: string;
  dotColor: string;
  dotClass: string;
  btnClass: string;
  textClass: string;
  tooltip: string;
  rMultiple: number | null;
}

/**
 * 5-Level Close Position Recommendation Indicator
 *
 * 1 = ไม่ควรปิด (สีแดง) - ติดลบ ยังอยู่ในขอบเขต Stop 1.5 ATR ไม่ควร Panic Close
 * 2 = ยังไม่แนะนำ (เหลืองส้ม) - กำไรเล็กน้อย (< +0.5R) ยังไม่ถึงจุดขยับ Breakeven
 * 3 = พิจารณาได้ (เหลือง) - ขยับ Breakeven (+0.5R) ปลอดภัยแล้ว สามารถถือต่อหรือปิดหากผันผวน
 * 4 = ปล่อยให้ trail ทำงาน (>= 1R) - จุดตัดขยับ 1R ปิดมือได้แต่ไม่ใช่คำแนะนำของระบบ
 * 5 = กำไรสูง · trail ล็อกอยู่ (>= 1.5R) - ปิดมือได้แต่ไม่ใช่คำแนะนำของระบบ
 */
export function evaluateCloseRecommendation(pos: {
  unrealized_pnl?: unknown;
  entry_price?: unknown;
  stop_price?: unknown;
  initial_stop_price?: unknown;
  qty?: unknown;
  stop_source?: unknown;
}): CloseRecommendation {
  const uPnl = Number(pos.unrealized_pnl) || 0;
  const entry = Number(pos.entry_price);
  const stop = pos.stop_price != null ? Number(pos.stop_price) : null;
  const initialStop = pos.initial_stop_price != null ? Number(pos.initial_stop_price) : stop;
  const qty = Number(pos.qty);
  const risk = initialStop != null && Number.isFinite(entry) ? Math.abs(entry - initialStop) : null;
  const rMultiple =
    risk != null && risk > 0 && Number.isFinite(qty) && qty !== 0
      ? uPnl / (qty * risk)
      : null;

  // Level 5: ควรปิดอย่างยิ่ง (R >= 1.5) - สีเขียวสดสะท้อนแสง (Neon Bright Lime)
  if ((rMultiple != null && rMultiple >= 1.5) || (rMultiple == null && uPnl >= 150)) {
    return {
      level: 5,
      label: "กำไรสูง · trail ล็อกอยู่",
      actionText: "trail ล็อกอยู่ (>= +1.5R)",
      dotColor: "#22c55e",
      dotClass: "bg-[#22c55e] shadow-[0_0_8px_#22c55e] animate-pulse ring-2 ring-[#4ade80]/60",
      btnClass:
        "border-[#22c55e] text-[#22c55e] bg-[#22c55e]/15 hover:bg-[#22c55e]/25 shadow-[0_0_12px_rgba(34,197,94,0.35)]",
      textClass: "text-[#22c55e] font-bold drop-shadow-[0_0_4px_rgba(34,197,94,0.4)]",
      tooltip: `ระดับ 5: กำไรสูง · trail ล็อกอยู่ (กำไร ${rMultiple != null ? `+${rMultiple.toFixed(2)}R` : `+$${uPnl.toFixed(2)}`}) จุดตัดตามกำไรอยู่แล้ว ปิดมือได้ แต่ไม่ใช่คำแนะนำของระบบ`,
      rMultiple,
    };
  }

  // Level 4: ควรปิด (1.0 <= R < 1.5) - สีเขียวเข้ม (Dark Green / Emerald)
  if ((rMultiple != null && rMultiple >= 1.0) || (rMultiple == null && uPnl >= 80)) {
    return {
      level: 4,
      label: "ปล่อยให้ trail ทำงาน",
      actionText: "trail ขยับ 1R (>= +1R)",
      dotColor: "#15803d",
      dotClass: "bg-[#16a34a] ring-2 ring-[#15803d]/60",
      btnClass:
        "border-[#16a34a] text-[#16a34a] bg-[#16a34a]/10 hover:bg-[#16a34a]/20",
      textClass: "text-[#16a34a] font-semibold",
      tooltip: `ระดับ 4: ปล่อยให้ trail ทำงาน (กำไร ${rMultiple != null ? `+${rMultiple.toFixed(2)}R` : `+$${uPnl.toFixed(2)}`}) จุดตัดขยับ 1R จากราคาที่ดีที่สุด ปิดมือได้ แต่ไม่ใช่คำแนะนำของระบบ`,
      rMultiple,
    };
  }

  // Level 3: พิจารณาได้ (0.5 <= R < 1.0) - สีเหลือง (Yellow / Amber)
  if ((rMultiple != null && rMultiple >= 0.5) || (rMultiple == null && uPnl > 20)) {
    const source = String(pos.stop_source || "");
    const locked = source === "breakeven" || source === "trail";
    const gain = rMultiple != null ? `+${rMultiple.toFixed(2)}R` : `+$${uPnl.toFixed(2)}`;
    return {
      level: 3,
      label: "พิจารณาได้",
      actionText: locked ? "บังทุนแล้ว (BE Locked)" : "จุดตัดยังไม่ขยับ",
      dotColor: "#eab308",
      dotClass: "bg-[#eab308] ring-2 ring-[#eab308]/50",
      btnClass:
        "border-[#eab308] text-[#eab308] bg-[#eab308]/10 hover:bg-[#eab308]/20",
      textClass: "text-[#eab308] font-medium",
      tooltip: locked
        ? `ระดับ 3: พิจารณาได้ (กำไร ${gain}) ขยับจุดตัดเสมอตัว (BE) แล้ว ต้นทุนปลอดภัย สามารถถือลุ้น 1R หรือปิดหากตลาดเริ่มชะลอตัว`
        : `ระดับ 3: พิจารณาได้ (กำไร ${gain}) จุดตัดยังไม่ขยับจากจุดเริ่ม ยังไม่ใช่บังทุน`,
      rMultiple,
    };
  }

  // Level 2: ยังไม่แนะนำ (0 <= R < 0.5) - สีเหลืองส้ม (Orange)
  if (uPnl >= 0) {
    return {
      level: 2,
      label: "ยังไม่แนะนำ",
      actionText: "เพิ่งเริ่มวิ่ง (< +0.5R)",
      dotColor: "#f97316",
      dotClass: "bg-[#f97316] ring-2 ring-[#f97316]/50",
      btnClass:
        "border-[#f97316]/70 text-[#f97316] bg-[#f97316]/10 hover:bg-[#f97316]/20",
      textClass: "text-[#f97316] font-medium",
      tooltip: `ระดับ 2: ยังไม่แนะนำให้ปิด (กำไร ${rMultiple != null ? `+${rMultiple.toFixed(2)}R` : `+$${uPnl.toFixed(2)}`}) กำไรยังไม่ถึงจุด Breakeven (+0.5R) ควรปล่อยรันตามแผน`,
      rMultiple,
    };
  }

  // Level 1: ไม่ควรปิด (R < 0 หรือ uPnL < 0) - สีแดง (Dark Red / Rose)
  return {
    level: 1,
    label: "ไม่ควรปิด",
    actionText: "อยู่ในแผน Stop 1.5 ATR",
    dotColor: "#ef4444",
    dotClass: "bg-[#ef4444] ring-2 ring-[#ef4444]/50",
    btnClass:
      "border-[#ef4444]/60 text-[#ef4444] bg-[#ef4444]/10 hover:bg-[#ef4444]/20",
    textClass: "text-[#ef4444] font-medium",
    tooltip: `ระดับ 1: ไม่ควรปิด (ติดลบ ${rMultiple != null ? `${rMultiple.toFixed(2)}R` : `$${uPnl.toFixed(2)}`}) ยังอยู่ในขอบเขตความเสี่ยงตามระบบ 1.5× ATR ไม่ควรใช้อารมณ์ Panic Sell ตัดก่อนจุดตัด`,
    rMultiple,
  };
}
