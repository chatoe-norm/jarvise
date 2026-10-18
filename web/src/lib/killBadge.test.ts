import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { KILL_UNKNOWN_TEXT, killBadge } from "./killBadge.ts";

const base = {
  paper_only: true,
  live_trading: false,
  kill_switch: false,
  ingest: null,
  rag: null,
  paper: null,
  paper_expire: null,
  risk_caps: {},
};

describe("killBadge", () => {
  it("shows the unknown fail-closed line in danger, never clear", () => {
    const badge = killBadge({
      ...base,
      kill_switch: true,
      kill_switch_state: { known: false, engaged: true, error: "ModuleNotFoundError" },
    });
    assert.equal(badge.kind, "unknown");
    assert.equal(badge.variant, "danger");
    assert.equal(badge.text, KILL_UNKNOWN_TEXT);
    assert.equal(badge.text.includes("CLEAR"), false);
    assert.equal(badge.text.includes("ENGAGED"), false);
  });

  it("follows a known engage and a known clear", () => {
    assert.equal(
      killBadge({ ...base, kill_switch: true, kill_switch_state: { known: true, engaged: true } }).kind,
      "engaged",
    );
    assert.equal(
      killBadge({ ...base, kill_switch: false, kill_switch_state: { known: true, engaged: false } }).kind,
      "clear",
    );
  });
});
