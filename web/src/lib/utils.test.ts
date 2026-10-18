import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { formatPrice } from "./utils.ts";

describe("formatPrice", () => {
  it("keeps sub-dollar prices apart and trims trailing zeros", () => {
    assert.equal(formatPrice(0.1834), "0.1834");
    assert.equal(formatPrice(0.179), "0.179");
    assert.equal(formatPrice(0.52), "0.52");
    assert.equal(formatPrice(2.31), "2.31");
    assert.equal(formatPrice(67000), "67000");
  });
});
