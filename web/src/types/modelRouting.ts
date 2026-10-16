import { z } from "zod";

export const agentRoleEnumSchema = z.enum([
  "DOCTRINE_JUDGE",
  "HEARTBEAT_INGEST",
  "QUANT_ANALYZER",
  "CONTEXT_DIGEST",
  "PAPER_SANDBOX",
]);

export type AgentRole = z.infer<typeof agentRoleEnumSchema>;

export const agentProfileSchema = z.object({
  role: agentRoleEnumSchema,
  key: z.string().min(1),
  name: z.string().min(1),
  emoji: z.string().min(1),
  theme: z.string().default("default"),
  primaryModel: z.string().min(1),
  fallbackModels: z.array(z.string()),
  temperature: z.number().min(0).max(2),
  coreTask: z.string().min(1),
  cacheControl: z.literal("ephemeral").default("ephemeral"),
  compactorEnabled: z.boolean().default(true),
  compactionThresholdKb: z.number().default(300),
});

export type AgentProfile = z.infer<typeof agentProfileSchema>;

export const modelRoutingMatrixSchema = z.object({
  version: z.string().default("2026-10-16"),
  agents: z.record(agentRoleEnumSchema, agentProfileSchema),
  availableModels: z.array(z.string()),
  totalAgents: z.number().int().min(1),
  lastSyncedAtMs: z.number(),
});

export type ModelRoutingMatrix = z.infer<typeof modelRoutingMatrixSchema>;

export const aiExecutionLogSchema = z.object({
  id: z.string(),
  agentRole: agentRoleEnumSchema,
  modelRequested: z.string(),
  modelResolved: z.string(),
  isFallback: z.boolean(),
  promptTokens: z.number().int().nonnegative(),
  completionTokens: z.number().int().nonnegative(),
  cachedTokens: z.number().int().nonnegative().optional(),
  costEstimateUsd: z.number().nonnegative(),
  durationMs: z.number().nonnegative(),
  status: z.enum(["success", "fallback_used", "rate_limited", "failed"]),
  timestampMs: z.number(),
});

export type AIExecutionLog = z.infer<typeof aiExecutionLogSchema>;
