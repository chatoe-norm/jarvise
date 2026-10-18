import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { OPS_ALL_GROUPS, defaultOpsTab } from "./opsTabs.ts";

describe("defaultOpsTab", () => {
  it("lets a valid hash beat the mobile default", () => {
    assert.equal(defaultOpsTab("#risk", true), "risk");
    assert.equal(defaultOpsTab("pipeline", true), "pipeline");
    assert.equal(defaultOpsTab("#ALL", false), "all");
  });

  it("opens View All on mobile when the hash is missing or unknown", () => {
    assert.equal(defaultOpsTab("", true), "all");
    assert.equal(defaultOpsTab("#nope", true), "all");
  });

  it("opens Risk on desktop when the hash is missing or unknown", () => {
    assert.equal(defaultOpsTab("", false), "risk");
    assert.equal(defaultOpsTab("#nope", false), "risk");
  });
});

describe("OPS_ALL_GROUPS", () => {
  it("keeps View All in Safety, Automation, Data, Intelligence order", () => {
    assert.deepEqual(
      OPS_ALL_GROUPS.map((group) => group.id),
      ["safety", "automation", "data", "intelligence"],
    );
  });
});
