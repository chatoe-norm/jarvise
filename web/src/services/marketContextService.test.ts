import test from "node:test";
import assert from "node:assert/strict";
import {
  DOMAIN_MAPPINGS,
  FALLBACK_MARKET_CAUSE,
  MarketContextService,
} from "./marketContextService";
import type { NewsHome } from "../lib/api";
import { semiDynamicContextSchema } from "../types/marketContext";

test("MarketContextService - 5 Score Domains mapping and boundaries", () => {
  const service = new MarketContextService();

  // 1. Extreme Fear (0 - 24)
  assert.equal(service.getDomainMapping(0).domain, "EXTREME_FEAR");
  assert.equal(service.getDomainMapping(12).domain, "EXTREME_FEAR");
  assert.equal(service.getDomainMapping(24).domain, "EXTREME_FEAR");
  assert.equal(service.getDomainMapping(10).themeColor, "#E53935");
  assert.equal(service.getDomainMapping(10).statusLabel, "Extreme Fear (กลัวจัด)");

  // 2. Fear (25 - 49)
  assert.equal(service.getDomainMapping(25).domain, "FEAR");
  assert.equal(service.getDomainMapping(37).domain, "FEAR");
  assert.equal(service.getDomainMapping(49).domain, "FEAR");
  assert.equal(service.getDomainMapping(35).themeColor, "#FB8C00");
  assert.equal(service.getDomainMapping(35).statusLabel, "Fear (กังวล)");

  // 3. Neutral (50)
  assert.equal(service.getDomainMapping(50).domain, "NEUTRAL");
  assert.equal(service.getDomainMapping(50).themeColor, "#FDD835");
  assert.equal(service.getDomainMapping(50).statusLabel, "Neutral (เป็นกลาง)");

  // 4. Greed (51 - 74)
  assert.equal(service.getDomainMapping(51).domain, "GREED");
  assert.equal(service.getDomainMapping(63).domain, "GREED");
  assert.equal(service.getDomainMapping(74).domain, "GREED");
  assert.equal(service.getDomainMapping(63).themeColor, "#4CAF50");
  assert.equal(service.getDomainMapping(63).statusLabel, "Greed (โลภ)");

  // 5. Extreme Greed (75 - 100)
  assert.equal(service.getDomainMapping(75).domain, "EXTREME_GREED");
  assert.equal(service.getDomainMapping(85).domain, "EXTREME_GREED");
  assert.equal(service.getDomainMapping(100).domain, "EXTREME_GREED");
  assert.equal(service.getDomainMapping(85).themeColor, "#2E7D32");
  assert.equal(service.getDomainMapping(85).statusLabel, "Extreme Greed (โลภจัด)");

  // Clamping for out-of-bound inputs
  assert.equal(service.getDomainMapping(-10).domain, "EXTREME_FEAR");
  assert.equal(service.getDomainMapping(120).domain, "EXTREME_GREED");
});

test("MarketContextService - Static definitions match spec", () => {
  const service = new MarketContextService();
  assert.equal(
    service.getDomainMapping(15).staticDefinition,
    "ตลาดอยู่ในภาวะกลัวจัด (Extreme Fear) สินทรัพย์โดนเทขายอย่างรุนแรงจากความตื่นตระหนก",
  );
  assert.ok(service.getDomainMapping(63).staticDefinition.includes("ตลาดอยู่ในภาวะโลภ (Greed)"));
});

test("MarketContextService - Fear & Greed carries no trade or queue advice", () => {
  // Owner rule: F&G is market context only, never a trade signal.
  for (const mapping of Object.values(DOMAIN_MAPPINGS)) {
    const text = JSON.stringify(mapping);
    for (const banned of ["แนะนำ", "Approve", "Approval Queue", "เปิด Buy", "งดเปิดโพสิชัน"]) {
      assert.ok(!text.includes(banned), `${mapping.domain} must not contain "${banned}"`);
    }
  }
});

test("MarketContextService - Dynamic Market Cause extraction & Fallback handling", () => {
  const service = new MarketContextService();

  // Case 1: News with valid lines
  const newsWithLines: NewsHome = {
    ok: true,
    lines: [
      "บริบทข่าว ไม่ใช่สัญญาณ",
      "Macro: ดัชนี CPI ต่ำกว่าคาด → หนุนแรงซื้อสินทรัพย์เสี่ยง (BTC)",
      "Tech: สภาพคล่องไหลเข้า SOL ต่อเนื่อง",
    ],
    generated_at_ms: 1729000000000,
  };
  const cause1 = service.resolveDynamicMarketCause(newsWithLines);
  assert.equal(cause1.bullets.length, 2);
  assert.ok(cause1.bullets[0].includes("CPI"));
  assert.ok(cause1.bullets[1].includes("SOL"));
  assert.ok(cause1.timestampText !== null);

  // Case 2: News with digest_items
  const newsWithDigest: NewsHome = {
    ok: true,
    digest_items: [
      { theme: "Fed", title: "ส่งสัญญาณลดดอกเบี้ย", comment: "ตลาดคริปโตตอบรับเชิงบวก" },
    ],
  };
  const cause2 = service.resolveDynamicMarketCause(newsWithDigest);
  assert.equal(cause2.bullets.length, 1);
  assert.ok(cause2.bullets[0].includes("Fed: ส่งสัญญาณลดดอกเบี้ย — ตลาดคริปโตตอบรับเชิงบวก"));

  // Case 3: Fallback when news is null, undefined, or empty
  const fallbackNull = service.resolveDynamicMarketCause(null);
  assert.deepEqual(fallbackNull.bullets, [FALLBACK_MARKET_CAUSE]);
  assert.equal(fallbackNull.timestampText, null);

  const fallbackEmpty = service.resolveDynamicMarketCause({ ok: true, lines: [] });
  assert.deepEqual(fallbackEmpty.bullets, [FALLBACK_MARKET_CAUSE]);
});

test("MarketContextService - Compose SemiDynamicContext and Zod Schema validation", () => {
  const service = new MarketContextService();

  const news: NewsHome = {
    ok: true,
    lines: [
      "Macro: สัญญาณบวกจากข่าวดัชนีราคาผู้บริโภค สะสมสถานะ Long เหรียญหลัก BTC และ SOL ต่อเนื่อง",
    ],
    generated_at_ms: Date.now(),
  };

  const result = service.composeSemiDynamicContext({ score: 63, news });

  const validated = semiDynamicContextSchema.safeParse(result);
  assert.ok(validated.success, "Result must satisfy semiDynamicContextSchema");

  assert.equal(result.score, 63);
  assert.equal(result.domain, "GREED");
  assert.equal(result.statusLabel, "Greed (โลภ)");
  assert.equal(result.themeColor, "#4CAF50");
  assert.ok(result.dynamicMarketCause.bullets[0].includes("BTC และ SOL"));
  assert.ok(!("systemActionSuggestion" in result));

  // [Static_Definition] + [Dynamic_Market_Cause] only.
  assert.ok(result.fullConcatenatedMessage.startsWith(result.staticDefinition));
  assert.ok(result.fullConcatenatedMessage.endsWith(result.dynamicMarketCause.bullets[0]));
});

test("MarketContextService - backend 'no news' placeholder is not shown as a market cause", () => {
  const service = new MarketContextService();
  const cause = service.resolveDynamicMarketCause({ ok: true, lines: ["ยังไม่มีรายงานข่าว"] });
  assert.deepEqual(cause.bullets, [FALLBACK_MARKET_CAUSE]);
});
