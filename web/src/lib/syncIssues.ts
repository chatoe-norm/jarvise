import type { SyncIssue } from "@/lib/api";

/** "BTCUSDT (ingest 4h, exit 2), ETHUSDT (analyze: no candles stored)" for owner messages. */
export function describeIssues(issues: SyncIssue[]): string {
  return issues
    .map((i) => {
      const why = i.error ? `: ${i.error}` : i.exit_code != null ? `, exit ${i.exit_code}` : "";
      return `${i.symbol} (${i.step}${why})`;
    })
    .join(", ");
}
