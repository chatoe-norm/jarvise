import { expect, test, type Route } from "@playwright/test";
import { dashboardPayload } from "./dashboardMocks";

async function fulfillJson(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

test("paper page shows the live loss lesson", async ({ page }) => {
  await page.route("**/api/dashboard", async (route) => {
    await fulfillJson(route, dashboardPayload({ pending: false }));
  });
  await page.route("**/api/paper/metrics", async (route) => {
    await fulfillJson(route, {
      ok: true,
      closed_trades: 3,
      wins: 1,
      losses: 2,
      expected_value_ev: -3.33,
      starting_equity: 10000,
      trades: [
        { close_ts: Date.UTC(2026, 9, 10), pnl_usd: -10, symbol: "ETHUSDT" },
        { close_ts: Date.UTC(2026, 9, 12), pnl_usd: -10, symbol: "BTCUSDT" },
      ],
      loss_lesson: {
        closed_trades: 3,
        wins: 1,
        losses: 2,
        by_exit: {
          stop_hit: { n: 1, pnl_usd: -10 },
          flat: { n: 1, pnl_usd: -10 },
        },
        by_symbol: {
          BTCUSDT: { n: 2, pnl_usd: 0 },
          ETHUSDT: { n: 1, pnl_usd: -10 },
        },
        auto_closed: 3,
        auto_ev: -3.33,
        floors_unchanged: true,
      },
    });
  });
  await page.route("**/api/paper", async (route) => {
    if (route.request().url().includes("/metrics")) {
      await route.fallback();
      return;
    }
    await fulfillJson(route, {
      ok: true,
      equity: 9970,
      cash: 10000,
      positions: [],
      orders: [],
    });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Command Control" })).toBeVisible();
  await expect(page.getByText("Equity trajectory")).toHaveCount(0);
  await expect(page.getByText("Win / loss mix")).toHaveCount(0);
  await page.getByRole("link", { name: "Paper" }).click();
  await expect(page.getByText("Win / loss count")).toBeVisible();
  await expect(page.getByText("Expectancy mix")).toHaveCount(0);
  await expect(page.getByText("2500")).toHaveCount(0);
  await expect(page.getByText("บทเรียนจากไม้ที่ปิดแล้ว")).toBeVisible();
  await expect(page.getByText("ปิดแล้ว 3 ไม้ ชนะ 1 ไม้ EV ติดลบ")).toBeVisible();
  await expect(page.getByText(/จุดตัด 1 ไม้/)).toBeVisible();
  await expect(page.getByText(/ปิดตาม FLAT 1 ไม้/)).toBeVisible();
  await expect(page.getByText(/ETHUSDT กินผลขาดทุนมากสุด/)).toBeVisible();
  await expect(page.getByText(/จากเกณฑ์ประมาณ 8/)).toBeVisible();
  await expect(page.getByText(/พื้นความมั่นใจ 0\.55\/0\.70 จึงยังอยู่/)).toBeVisible();
  await expect(page.getByText(/ไม่ใช่เหตุให้เปลี่ยนสูตร/)).toBeVisible();
});
