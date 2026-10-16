import { api } from "@/lib/api";
import { modelRoutingRepository } from "@/repositories/modelRoutingRepository";
import type { ModelRoutingMatrix } from "@/types/modelRouting";

export interface ActionResult<T> {
  success: boolean;
  data?: T;
  error?: string;
  revalidatedAtMs?: number;
}

/**
 * Action to fetch and revalidate the OpenRouter Multi-Tier Model Routing Matrix.
 * Follows strict layered architecture and standardized error handling.
 */
export async function syncModelRoutingAction(): Promise<ActionResult<ModelRoutingMatrix>> {
  try {
    const remoteMatrix = await api.getModelRouting();
    modelRoutingRepository.saveCachedMatrix(remoteMatrix);
    return {
      success: true,
      data: remoteMatrix,
      revalidatedAtMs: Date.now(),
    };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.warn("[syncModelRoutingAction] Remote sync failed, falling back to cached matrix:", message);
    const cached = modelRoutingRepository.getCachedMatrix();
    return {
      success: false,
      data: cached,
      error: `โหลดจากเซิร์ฟเวอร์ไม่สำเร็จ ใช้ค่าที่เก็บไว้ในเครื่อง: ${message}`,
      revalidatedAtMs: Date.now(),
    };
  }
}
