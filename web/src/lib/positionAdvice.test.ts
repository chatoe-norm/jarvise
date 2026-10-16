import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { evaluateCloseRecommendation } from "./positionAdvice.ts";

describe("evaluateCloseRecommendation - 5 Levels Indicator", () => {
  it("Level 1: Red - ไม่ควรปิด when in negative R or uPnL < 0", () => {
    const pos = {
      unrealized_pnl: -45,
      entry_price: 65000,
      stop_price: 64000,
      initial_stop_price: 64000,
      qty: 0.1,
    };
    const rec = evaluateCloseRecommendation(pos);
    assert.equal(rec.level, 1);
    assert.equal(rec.label, "ไม่ควรปิด");
    assert.equal(rec.dotColor, "#ef4444");
    assert.ok(rec.rMultiple != null && rec.rMultiple < 0);
  });

  it("Level 2: Orange - ยังไม่แนะนำ when 0 <= R < 0.5", () => {
    const pos = {
      unrealized_pnl: 30, // risk = 1000 * 0.1 = 100, so R = 30 / 100 = 0.3R
      entry_price: 65000,
      stop_price: 64000,
      initial_stop_price: 64000,
      qty: 0.1,
    };
    const rec = evaluateCloseRecommendation(pos);
    assert.equal(rec.level, 2);
    assert.equal(rec.label, "ยังไม่แนะนำ");
    assert.equal(rec.dotColor, "#f97316");
    assert.ok(rec.rMultiple != null && rec.rMultiple >= 0 && rec.rMultiple < 0.5);
  });

  it("Level 3: Yellow - พิจารณาได้ when 0.5 <= R < 1.0", () => {
    const pos = {
      unrealized_pnl: 75, // R = 75 / 100 = 0.75R
      entry_price: 65000,
      stop_price: 65130,
      initial_stop_price: 64000,
      qty: 0.1,
    };
    const rec = evaluateCloseRecommendation(pos);
    assert.equal(rec.level, 3);
    assert.equal(rec.label, "พิจารณาได้");
    assert.equal(rec.dotColor, "#eab308");
    assert.ok(rec.rMultiple != null && rec.rMultiple >= 0.5 && rec.rMultiple < 1.0);
  });

  it("Level 4: Dark Green - ควรปิด when 1.0 <= R < 1.5", () => {
    const pos = {
      unrealized_pnl: 125, // R = 125 / 100 = 1.25R
      entry_price: 65000,
      stop_price: 66000,
      initial_stop_price: 64000,
      qty: 0.1,
    };
    const rec = evaluateCloseRecommendation(pos);
    assert.equal(rec.level, 4);
    assert.equal(rec.label, "ควรปิด");
    assert.equal(rec.dotColor, "#15803d");
    assert.ok(rec.rMultiple != null && rec.rMultiple >= 1.0 && rec.rMultiple < 1.5);
  });

  it("Level 5: Neon Green - ควรปิดอย่างยิ่ง when R >= 1.5", () => {
    const pos = {
      unrealized_pnl: 180, // R = 180 / 100 = 1.8R
      entry_price: 65000,
      stop_price: 66000,
      initial_stop_price: 64000,
      qty: 0.1,
    };
    const rec = evaluateCloseRecommendation(pos);
    assert.equal(rec.level, 5);
    assert.equal(rec.label, "ควรปิดอย่างยิ่ง");
    assert.equal(rec.dotColor, "#22c55e");
    assert.ok(rec.rMultiple != null && rec.rMultiple >= 1.5);
  });
});
