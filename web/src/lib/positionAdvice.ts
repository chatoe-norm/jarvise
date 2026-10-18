export type CloseAdviceLevel = 0 | 1 | 2 | 3 | 4 | 5;

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

  if (rMultiple == null) {
    return {
      level: 0,
      label: "R unknown",
      actionText: "R unknown",
      dotColor: "var(--color-muted)",
      dotClass: "bg-[var(--color-muted)]",
      btnClass: "border-[var(--color-border)] text-[var(--color-muted)]",
      textClass: "text-[var(--color-muted)]",
      tooltip: "คำนวณ R ไม่ได้ เพราะไม่มีจุดตัดหรือจำนวน ไม่เดาจากจำนวนดอลลาร์",
      rMultiple: null,
    };
  }

  // Level 5: กำไรสูง · trail ล็อกอยู่ (R >= 1.5)
  if (rMultiple >= 1.5) {
    return {
      level: 5,
      label: "กำไรสูง · trail ล็อกอยู่",
      actionText: "trail ล็อกอยู่ (>= +1.5R)",
      dotColor: "var(--color-ok)",
      dotClass: "bg-[var(--color-ok)]",
      btnClass: "border-[var(--color-ok)] text-[var(--color-ok)] bg-[var(--color-ok)]/15",
      textClass: "text-[var(--color-ok)] font-bold",
      tooltip: `ระดับ 5: กำไรสูง · trail ล็อกอยู่ (กำไร +${rMultiple.toFixed(2)}R) จุดตัดตามกำไรอยู่แล้ว ปิดมือได้ แต่ไม่ใช่คำแนะนำของระบบ`,
      rMultiple,
    };
  }

  // Level 4: ปล่อยให้ trail ทำงาน (1.0 <= R < 1.5)
  if (rMultiple >= 1.0) {
    return {
      level: 4,
      label: "ปล่อยให้ trail ทำงาน",
      actionText: "trail ขยับ 1R (>= +1R)",
      dotColor: "var(--color-ok)",
      dotClass: "bg-[var(--color-ok)]",
      btnClass: "border-[var(--color-ok)] text-[var(--color-ok)] bg-[var(--color-ok)]/10",
      textClass: "text-[var(--color-ok)] font-semibold",
      tooltip: `ระดับ 4: ปล่อยให้ trail ทำงาน (กำไร +${rMultiple.toFixed(2)}R) จุดตัดขยับ 1R จากราคาที่ดีที่สุด ปิดมือได้ แต่ไม่ใช่คำแนะนำของระบบ`,
      rMultiple,
    };
  }

  // Level 3: พิจารณาได้ (0.5 <= R < 1.0) - สีเหลือง (Yellow / Amber)
  if (rMultiple >= 0.5) {
    const source = String(pos.stop_source || "");
    const locked = source === "breakeven" || source === "trail";
    const gain = `+${rMultiple.toFixed(2)}R`;
    return {
      level: 3,
      label: "พิจารณาได้",
      actionText: locked ? "บังทุนแล้ว (BE Locked)" : "จุดตัดยังไม่ขยับ",
      dotColor: "var(--color-warn)",
      dotClass: "bg-[var(--color-warn)]",
      btnClass: "border-[var(--color-warn)] text-[var(--color-warn)] bg-[var(--color-warn)]/10",
      textClass: "text-[var(--color-warn)] font-medium",
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
      dotColor: "var(--color-warn)",
      dotClass: "bg-[var(--color-warn)]",
      btnClass: "border-[var(--color-warn)]/70 text-[var(--color-warn)] bg-[var(--color-warn)]/10",
      textClass: "text-[var(--color-warn)] font-medium",
      tooltip: `ระดับ 2: ยังไม่แนะนำให้ปิด (กำไร +${rMultiple.toFixed(2)}R) กำไรยังไม่ถึงจุด Breakeven (+0.5R) ควรปล่อยรันตามแผน`,
      rMultiple,
    };
  }

  // Level 1: ไม่ควรปิด (R < 0 หรือ uPnL < 0) - สีแดง (Dark Red / Rose)
  return {
    level: 1,
    label: "ไม่ควรปิด",
    actionText: "อยู่ในแผน Stop 1.5 ATR",
    dotColor: "var(--color-danger)",
    dotClass: "bg-[var(--color-danger)]",
    btnClass: "border-[var(--color-danger)]/60 text-[var(--color-danger)] bg-[var(--color-danger)]/10",
    textClass: "text-[var(--color-danger)] font-medium",
    tooltip: `ระดับ 1: ไม่ควรปิด (ติดลบ ${rMultiple.toFixed(2)}R) ยังอยู่ในขอบเขตความเสี่ยงตามระบบ 1.5× ATR ไม่ควรใช้อารมณ์ Panic Sell ตัดก่อนจุดตัด`,
    rMultiple,
  };
}
