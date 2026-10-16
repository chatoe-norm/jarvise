import type { SemiDynamicContext } from "@/types/marketContext";
import { semiDynamicContextSchema } from "@/types/marketContext";
import type { NewsHome } from "@/lib/api";

// v2: context no longer carries a trade suggestion; v1 entries are ignored.
const CACHE_KEY = "jarvise_fng_market_context_v2";
/** Cached context only bridges first paint; older than this it is not shown at all. */
export const CONTEXT_CACHE_TTL_MS = 30 * 60 * 1000;

export interface IMarketContextRepository {
  getCachedContext(nowMs?: number): SemiDynamicContext | null;
  saveCachedContext(context: SemiDynamicContext): void;
  extractNewsBullets(news: NewsHome | null | undefined): {
    bullets: string[];
    generatedAtMs: number | null;
    windowYears: number | null;
  };
}

export class MarketContextRepository implements IMarketContextRepository {
  /**
   * Reads the latest cached SemiDynamicContext from local storage.
   * Runs synchronously in <1ms to support instantaneous (<0.1s) PWA first paint.
   */
  getCachedContext(nowMs: number = Date.now()): SemiDynamicContext | null {
    if (typeof window === "undefined" || !window.localStorage) {
      return null;
    }
    try {
      const raw = window.localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const validated = semiDynamicContextSchema.safeParse(parsed);
      if (validated.success && nowMs - validated.data.assembledAtMs <= CONTEXT_CACHE_TTL_MS) {
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
          // Backend placeholder is "ยังไม่มีรายงานข่าว"; older copy added "รอบล่าสุด".
          if (l.startsWith("ยังไม่มีรายงานข่าว")) return false;
          return true;
        });

      const bullets = filtered.slice(0, 2);
      return { bullets, generatedAtMs, windowYears };
    }

    return { bullets: [], generatedAtMs, windowYears };
  }
}

export const marketContextRepository = new MarketContextRepository();
