import type {
  DomainScoreMapping,
  DynamicMarketCause,
  ScoreDomain,
  SemiDynamicContext,
  SystemActionSuggestion,
} from "@/types/marketContext";
import { semiDynamicContextSchema } from "@/types/marketContext";
import type { ApprovalRow, NewsHome } from "@/lib/api";
import {
  type IMarketContextRepository,
  marketContextRepository,
} from "@/repositories/marketContextRepository";

export const DOMAIN_MAPPINGS: Record<ScoreDomain, DomainScoreMapping> = {
  EXTREME_FEAR: {
    domain: "EXTREME_FEAR",
    min: 0,
    max: 24,
    statusLabel: "Extreme Fear (กลัวจัด)",
    themeColor: "#E53935",
    staticDefinition:
      "ตลาดอยู่ในภาวะกลัวจัด (Extreme Fear) สินทรัพย์โดนเทขายอย่างรุนแรงจากความตื่นตระหนก",
    defaultActionTemplate:
      "แนะนำ: ตรวจสอบสถานะพอร์ตอย่างระมัดระวัง หลีกเลี่ยงการใช้อารมณ์ Panic Sell และจับตาดูสัญญาณแนวรับสำคัญในระบบ",
  },
  FEAR: {
    domain: "FEAR",
    min: 25,
    max: 49,
    statusLabel: "Fear (กังวล)",
    themeColor: "#FB8C00",
    staticDefinition:
      "ตลาดอยู่ในภาวะกังวล (Fear) นักลงทุนส่วนใหญ่ชะลอการซื้อขายเพื่อประเมินสถานการณ์",
    defaultActionTemplate:
      "แนะนำ: ป้องกันความเสี่ยงด้วยการถือเงินสด (Cash) บางส่วน และรอสัญญาณที่มีค่า Conf สูงกว่า 0.70 ในระบบก่อนพิจารณาเข้าออเดอร์",
  },
  NEUTRAL: {
    domain: "NEUTRAL",
    min: 50,
    max: 50,
    statusLabel: "Neutral (เป็นกลาง)",
    themeColor: "#FDD835",
    staticDefinition:
      "ตลาดอยู่ในภาวะเป็นกลาง (Neutral) แรงซื้อและแรงขายมีความสมดุล ราคามักวิ่งออกข้างเพื่อสะสมพลัง",
    defaultActionTemplate:
      "แนะนำ: วางแผนกลยุทธ์รับ-ต้านล่วงหน้า และติดตามการอัปเดตจากโมดูล Intelligence & News Digest เพื่อจับจังหวะการ Breakout",
  },
  GREED: {
    domain: "GREED",
    min: 51,
    max: 74,
    statusLabel: "Greed (โลภ)",
    themeColor: "#4CAF50",
    staticDefinition:
      "ตลาดอยู่ในภาวะโลภ (Greed) สินทรัพย์เป็นขาขึ้นที่แข็งแกร่ง นักลงทุนเริ่มไล่ราคาจากอาการกลัวตกขบวน (FOMO)",
    defaultActionTemplate:
      "แนะนำ: หลีกเลี่ยงการเปิด Buy ไล่ราคาที่ยอดพอยท์ และตรวจสอบ Approval Queue ด้านบนเพื่อพิจารณา Approve คำสั่ง flat เพื่อทยอยล็อกกำไร",
  },
  EXTREME_GREED: {
    domain: "EXTREME_GREED",
    min: 75,
    max: 100,
    statusLabel: "Extreme Greed (โลภจัด)",
    themeColor: "#2E7D32",
    staticDefinition:
      "ตลาดอยู่ในภาวะโลภจัด (Extreme Greed) ราคาพุ่งสูงเกินมูลค่าพื้นฐาน และตลาดมีความร้อนแรงเกินไป",
    defaultActionTemplate:
      "แนะนำ: ตลาดเสี่ยงต่อการเกิดการปรับฐานลึก (Correction) ในไม่ช้า ควรงดเปิดโพสิชันใหม่เพิ่ม และเน้นการปกป้องเงินทุนเป็นหลัก",
  },
};

export const FALLBACK_MARKET_CAUSE = "ยังไม่มีรายงานข่าวหรือปัจจัยมหภาคใหม่ที่มีนัยสำคัญ";

export class MarketContextService {
  constructor(private repo: IMarketContextRepository = marketContextRepository) {}

  /**
   * Clamps score into [0, 100] and maps to the appropriate ScoreDomain.
   */
  getDomainMapping(score: number): DomainScoreMapping {
    const clamped = Math.round(Math.max(0, Math.min(100, score)));
    if (clamped <= 24) return DOMAIN_MAPPINGS.EXTREME_FEAR;
    if (clamped <= 49) return DOMAIN_MAPPINGS.FEAR;
    if (clamped === 50) return DOMAIN_MAPPINGS.NEUTRAL;
    if (clamped <= 74) return DOMAIN_MAPPINGS.GREED;
    return DOMAIN_MAPPINGS.EXTREME_GREED;
  }

