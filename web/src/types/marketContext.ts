import { z } from "zod";

export const scoreDomainSchema = z.enum([
  "EXTREME_FEAR",
  "FEAR",
  "NEUTRAL",
  "GREED",
  "EXTREME_GREED",
]);

export type ScoreDomain = z.infer<typeof scoreDomainSchema>;

export const domainScoreMappingSchema = z.object({
  domain: scoreDomainSchema,
  min: z.number().int().min(0).max(100),
  max: z.number().int().min(0).max(100),
  statusLabel: z.string().min(1),
  themeColor: z.string().regex(/^#[0-9A-Fa-f]{6}$/),
  staticDefinition: z.string().min(1),
});

export type DomainScoreMapping = z.infer<typeof domainScoreMappingSchema>;

export const dynamicMarketCauseSchema = z.object({
  bullets: z.array(z.string()).min(1),
  timestampText: z.string().nullable().optional(),
  generatedAtMs: z.number().nullable().optional(),
});

export type DynamicMarketCause = z.infer<typeof dynamicMarketCauseSchema>;

export const semiDynamicContextSchema = z.object({
  score: z.number().int().min(0).max(100),
  domain: scoreDomainSchema,
  statusLabel: z.string().min(1),
  themeColor: z.string().regex(/^#[0-9A-Fa-f]{6}$/),
  staticDefinition: z.string().min(1),
  dynamicMarketCause: dynamicMarketCauseSchema,
  fullConcatenatedMessage: z.string().min(1),
  assembledAtMs: z.number(),
});

export type SemiDynamicContext = z.infer<typeof semiDynamicContextSchema>;
