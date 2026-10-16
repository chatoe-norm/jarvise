import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { capsToRaw, validateRiskCap, validateRiskCaps } from "./riskCaps.ts";

const GOOD = {
  max_notional_per_order: 2000,
  max_daily_loss_usd: 100,
  drawdown_lock_pct: 5,
  max_open_positions: 4,
  max_gross_notional_pct: 10,
  max_symbol_notional_pct: 6,
  max_correlated_bucket_pct: 10,
  max_risk_per_trade_pct: 1,
};

describe("validateRiskCap", () => {
  it("never turns a blank field into 0", () => {
    assert.equal(validateRiskCap("max_daily_loss_usd", ""), "ต้องกรอกค่า");
    assert.equal(validateRiskCap("max_daily_loss_usd", "   "), "ต้องกรอกค่า");
  });

  it("rejects zero, negatives and non-numbers", () => {
    assert.equal(validateRiskCap("max_daily_loss_usd", "0"), "ต้องมากกว่า 0");
    assert.equal(validateRiskCap("max_notional_per_order", "-5"), "ต้องมากกว่า 0");
    assert.equal(validateRiskCap("max_notional_per_order", "abc"), "ต้องเป็นตัวเลข");
  });

  it("enforces whole positions, the 1% risk ceiling and 100% pct caps", () => {
    assert.equal(validateRiskCap("max_open_positions", "2.5"), "ต้องเป็นจำนวนเต็ม");
    assert.equal(validateRiskCap("max_risk_per_trade_pct", "1.5"), "สูงสุด 1.0%");
    assert.equal(validateRiskCap("drawdown_lock_pct", "101"), "สูงสุด 100%");
    assert.equal(validateRiskCap("max_risk_per_trade_pct", "0.5"), null);
  });
});

describe("validateRiskCaps", () => {
  it("returns caps only when every field is valid", () => {
    const ok = validateRiskCaps(capsToRaw(GOOD));
    assert.deepEqual(ok.caps, GOOD);
    assert.deepEqual(ok.errors, {});

    const bad = validateRiskCaps({ ...capsToRaw(GOOD), max_daily_loss_usd: "" });
    assert.equal(bad.caps, null);
    assert.deepEqual(Object.keys(bad.errors), ["max_daily_loss_usd"]);
  });
});
