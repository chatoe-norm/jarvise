import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { errorMessage } from "./errors.ts";

describe("errorMessage", () => {
  it("reads the backend error field instead of dumping JSON", () => {
    const body = JSON.stringify({ ok: false, error: "approval expired", result: { ok: false } });
    assert.equal(errorMessage(body, 409), "approval expired");
  });

  it("reads FastAPI detail strings and validation arrays", () => {
    assert.equal(errorMessage('{"detail":"Auth required"}', 401), "Auth required");
    const arr = JSON.stringify({ detail: [{ loc: ["body", "id"], msg: "field required" }] });
    assert.equal(errorMessage(arr, 422), "field required");
  });

  it("lists risk-cap field errors", () => {
    const body = JSON.stringify({ ok: false, error: "invalid risk caps", field_errors: { max_daily_loss_usd: "must be greater than 0" } });
    assert.equal(errorMessage(body, 400), "max_daily_loss_usd: must be greater than 0");
  });

  it("falls back to the status for empty bodies and HTML pages", () => {
    assert.equal(errorMessage("", 502), "HTTP 502");
    assert.equal(errorMessage("<html><body>Bad gateway</body></html>", 502), "HTTP 502");
  });

  it("keeps short plain-text bodies", () => {
    assert.equal(errorMessage("no database", 404), "no database");
  });
});
