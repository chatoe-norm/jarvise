import test from "node:test";
import assert from "node:assert/strict";
import {
  DOMAIN_MAPPINGS,
  FALLBACK_MARKET_CAUSE,
  MarketContextService,
} from "./marketContextService";
import type { ApprovalRow, NewsHome } from "../lib/api";
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

test("MarketContextService - Static definitions and default action templates match spec", () => {
  const service = new MarketContextService();

  const extremeFear = service.getDomainMapping(15);
  assert.equal(
    extremeFear.staticDefinition,
    "ตลาดอยู่ในภาวะกลัวจัด (Extreme Fear) สินทรัพย์โดนเทขายอย่างรุนแรงจากความตื่นตระหนก",
  );
  assert.equal(
    extremeFear.defaultActionTemplate,
    "แนะนำ: ตรวจสอบสถานะพอร์ตอย่างระมัดระวัง หลีกเลี่ยงการใช้อารมณ์ Panic Sell และจับตาดูสัญญาณแนวรับสำคัญในระบบ",
  );

  const fear = service.getDomainMapping(30);
  assert.equal(
    fear.staticDefinition,
    "ตลาดอยู่ในภาวะกังวล (Fear) นักลงทุนส่วนใหญ่ชะลอการซื้อขายเพื่อประเมินสถานการณ์",
  );
  assert.equal(
    fear.defaultActionTemplate,
    "แนะนำ: ป้องกันความเสี่ยงด้วยการถือเงินสด (Cash) บางส่วน และรอสัญญาณที่มีค่า Conf สูงกว่า 0.70 ในระบบก่อนพิจารณาเข้าออเดอร์",
  );

  const neutral = service.getDomainMapping(50);
  assert.equal(
    neutral.staticDefinition,
    "ตลาดอยู่ในภาวะเป็นกลาง (Neutral) แรงซื้อและแรงขายมีความสมดุล ราคามักวิ่งออกข้างเพื่อสะสมพลัง",
  );
  assert.equal(
    neutral.defaultActionTemplate,
    "แนะนำ: วางแผนกลยุทธ์รับ-ต้านล่วงหน้า และติดตามการอัปเดตจากโมดูล Intelligence & News Digest เพื่อจับจังหวะการ Breakout",
  );

  const greed = service.getDomainMapping(60);
  assert.equal(
    greed.staticDefinition,
    "ตลาดอยู่ในภาวะโลภ (Greed) สินทรัพย์เป็นขาขึ้นที่แข็งแกร่ง นักลงทุนเริ่มไล่ราคาจากอาการกลัวตกขบวน (FOMO)",
  );
  assert.equal(
    greed.defaultActionTemplate,
    "แนะนำ: หลีกเลี่ยงการเปิด Buy ไล่ราคาที่ยอดพอยท์ และตรวจสอบ Approval Queue ด้านบนเพื่อพิจารณา Approve คำสั่ง flat เพื่อทยอยล็อกกำไร",
  );

  const extremeGreed = service.getDomainMapping(80);
  assert.equal(
    extremeGreed.staticDefinition,
    "ตลาดอยู่ในภาวะโลภจัด (Extreme Greed) ราคาพุ่งสูงเกินมูลค่าพื้นฐาน และตลาดมีความร้อนแรงเกินไป",
  );
  assert.equal(
    extremeGreed.defaultActionTemplate,
    "แนะนำ: ตลาดเสี่ยงต่อการเกิดการปรับฐานลึก (Correction) ในไม่ช้า ควรงดเปิดโพสิชันใหม่เพิ่ม และเน้นการปกป้องเงินทุนเป็นหลัก",
  );
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

test("MarketContextService - Dynamic Action Suggestion from Approval Queue", () => {
  const service = new MarketContextService();

  // 1. Greed with pending flat order
  const approvalsGreedFlat: ApprovalRow[] = [
    {
      id: "app-1",
      symbol: "SOLUSDT",
      action: "flat",
      confidence_score: 0.75,
    },
  ];
  const suggestionGreedFlat = service.resolveActionSuggestion(
    DOMAIN_MAPPINGS.GREED,
    approvalsGreedFlat,
  );
  assert.equal(suggestionGreedFlat.isDynamic, true);
  assert.equal(suggestionGreedFlat.targetSymbol, "SOLUSDT");
  assert.equal(suggestionGreedFlat.targetConf, 0.75);
  assert.ok(
    suggestionGreedFlat.suggestion.includes(
      "ตรวจสอบ Approval Queue ด้านบนสำหรับเหรียญ SOLUSDT (Conf 0.75) เพื่อพิจารณา Approve คำสั่ง Flat ล็อกกำไร",
    ),
  );

  // 2. Fear with high confidence candidate (>= 0.70)
  const approvalsFearHighConf: ApprovalRow[] = [
    {
      id: "app-2",
      symbol: "BTCUSDT",
      action: "buy",
      confidence_score: 0.78,
    },
  ];
  const suggestionFearHigh = service.resolveActionSuggestion(
    DOMAIN_MAPPINGS.FEAR,
    approvalsFearHighConf,
  );
  assert.equal(suggestionFearHigh.isDynamic, true);
  assert.equal(suggestionFearHigh.targetSymbol, "BTCUSDT");
  assert.ok(suggestionFearHigh.suggestion.includes("ผ่านเกณฑ์ High Confidence"));

  // 3. Fear with low confidence candidate (< 0.70)
  const approvalsFearLowConf: ApprovalRow[] = [
    {
      id: "app-3",
      symbol: "ETHUSDT",
      action: "buy",
      confidence_score: 0.58,
    },
  ];
  const suggestionFearLow = service.resolveActionSuggestion(
    DOMAIN_MAPPINGS.FEAR,
    approvalsFearLowConf,
  );
  assert.equal(suggestionFearLow.isDynamic, true);
  assert.ok(suggestionFearLow.suggestion.includes("ยังไม่ถึงเกณฑ์ 0.70"));

  // 4. Neutral with candidate
  const approvalsNeutral: ApprovalRow[] = [
    {
      id: "app-4",
      symbol: "ETHUSDT",
      action: "buy",
      confidence_score: 0.62,
    },
  ];
  const suggestionNeutral = service.resolveActionSuggestion(
    DOMAIN_MAPPINGS.NEUTRAL,
    approvalsNeutral,
  );
  assert.equal(suggestionNeutral.isDynamic, true);
  assert.equal(suggestionNeutral.targetSymbol, "ETHUSDT");
  assert.ok(suggestionNeutral.suggestion.includes("ETHUSDT (Conf 0.62)"));
  assert.ok(suggestionNeutral.suggestion.includes("ติดตามการ Breakout"));

  // 5. Extreme Greed with non-flat candidate
  const approvalsExtremeGreed: ApprovalRow[] = [
    {
      id: "app-5",
      symbol: "SOLUSDT",
      action: "buy",
      confidence_score: 0.82,
    },
  ];
  const suggestionExtremeGreed = service.resolveActionSuggestion(
    DOMAIN_MAPPINGS.EXTREME_GREED,
    approvalsExtremeGreed,
  );
  assert.equal(suggestionExtremeGreed.isDynamic, true);
  assert.ok(suggestionExtremeGreed.suggestion.includes("หลีกเลี่ยงการเปิด Buy ไล่ราคาที่ยอดพอยท์"));

  // 6. Queue is empty -> returns defaultActionTemplate
  const suggestionEmpty = service.resolveActionSuggestion(DOMAIN_MAPPINGS.GREED, []);
  assert.equal(suggestionEmpty.isDynamic, false);
  assert.equal(suggestionEmpty.targetSymbol, null);
  assert.equal(suggestionEmpty.suggestion, DOMAIN_MAPPINGS.GREED.defaultActionTemplate);
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

  const approvals: ApprovalRow[] = [
    {
      id: "app-sol",
      symbol: "SOLUSDT",
      action: "flat",
      confidence_score: 0.75,
    },
  ];

  const result = service.composeSemiDynamicContext({
    score: 63,
    news,
    approvals,
  });

  // Verify Zod parsing
  const validated = semiDynamicContextSchema.safeParse(result);
  assert.ok(validated.success, "Result must satisfy semiDynamicContextSchema");

  assert.equal(result.score, 63);
  assert.equal(result.domain, "GREED");
  assert.equal(result.statusLabel, "Greed (โลภ)");
  assert.equal(result.themeColor, "#4CAF50");
  assert.ok(result.staticDefinition.includes("ตลาดอยู่ในภาวะโลภ (Greed)"));
  assert.ok(result.dynamicMarketCause.bullets[0].includes("BTC และ SOL"));
  assert.ok(result.systemActionSuggestion.suggestion.includes("SOLUSDT (Conf 0.75)"));

  // Verify full concatenation structure: [Static_Definition] + [Dynamic_Market_Cause] + [System_Action_Suggestion]
  assert.ok(result.fullConcatenatedMessage.startsWith(result.staticDefinition));
  assert.ok(result.fullConcatenatedMessage.includes(result.dynamicMarketCause.bullets[0]));
  assert.ok(result.fullConcatenatedMessage.endsWith(result.systemActionSuggestion.suggestion));
});
