import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { ageIsStale, autoDecideChip } from "../lib/dashboardRules.ts";

describe("autoDecideChip", () => {
  it("leads with off, latched, then on", () => {
    assert.deepEqual(autoDecideChip(false, true), { tone: "off", label: "off" });
    assert.deepEqual(autoDecideChip(undefined, false), { tone: "off", label: "off" });
    assert.deepEqual(autoDecideChip(true, true), { tone: "warn", label: "latched" });
    assert.deepEqual(autoDecideChip(true, false), { tone: "ok", label: "on" });
  });
});

describe("ageIsStale", () => {
  it("uses the backend age and does not invent a threshold", () => {
    const now = 1_000_000;
    assert.equal(ageIsStale(now - 10 * 60_000, 15, now), false);
    assert.equal(ageIsStale(now - 20 * 60_000, 15, now), true);
    assert.equal(ageIsStale(now - 20 * 60_000, null, now), null);
    assert.equal(ageIsStale(null, 15, now), null);
  });
});
