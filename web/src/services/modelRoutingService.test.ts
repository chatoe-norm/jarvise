import test from "node:test";
import assert from "node:assert/strict";
import {
  ModelRoutingService,
  MODEL_PRICING,
  SESSION_COMPACTION_THRESHOLD_KB,
} from "./modelRoutingService";
import {
  DEFAULT_MODEL_ROUTING_PROFILES,
  type IModelRoutingRepository,
} from "../repositories/modelRoutingRepository";
import type {
  AIExecutionLog,
  ModelRoutingMatrix,
} from "../types/modelRouting";

class MockModelRoutingRepository implements IModelRoutingRepository {
  private matrix: ModelRoutingMatrix = {
    version: "2026-10-16",
    agents: DEFAULT_MODEL_ROUTING_PROFILES,
    availableModels: ["anthropic/claude-sonnet-5"],
    totalAgents: 5,
    lastSyncedAtMs: 123456789,
  };
  private logs: AIExecutionLog[] = [];

  getCachedMatrix(): ModelRoutingMatrix {
    return this.matrix;
  }
  saveCachedMatrix(matrix: ModelRoutingMatrix): void {
    this.matrix = matrix;
  }
  getExecutionLogs(): AIExecutionLog[] {
    return this.logs;
  }
  appendExecutionLog(log: AIExecutionLog): void {
    this.logs.unshift(log);
  }
  clearExecutionLogs(): void {
    this.logs = [];
  }
}

test("ModelRoutingService - All 5 agent roles configured per Phase 1 spec", () => {
  const repo = new MockModelRoutingRepository();
  const service = new ModelRoutingService(repo);
  const matrix = service.getMatrix();

  assert.equal(matrix.totalAgents, 5);

  // 1. DOCTRINE_JUDGE
  const judge = service.getAgentProfile("DOCTRINE_JUDGE");
  assert.equal(judge.primaryModel, "anthropic/claude-sonnet-5");
  assert.deepEqual(judge.fallbackModels, [
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-5.5",
  ]);
  assert.equal(judge.temperature, 0.0);
  assert.match(judge.coreTask, /1% Risk/);

  // 2. HEARTBEAT_INGEST
  const ingest = service.getAgentProfile("HEARTBEAT_INGEST");
  assert.equal(ingest.primaryModel, "deepseek/deepseek-v4.1-flash");
  assert.deepEqual(ingest.fallbackModels, [
    "minimax/minimax-m3",
    "google/gemini-3.8-flash",
  ]);
  assert.equal(ingest.temperature, 0.1);

  // 3. QUANT_ANALYZER
  const quant = service.getAgentProfile("QUANT_ANALYZER");
  assert.equal(quant.primaryModel, "deepseek/deepseek-v4-pro");
  assert.deepEqual(quant.fallbackModels, [
    "openai/gpt-5.6-luna",
    "anthropic/claude-sonnet-5",
  ]);
  assert.equal(quant.temperature, 0.0);

  // 4. CONTEXT_DIGEST
  const digest = service.getAgentProfile("CONTEXT_DIGEST");
  assert.equal(digest.primaryModel, "google/gemini-3.8-flash");
  assert.deepEqual(digest.fallbackModels, [
    "z-ai/glm-5.3-flash",
    "deepseek/deepseek-v4.1-flash",
  ]);
  assert.equal(digest.temperature, 0.3);

  // 5. PAPER_SANDBOX
  const sandbox = service.getAgentProfile("PAPER_SANDBOX");
  assert.equal(sandbox.primaryModel, "nvidia/nemotron-3-ultra-550b-a55b:free");
  assert.deepEqual(sandbox.fallbackModels, [
    "nvidia/nemotron-3-super:free",
    "google/gemini-3.8-flash",
  ]);
  assert.equal(sandbox.temperature, 0.1);
});

test("ModelRoutingService - resolveExecutionChain concatenates primary and fallbacks", () => {
  const repo = new MockModelRoutingRepository();
  const service = new ModelRoutingService(repo);

  const chain = service.resolveExecutionChain("DOCTRINE_JUDGE");
  assert.deepEqual(chain, [
    "anthropic/claude-sonnet-5",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-5.5",
  ]);
});

test("ModelRoutingService - buildSystemPromptOptions enforces ephemeral caching", () => {
  const repo = new MockModelRoutingRepository();
  const service = new ModelRoutingService(repo);

  const options = service.buildSystemPromptOptions("DOCTRINE_JUDGE");
  assert.deepEqual(options.cacheControl, { type: "ephemeral" });
  assert.equal(options.temperature, 0.0);
});

test("ModelRoutingService - shouldCompactSession triggers at 300KB", () => {
  const repo = new MockModelRoutingRepository();
  const service = new ModelRoutingService(repo);

  assert.equal(SESSION_COMPACTION_THRESHOLD_KB, 300);
  assert.equal(service.shouldCompactSession(250 * 1024), false);
  assert.equal(service.shouldCompactSession(299 * 1024), false);
  assert.equal(service.shouldCompactSession(300 * 1024), true);
  assert.equal(service.shouldCompactSession(350 * 1024), true);
});

test("ModelRoutingService - estimateCostUsd correctly computes pricing", () => {
  const repo = new MockModelRoutingRepository();
  const service = new ModelRoutingService(repo);

  // 1M prompt tokens for sonnet-5 ($3) + 1M completion ($15) = $18
  const cost = service.estimateCostUsd("anthropic/claude-sonnet-5", 1_000_000, 1_000_000);
  assert.equal(cost, 18.0);

  // Free model = $0
  const freeCost = service.estimateCostUsd(
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    500_000,
    500_000,
  );
  assert.equal(freeCost, 0.0);
});

test("ModelRoutingService - recordExecution and log retrieval", () => {
  const repo = new MockModelRoutingRepository();
  const service = new ModelRoutingService(repo);

  const log = service.recordExecution({
    agentRole: "CONTEXT_DIGEST",
    modelRequested: "google/gemini-3.8-flash",
    modelResolved: "google/gemini-3.8-flash",
    isFallback: false,
    promptTokens: 1000,
    completionTokens: 200,
    durationMs: 450,
    status: "success",
  });

  assert.equal(log.agentRole, "CONTEXT_DIGEST");
  assert.equal(log.status, "success");
  assert.ok(log.costEstimateUsd >= 0);

  const logs = service.getExecutionLogs();
  assert.equal(logs.length, 1);
  assert.equal(logs[0].id, log.id);
});
