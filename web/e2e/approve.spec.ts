"""Optional Playwright smoke — skipped unless PLAYWRIGHT=1 (browsers not in CI)."""

import { test, expect } from "@playwright/test";

test("approve smoke", async ({ page }) => {
  test.skip(!process.env.PLAYWRIGHT, "set PLAYWRIGHT=1 to run");
  await page.goto("http://127.0.0.1:8080/");
  await expect(page.getByText("Jarvise")).toBeVisible();
});
