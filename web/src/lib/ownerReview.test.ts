import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { ownerReasonReady, showQueueJump } from "./ownerReview.ts";

describe("ownerReasonReady", () => {
  it("rejects empty, blank, and short reviews", () => {
    assert.equal(ownerReasonReady(""), false);
    assert.equal(ownerReasonReady("   "), false);
    assert.equal(ownerReasonReady("short"), false);
    assert.equal(ownerReasonReady("123456789"), false);
  });

  it("accepts ten characters after trim", () => {
    assert.equal(ownerReasonReady("1234567890"), true);
    assert.equal(ownerReasonReady("  reviewed the halt  "), true);
  });
});

describe("showQueueJump", () => {
  it("stays hidden when the queue is empty or still on screen", () => {
    assert.equal(showQueueJump(0, true), false);
    assert.equal(showQueueJump(2, false), false);
    assert.equal(showQueueJump(2, true), true);
  });
});