  /**
   * Extracts dynamic market causes from news digest with safe fallback.
   */
  resolveDynamicMarketCause(news: NewsHome | null | undefined): DynamicMarketCause {
    const extracted = this.repo.extractNewsBullets(news);
    const bullets = extracted.bullets.length > 0 ? extracted.bullets : [FALLBACK_MARKET_CAUSE];

    let timestampText: string | null = null;
    if (extracted.generatedAtMs) {
      const date = new Date(extracted.generatedAtMs);
      timestampText = date.toLocaleTimeString("th-TH", {
        hour: "2-digit",
        minute: "2-digit",
        day: "2-digit",
        month: "short",
      });
    }

    return {
      bullets,
      timestampText,
      generatedAtMs: extracted.generatedAtMs,
    };
  }

  /**
   * Generates dynamic actionable recommendation based on current domain and pending approval queue candidates.
   */
  resolveActionSuggestion(
    domainMapping: DomainScoreMapping,
    approvals: ApprovalRow[] | undefined,
  ): SystemActionSuggestion {
    const candidate = this.repo.findActionableApproval(approvals);

    if (!candidate || !candidate.symbol) {
      return {
        suggestion: domainMapping.defaultActionTemplate,
        targetSymbol: null,
        targetConf: null,
        isDynamic: false,
      };
    }

    const symbol = candidate.symbol;
    const confVal = typeof candidate.confidence_score === "number" ? candidate.confidence_score : null;
    const confStr = confVal !== null ? confVal.toFixed(2) : "N/A";
    const action = (candidate.action || "").toLowerCase();

    switch (domainMapping.domain) {
      case "GREED":
      case "EXTREME_GREED": {
        if (action === "flat" || action === "close") {
          return {
            suggestion: `แนะนำ: หลีกเลี่ยงการเปิด Buy ไล่ราคา และตรวจสอบ Approval Queue ด้านบนสำหรับเหรียญ ${symbol} (Conf ${confStr}) เพื่อพิจารณา Approve คำสั่ง Flat ล็อกกำไร`,
            targetSymbol: symbol,
            targetConf: confVal,
            isDynamic: true,
          };
        }
        return {
          suggestion: `แนะนำ: หลีกเลี่ยงการเปิด Buy ไล่ราคาที่ยอดพอยท์ และตรวจสอบ Approval Queue ด้านบนสำหรับเหรียญ ${symbol} (Conf ${confStr}) เพื่อประเมินความเสี่ยงและพิจารณาตัดสินใจอย่างรอบคอบ`,
          targetSymbol: symbol,
          targetConf: confVal,
          isDynamic: true,
        };
      }
      case "EXTREME_FEAR":
      case "FEAR": {
        if (confVal !== null && confVal >= 0.70) {
          return {
            suggestion: `แนะนำ: แม้ตลาดจะกังวล ตรวจสอบ Approval Queue ด้านบนสำหรับเหรียญ ${symbol} ซึ่งผ่านเกณฑ์ High Confidence (Conf ${confStr}) พร้อมตั้ง Stop ตามวินัยเคร่งครัด`,
            targetSymbol: symbol,
            targetConf: confVal,
            isDynamic: true,
          };
        }
        return {
          suggestion: `แนะนำ: ป้องกันความเสี่ยงด้วยการถือเงินสด (Cash) บางส่วน รายการใน Approval Queue สำหรับเหรียญ ${symbol} มีค่า Conf (${confStr}) ยังไม่ถึงเกณฑ์ 0.70 ควรงดไล่เปิดออเดอร์`,
          targetSymbol: symbol,
          targetConf: confVal,
          isDynamic: true,
        };
      }
      case "NEUTRAL": {
        return {
          suggestion: `แนะนำ: วางแผนกลยุทธ์รับ-ต้านล่วงหน้า ตรวจสอบ Approval Queue ด้านบนสำหรับเหรียญ ${symbol} (Conf ${confStr}) และติดตามการ Breakout`,
          targetSymbol: symbol,
          targetConf: confVal,
          isDynamic: true,
        };
      }
    }
  }

  /**
   * Composes the SemiDynamicContext by concatenating:
   * [Static_Definition] + [Dynamic_Market_Cause] + [System_Action_Suggestion]
   */
  composeSemiDynamicContext(params: {
    score: number;
    news?: NewsHome | null;
    approvals?: ApprovalRow[];
  }): SemiDynamicContext {
    const clampedScore = Math.round(Math.max(0, Math.min(100, params.score)));
    const domainMapping = this.getDomainMapping(clampedScore);
    const dynamicCause = this.resolveDynamicMarketCause(params.news);
    const actionSuggestion = this.resolveActionSuggestion(domainMapping, params.approvals);

    const causeText = dynamicCause.bullets.join(" ");
    const fullMessage = `${domainMapping.staticDefinition} ${causeText} ${actionSuggestion.suggestion}`.trim();

    const rawPayload = {
      score: clampedScore,
      domain: domainMapping.domain,
      statusLabel: domainMapping.statusLabel,
      themeColor: domainMapping.themeColor,
      staticDefinition: domainMapping.staticDefinition,
      dynamicMarketCause: dynamicCause,
      systemActionSuggestion: actionSuggestion,
      fullConcatenatedMessage: fullMessage,
      assembledAtMs: Date.now(),
    };

    // Strict validation with Zod
    return semiDynamicContextSchema.parse(rawPayload);
  }
}

export const marketContextService = new MarketContextService();
