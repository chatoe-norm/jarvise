import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { describeIssues } from "./syncIssues.ts";

describe("describeIssues", () => {
  it("names each failed symbol with its step and reason", () => {
    assert.equal(
      describeIssues([
        { symbol: "BTCUSDT", step: "ingest 4h", exit_code: 2 },
        { symbol: "ETHUSDT", step: "ingest 4h", error: "no candles stored" },
      ]),
      "BTCUSDT (ingest 4h, exit 2), ETHUSDT (ingest 4h: no candles stored)",
    );
  });
});
