import type {
  AgentProfile,
  AgentRole,
  AIExecutionLog,
  ModelRoutingMatrix,
} from "@/types/modelRouting";
import {
  DEFAULT_MODEL_ROUTING_PROFILES,
  type IModelRoutingRepository,
  modelRoutingRepository,
} from "@/repositories/modelRoutingRepository";

export interface ModelPricingTier {
  promptCostPerM: number;
  completionCostPerM: number;
}

export const MODEL_PRICING: Record<string, ModelPricingTier> = {
  "anthropic/claude-sonnet-5": { promptCostPerM: 3.0, completionCostPerM: 15.0 },
  "anthropic/claude-sonnet-4.6": { promptCostPerM: 3.0, completionCostPerM: 15.0 },
  "anthropic/claude-opus-5.5": { promptCostPerM: 15.0, completionCostPerM: 75.0 },
  "deepseek/deepseek-v4.1-flash": { promptCostPerM: 0.14, completionCostPerM: 0.28 },
  "minimax/minimax-m3": { promptCostPerM: 0.2, completionCostPerM: 1.1 },
  "google/gemini-3.8-flash": { promptCostPerM: 0.075, completionCostPerM: 0.3 },
  "deepseek/deepseek-v4-pro": { promptCostPerM: 0.27, completionCostPerM: 1.1 },
  "openai/gpt-5.6-luna": { promptCostPerM: 2.5, completionCostPerM: 10.0 },
  "z-ai/glm-5.3-flash": { promptCostPerM: 0.1, completionCostPerM: 0.2 },
  "nvidia/nemotron-3-ultra-550b-a55b:free": { promptCostPerM: 0.0, completionCostPerM: 0.0 },
  "nvidia/nemotron-3-super:free": { promptCostPerM: 0.0, completionCostPerM: 0.0 },
};

export const SESSION_COMPACTION_THRESHOLD_KB = 300;

export class ModelRoutingService {
  constructor(private repo: IModelRoutingRepository = modelRoutingRepository) {}

  /**
   * Returns current active ModelRoutingMatrix from repository.
   */
  getMatrix(): ModelRoutingMatrix {
    return this.repo.getCachedMatrix();
  }

  /**
   * Retrieves profile for specific agent role.
   */
  getAgentProfile(role: AgentRole): AgentProfile {
    const matrix = this.getMatrix();
    return matrix.agents[role] ?? DEFAULT_MODEL_ROUTING_PROFILES[role];
  }

  /**
   * Resolves model execution chain (primary + fallbacks).
   */
  resolveExecutionChain(role: AgentRole): string[] {
    const profile = this.getAgentProfile(role);
    return [profile.primaryModel, ...profile.fallbackModels];
  }

  /**
   * Returns whether a session of given byte size triggers claw-compactor.
   */
  shouldCompactSession(sessionSizeBytes: number): boolean {
    const sizeKb = sessionSizeBytes / 1024;
    return sizeKb >= SESSION_COMPACTION_THRESHOLD_KB;
  }

  /**
   * Builds system prompt metadata with ephemeral caching headers.
   */
  buildSystemPromptOptions(role: AgentRole): {
    cacheControl: { type: "ephemeral" };
    temperature: number;
  } {
    const profile = this.getAgentProfile(role);
    return {
      cacheControl: { type: "ephemeral" },
      temperature: profile.temperature,
    };
  }

  /**
   * Estimates token cost in USD for a given model and token counts.
   */
  estimateCostUsd(model: string, promptTokens: number, completionTokens: number): number {
    const pricing = MODEL_PRICING[model] || { promptCostPerM: 1.0, completionCostPerM: 2.0 };
    const promptCost = (promptTokens / 1_000_000) * pricing.promptCostPerM;
    const completionCost = (completionTokens / 1_000_000) * pricing.completionCostPerM;
    return Number((promptCost + completionCost).toFixed(6));
  }

  /**
   * Records execution log into repository.
   */
  recordExecution(params: {
    agentRole: AgentRole;
    modelRequested: string;
    modelResolved: string;
    isFallback: boolean;
    promptTokens: number;
    completionTokens: number;
    cachedTokens?: number;
    durationMs: number;
    status: "success" | "fallback_used" | "rate_limited" | "failed";
  }): AIExecutionLog {
    const costEstimateUsd = this.estimateCostUsd(
      params.modelResolved,
      params.promptTokens,
      params.completionTokens,
    );

    const log: AIExecutionLog = {
      id: `exec_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
      agentRole: params.agentRole,
      modelRequested: params.modelRequested,
      modelResolved: params.modelResolved,
      isFallback: params.isFallback,
      promptTokens: params.promptTokens,
      completionTokens: params.completionTokens,
      cachedTokens: params.cachedTokens,
      costEstimateUsd,
      durationMs: params.durationMs,
      status: params.status,
      timestampMs: Date.now(),
    };

    this.repo.appendExecutionLog(log);
    return log;
  }

  /**
   * Returns all recorded execution logs.
   */
  getExecutionLogs(): AIExecutionLog[] {
    return this.repo.getExecutionLogs();
  }
}

export const modelRoutingService = new ModelRoutingService();
