import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { exchangeEvBadge } from "./exchangeReadiness.ts";

describe("exchangeEvBadge", () => {
  it("treats a missing or empty sample as no data", () => {
    assert.equal(exchangeEvBadge(null).text, "ยังไม่มีข้อมูล");
    assert.equal(exchangeEvBadge({ n: 0, ev: null, latched: false, min_n: 10 }).text, "ยังไม่มีข้อมูล");
  });

  it("passes only when n meets min_n, EV is at least zero, and the latch is clear", () => {
    const passed = exchangeEvBadge({ n: 10, ev: 0.1, latched: false, min_n: 10 });
    assert.equal(passed.text, "ผ่านเกณฑ์ (N=10)");
    assert.equal(passed.variant, "ok");
  });

  it("fails a negative EV, a latch, or a thin sample", () => {
    assert.equal(exchangeEvBadge({ n: 10, ev: -0.65, latched: false, min_n: 10 }).text, "ยังไม่ผ่าน (N=10)");
    assert.equal(exchangeEvBadge({ n: 10, ev: 0.2, latched: true, min_n: 10 }).text, "ยังไม่ผ่าน (N=10)");
    assert.equal(exchangeEvBadge({ n: 3, ev: 0.2, latched: false, min_n: 10 }).text, "ยังไม่ผ่าน (N=3)");
  });
});
