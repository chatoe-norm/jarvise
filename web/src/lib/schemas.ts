import { z } from "zod";

export const statusSchema = z
  .object({
    paper_only: z.boolean(),
    live_trading: z.boolean(),
    kill_switch: z.boolean(),
    ingest: z.unknown(),
    rag: z.unknown(),
    paper: z.unknown(),
    paper_expire: z.unknown(),
    risk_monitor: z.unknown().optional(),
    risk_caps: z.record(z.string(), z.unknown()),
    qdrant: z.record(z.string(), z.unknown()),
  })
  .loose();

export const paperSchema = z
  .object({
    ok: z.boolean(),
    positions: z.array(z.record(z.string(), z.unknown())),
    orders: z.array(z.record(z.string(), z.unknown())),
  })
  .loose();

export const dashboardSchema = z
  .object({
    ok: z.boolean(),
    status: statusSchema,
    pending_count: z.number(),
    approvals: z.array(z.record(z.string(), z.unknown())),
    paper: z.record(z.string(), z.unknown()),
    metrics: z.record(z.string(), z.unknown()),
  })
  .loose();
