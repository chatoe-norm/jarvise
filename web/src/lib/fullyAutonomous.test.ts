import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { FULLY_AUTONOMOUS_LINE, fullyAutonomousLine } from "./fullyAutonomous.ts";

describe("fullyAutonomousLine", () => {
  it("renders only when auto-decide is on", () => {
    assert.equal(fullyAutonomousLine(false), null);
    assert.equal(fullyAutonomousLine(true), FULLY_AUTONOMOUS_LINE);
  });
});
