import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { ownerReasonCopy, resolveReasonLabel } from "./copy.ts";
import { formatThesisDisplay } from "./utils.ts";

describe("ownerReasonCopy", () => {
  it("remaps หลักคำสอน and the English word doctrine to กฎการเทรด", () => {
    assert.equal(ownerReasonCopy("ขัดหลักคำสอน pullback"), "ขัดกฎการเทรด pullback");
    assert.equal(ownerReasonCopy("violates Doctrine rule 3"), "violates กฎการเทรด rule 3");
  });

  it("leaves identifiers like doctrine_query untouched", () => {
    assert.equal(ownerReasonCopy("doctrine_query timeout"), "doctrine_query timeout");
    assert.equal(ownerReasonCopy("jarvise_doctrine empty"), "jarvise_doctrine empty");
  });
});

describe("resolveReasonLabel", () => {
  it("maps auto-decide codes the backend emits", () => {
    assert.equal(resolveReasonLabel("auto_ev_gate"), "เลื่อน — EV ของไม้ auto ยังไม่ผ่านเกณฑ์ (ดูหน้า Paper)");
    assert.equal(resolveReasonLabel("kill_switch engaged"), "ข้าม — Kill-Switch เปิดอยู่");
    assert.equal(resolveReasonLabel("action_flat"), "ไม่ส่งรีวิว — สัญญาณ FLAT");
    assert.equal(resolveReasonLabel("auto:error:TimeoutError"), "เลื่อน — ระบบผิดพลาด: TimeoutError");
  });

  it("remaps doctrine inside Claude error and reject text", () => {
    assert.equal(resolveReasonLabel("auto:claude:error:doctrine missing"), "เลื่อน — Claude ผิดพลาด: กฎการเทรด missing");
    assert.equal(resolveReasonLabel("auto:claude:reject:ขัดหลักคำสอน"), "ปฏิเสธ — Claude: ขัดกฎการเทรด");
  });
});

describe("formatThesisDisplay", () => {
  it("rounds raw floats for display", () => {
    assert.equal(
      formatThesisDisplay("Trend down: EMA20<0.2371; RSI=39.06286504269636. HTF range."),
      "Trend down: EMA20<0.2371; RSI=39.06. HTF range.",
    );
    assert.equal(formatThesisDisplay("ATR=0.000123456789"), "ATR=0.0001235");
  });
});
