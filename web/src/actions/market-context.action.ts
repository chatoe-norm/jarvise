import { marketContextRepository } from "@/repositories/marketContextRepository";
import { marketContextService } from "@/services/marketContextService";
import type { SemiDynamicContext } from "@/types/marketContext";
import type { ApprovalRow, NewsHome } from "@/lib/api";

export interface MarketContextActionResult {
  success: boolean;
  data?: SemiDynamicContext;
  error?: string;
  revalidatedAtMs?: number;
}

/**
 * Action to recompute and cache the 3-part Semi-Dynamic Market Context.
 * [Static_Definition] + [Dynamic_Market_Cause] + [System_Action_Suggestion]
 */
export async function revalidateMarketContextAction(params: {
  score: number;
  news?: NewsHome | null;
  approvals?: ApprovalRow[];
}): Promise<MarketContextActionResult> {
  try {
    const context = marketContextService.composeSemiDynamicContext(params);
    marketContextRepository.saveCachedContext(context);
    return {
      success: true,
      data: context,
      revalidatedAtMs: Date.now(),
    };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("[revalidateMarketContextAction] Failed to compose context:", message);
    const cached = marketContextRepository.getCachedContext();
    return {
      success: false,
      data: cached ?? undefined,
      error: message,
      revalidatedAtMs: Date.now(),
    };
  }
}
