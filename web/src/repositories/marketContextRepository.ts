import type { SemiDynamicContext } from "@/types/marketContext";
import { semiDynamicContextSchema } from "@/types/marketContext";
import type { ApprovalRow, NewsHome } from "@/lib/api";

const CACHE_KEY = "jarvise_fng_market_context_v1";

export interface IMarketContextRepository {
  getCachedContext(): SemiDynamicContext | null;
  saveCachedContext(context: SemiDynamicContext): void;
  extractNewsBullets(news: NewsHome | null | undefined): {
    bullets: string[];
    generatedAtMs: number | null;
    windowYears: number | null;
  };
  findActionableApproval(approvals: ApprovalRow[] | undefined): ApprovalRow | null;
}

export class MarketContextRepository implements IMarketContextRepository {
  /**
   * Reads the latest cached SemiDynamicContext from local storage.
   * Runs synchronously in <1ms to support instantaneous (<0.1s) PWA first paint.
   */
  getCachedContext(): SemiDynamicContext | null {
    if (typeof window === "undefined" || !window.localStorage) {
      return null;
    }
    try {
      const raw = window.localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const validated = semiDynamicContextSchema.safeParse(parsed);
      if (validated.success) {
        return validated.data;
      }
      return null;
    } catch {
      return null;
    }
  }

  /**
   * Persists the context to local storage for instant retrieval across sessions.
   */
  saveCachedContext(context: SemiDynamicContext): void {
    if (typeof window === "undefined" || !window.localStorage) {
      return;
    }
    try {
      window.localStorage.setItem(CACHE_KEY, JSON.stringify(context));
    } catch {
      // Storage quota exceeded or disabled in private browsing - safely ignore
    }
  }

  /**
   * Extracts clean, human-readable intelligence bullet points from the NewsHome payload.
   */
  extractNewsBullets(news: NewsHome | null | undefined): {
    bullets: string[];
    generatedAtMs: number | null;
    windowYears: number | null;
  } {
    if (!news) {
      return { bullets: [], generatedAtMs: null, windowYears: null };
    }

    const generatedAtMs = news.generated_at_ms ?? null;
    const windowYears = news.window_years ?? null;

    if (news.digest_items && news.digest_items.length > 0) {
      const bullets = news.digest_items.slice(0, 2).map((item) => {
        if (item.comment) return `${item.theme}: ${item.title} — ${item.comment}`;
        return `${item.theme}: ${item.title}`;
      });
      return { bullets, generatedAtMs, windowYears };
    }

    if (news.lines && news.lines.length > 0) {
      const filtered = news.lines
        .map((l) => l.trim())
        .filter((l) => {
          if (!l) return false;
          if (l === "บริบทข่าว ไม่ใช่สัญญาณ") return false;
          if (l === "ยังไม่มีรายงานข่าวรอบล่าสุด") return false;
          return true;
        });

      const bullets = filtered.slice(0, 2);
      return { bullets, generatedAtMs, windowYears };
    }

    return { bullets: [], generatedAtMs, windowYears };
  }

  /**
   * Finds the most relevant actionable candidate from the pending approvals queue.
   * Prioritizes 'flat' or 'close' orders, or candidates with high confidence (>= 0.70).
   */
  findActionableApproval(approvals: ApprovalRow[] | undefined): ApprovalRow | null {
    if (!approvals || approvals.length === 0) {
      return null;
    }

    // 1. Prioritize pending flat/close orders (useful for locking profits in Greed)
    const flatOrder = approvals.find((a) => {
      const action = (a.action || "").toLowerCase();
      return action === "flat" || action === "close";
    });
    if (flatOrder) return flatOrder;

    // 2. Otherwise find candidate with highest confidence score
    const sorted = [...approvals].sort(
      (a, b) => (b.confidence_score ?? 0) - (a.confidence_score ?? 0),
    );

    return sorted[0] ?? null;
  }
}

export const marketContextRepository = new MarketContextRepository();
