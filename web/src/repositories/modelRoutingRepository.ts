import type {
  AgentProfile,
  AgentRole,
  AIExecutionLog,
  ModelRoutingMatrix,
} from "@/types/modelRouting";
import { modelRoutingMatrixSchema } from "@/types/modelRouting";

const CACHE_KEY = "jarvise_model_routing_matrix_v1";
const LOGS_KEY = "jarvise_ai_execution_logs_v1";
const MAX_LOGS = 100;

export const DEFAULT_MODEL_ROUTING_PROFILES: Record<AgentRole, AgentProfile> = {
  DOCTRINE_JUDGE: {
    role: "DOCTRINE_JUDGE",
    key: "doctrine_judge",
    name: "Doctrine Judge",
    emoji: "⚖️",
    theme: "doctrine",
    primaryModel: "anthropic/claude-sonnet-5",
    fallbackModels: [
      "anthropic/claude-sonnet-4.6",
      "anthropic/claude-opus-5.5",
    ],
    temperature: 0.0,
    coreTask: "ตรวจสอบกฎ 1% Risk, Token Audit Veto (HIGH risk -> FLAT), และเงื่อนไข Pullback 6 ข้อ",
    cacheControl: "ephemeral",
    compactorEnabled: true,
    compactionThresholdKb: 300,
  },
  HEARTBEAT_INGEST: {
    role: "HEARTBEAT_INGEST",
    key: "heartbeat_ingest",
    name: "Heartbeat Ingest",
    emoji: "💓",
    theme: "ingest",
    primaryModel: "deepseek/deepseek-v4.1-flash",
    fallbackModels: [
      "minimax/minimax-m3",
      "google/gemini-3.8-flash",
    ],
    temperature: 0.1,
    coreTask: "รันสแกนตลาด 24/7 รอบละ 30 นาที ดึงข้อมูล TradingView, CMC, และ CoinGlass V4 API",
    cacheControl: "ephemeral",
    compactorEnabled: true,
    compactionThresholdKb: 300,
  },
  QUANT_ANALYZER: {
    role: "QUANT_ANALYZER",
    key: "quant_analyzer",
    name: "Quant Analyzer",
    emoji: "📐",
    theme: "quant",
    primaryModel: "deepseek/deepseek-v4-pro",
    fallbackModels: [
      "openai/gpt-5.6-luna",
      "anthropic/claude-sonnet-5",
    ],
    temperature: 0.0,
    coreTask: "คำนวณสมการ EV, ระยะ Stop-Loss (>= 1.5x ATR), และสร้าง JSON Signal",
    cacheControl: "ephemeral",
    compactorEnabled: true,
    compactionThresholdKb: 300,
  },
  CONTEXT_DIGEST: {
    role: "CONTEXT_DIGEST",
    key: "context_digest",
    name: "Context Digest",
    emoji: "📰",
    theme: "context",
    primaryModel: "google/gemini-3.8-flash",
    fallbackModels: [
      "z-ai/glm-5.3-flash",
      "deepseek/deepseek-v4.1-flash",
    ],
    temperature: 0.3,
    coreTask: "สรุปข่าวสาร Macro และสร้างข้อความภาษาไทยแบบ Semi-Dynamic สำหรับ Fear & Greed Index",
    cacheControl: "ephemeral",
    compactorEnabled: true,
    compactionThresholdKb: 300,
  },
  PAPER_SANDBOX: {
    role: "PAPER_SANDBOX",
    key: "paper_sandbox",
    name: "Paper Sandbox",
    emoji: "🧪",
    theme: "sandbox",
    primaryModel: "nvidia/nemotron-3-ultra-550b-a55b:free",
    fallbackModels: [
      "nvidia/nemotron-3-super:free",
      "google/gemini-3.8-flash",
    ],
    temperature: 0.1,
    coreTask: "โหมดทดสอบระบบจำลอง (Paper Trading) แบบไม่เสียค่าใช้จ่าย",
    cacheControl: "ephemeral",
    compactorEnabled: true,
    compactionThresholdKb: 300,
  },
};

export const DEFAULT_MODEL_ROUTING_MATRIX: ModelRoutingMatrix = {
  version: "2026-10-16",
  agents: DEFAULT_MODEL_ROUTING_PROFILES,
  availableModels: [
    "anthropic/claude-sonnet-5",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-5.5",
    "deepseek/deepseek-v4.1-flash",
    "minimax/minimax-m3",
    "google/gemini-3.8-flash",
    "deepseek/deepseek-v4-pro",
    "openai/gpt-5.6-luna",
    "z-ai/glm-5.3-flash",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super:free",
  ],
  totalAgents: 5,
  lastSyncedAtMs: Date.now(),
};

export interface IModelRoutingRepository {
  getCachedMatrix(): ModelRoutingMatrix;
  saveCachedMatrix(matrix: ModelRoutingMatrix): void;
  getExecutionLogs(): AIExecutionLog[];
  appendExecutionLog(log: AIExecutionLog): void;
  clearExecutionLogs(): void;
}

export class ModelRoutingRepository implements IModelRoutingRepository {
  /**
   * Safely loads the ModelRoutingMatrix from local storage or returns the default matrix.
   */
  getCachedMatrix(): ModelRoutingMatrix {
    try {
      if (typeof window === "undefined" || !window.localStorage) {
        return DEFAULT_MODEL_ROUTING_MATRIX;
      }
      const raw = window.localStorage.getItem(CACHE_KEY);
      if (!raw) return DEFAULT_MODEL_ROUTING_MATRIX;
      const parsed = JSON.parse(raw);
      const validated = modelRoutingMatrixSchema.safeParse(parsed);
      if (validated.success) {
        return validated.data;
      }
      return DEFAULT_MODEL_ROUTING_MATRIX;
    } catch (err) {
      console.warn("[ModelRoutingRepository] Failed to read cached matrix:", err);
      return DEFAULT_MODEL_ROUTING_MATRIX;
    }
  }

  /**
   * Persists matrix into localStorage with standard error handling.
   */
  saveCachedMatrix(matrix: ModelRoutingMatrix): void {
    try {
      if (typeof window === "undefined" || !window.localStorage) return;
      window.localStorage.setItem(CACHE_KEY, JSON.stringify(matrix));
    } catch (err) {
      console.warn("[ModelRoutingRepository] Failed to save cached matrix:", err);
    }
  }

  /**
   * Retrieves recorded AI usage execution logs.
   */
  getExecutionLogs(): AIExecutionLog[] {
    try {
      if (typeof window === "undefined" || !window.localStorage) return [];
      const raw = window.localStorage.getItem(LOGS_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
      console.warn("[ModelRoutingRepository] Failed to read logs:", err);
      return [];
    }
  }

  /**
   * Appends execution log, capping total stored entries to MAX_LOGS.
   */
  appendExecutionLog(log: AIExecutionLog): void {
    try {
      if (typeof window === "undefined" || !window.localStorage) return;
      const existing = this.getExecutionLogs();
      const updated = [log, ...existing].slice(0, MAX_LOGS);
      window.localStorage.setItem(LOGS_KEY, JSON.stringify(updated));
    } catch (err) {
      console.warn("[ModelRoutingRepository] Failed to append log:", err);
    }
  }

  /**
   * Clears all stored execution logs.
   */
  clearExecutionLogs(): void {
    try {
      if (typeof window === "undefined" || !window.localStorage) return;
      window.localStorage.removeItem(LOGS_KEY);
    } catch (err) {
      console.warn("[ModelRoutingRepository] Failed to clear logs:", err);
    }
  }
}

export const modelRoutingRepository = new ModelRoutingRepository();
