import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { summarizeJob } from "./jobSummary.ts";

describe("summarizeJob", () => {
  it("summarizes the risk monitor without a JSON dump", () => {
    const line = summarizeJob("risk_monitor", {
      ok: true,
      mtm: { equity: 9986.58659493, cash: 9986.58 },
      stops: { closed: 0 },
      day_mtm_pnl: 0,
      halted: false,
    });
    assert.equal(line, "ปกติ · equity $9,986.59 · PnL วันนี้ 0 · stop ปิด 0");
  });

  it("counts queued candidates from a paper run", () => {
    const line = summarizeJob("paper", {
      ok: true,
      results: [{ queued: { id: "a" } }, { queued: null }, { queued: { id: "b" } }],
      errors: [],
    });
    assert.equal(line, "สแกน 3 คู่ · เข้าคิว 2");
  });

  it("explains a skipped auto-decide run in owner copy", () => {
    assert.equal(
      summarizeJob("paper_auto", { ok: true, skipped: true, reason: "auto_ev_gate" }),
      "ข้ามรอบ: เลื่อน — EV ของไม้ auto ยังไม่ผ่านเกณฑ์ (ดูหน้า Paper)",
    );
  });

  it("flags unreadable payloads", () => {
    assert.equal(summarizeJob("ingest", { raw: "not json" }), "อ่านผลไม่ได้ (payload ไม่ใช่ JSON)");
  });
});
