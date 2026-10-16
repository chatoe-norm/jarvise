import type {
  DomainScoreMapping,
  DynamicMarketCause,
  ScoreDomain,
  SemiDynamicContext,
} from "@/types/marketContext";
import { semiDynamicContextSchema } from "@/types/marketContext";
import type { NewsHome } from "@/lib/api";
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
  },
  FEAR: {
    domain: "FEAR",
    min: 25,
    max: 49,
    statusLabel: "Fear (กังวล)",
    themeColor: "#FB8C00",
    staticDefinition:
      "ตลาดอยู่ในภาวะกังวล (Fear) นักลงทุนส่วนใหญ่ชะลอการซื้อขายเพื่อประเมินสถานการณ์",
  },
  NEUTRAL: {
    domain: "NEUTRAL",
    min: 50,
    max: 50,
    statusLabel: "Neutral (เป็นกลาง)",
    themeColor: "#FDD835",
    staticDefinition:
      "ตลาดอยู่ในภาวะเป็นกลาง (Neutral) แรงซื้อและแรงขายมีความสมดุล ราคามักวิ่งออกข้างเพื่อสะสมพลัง",
  },
  GREED: {
    domain: "GREED",
    min: 51,
    max: 74,
    statusLabel: "Greed (โลภ)",
    themeColor: "#4CAF50",
    staticDefinition:
      "ตลาดอยู่ในภาวะโลภ (Greed) สินทรัพย์เป็นขาขึ้นที่แข็งแกร่ง นักลงทุนเริ่มไล่ราคาจากอาการกลัวตกขบวน (FOMO)",
  },
  EXTREME_GREED: {
    domain: "EXTREME_GREED",
    min: 75,
    max: 100,
    statusLabel: "Extreme Greed (โลภจัด)",
    themeColor: "#2E7D32",
    staticDefinition:
      "ตลาดอยู่ในภาวะโลภจัด (Extreme Greed) ราคาพุ่งสูงเกินมูลค่าพื้นฐาน และตลาดมีความร้อนแรงเกินไป",
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
   * Composes the SemiDynamicContext: [Static_Definition] + [Dynamic_Market_Cause].
   * Fear & Greed is market context only, so there is no trade or queue suggestion here.
   */
  composeSemiDynamicContext(params: { score: number; news?: NewsHome | null }): SemiDynamicContext {
    const clampedScore = Math.round(Math.max(0, Math.min(100, params.score)));
    const domainMapping = this.getDomainMapping(clampedScore);
    const dynamicCause = this.resolveDynamicMarketCause(params.news);

    const causeText = dynamicCause.bullets.join(" ");
    const fullMessage = `${domainMapping.staticDefinition} ${causeText}`.trim();

    const rawPayload = {
      score: clampedScore,
      domain: domainMapping.domain,
      statusLabel: domainMapping.statusLabel,
      themeColor: domainMapping.themeColor,
      staticDefinition: domainMapping.staticDefinition,
      dynamicMarketCause: dynamicCause,
      fullConcatenatedMessage: fullMessage,
      assembledAtMs: Date.now(),
    };

    // Strict validation with Zod
    return semiDynamicContextSchema.parse(rawPayload);
  }
}

export const marketContextService = new MarketContextService();
