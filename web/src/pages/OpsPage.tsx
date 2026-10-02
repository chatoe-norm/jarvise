import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type IngestHealthStatus, type PaperAutoStatus, type StatusPayload } from "@/lib/api";
import { relativeAge } from "@/lib/utils";

function jobSummary(payload: unknown): { ok: boolean | null; label: string } {
  if (payload == null) return { ok: null, label: "No run recorded" };
  if (typeof payload !== "object") return { ok: null, label: String(payload) };
  const obj = payload as Record<string, unknown>;
  const ok = typeof obj.ok === "boolean" ? obj.ok : null;
  const bits = Object.entries(obj)
    .filter(([k]) => k !== "ok")
    .slice(0, 4)
    .map(([k, v]) => `${k}=${typeof v === "object" ? "…" : String(v)}`)
    .join(" · ");
  return { ok, label: bits || (ok === true ? "OK" : ok === false ? "Failed" : "Status") };
}

function IngestHealthCard({ health }: { health: IngestHealthStatus | null | undefined }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Ingest health</CardTitle>
        <CardDescription>
          EMA200 warm-up, freshness and gaps on the paper timeframe. Alert-only — fix with the one-shot backfill.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {!health ? (
          <p className="text-[var(--color-muted)]">No health run recorded yet.</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={health.ok ? "ok" : "danger"}>{health.ok ? "healthy" : "attention"}</Badge>
              <span className="text-xs text-[var(--color-muted)]">
                {health.timeframe} · checked {relativeAge(health.at_ms)}
              </span>
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              {Object.entries(health.symbols).map(([sym, s]) => (
                <div key={sym} className="rounded-md border border-[var(--color-border)] px-3 py-2">
                  <div className="font-medium">{sym}</div>
                  <div className="text-xs text-[var(--color-muted)]">
                    rows {s.rows} · EMA200 ready {s.ema200_ready} · age {s.newest_age_min ?? "—"} min · gaps {s.gaps}
                  </div>
                </div>
              ))}
            </div>
            {health.alerts.length ? (
              <ul className="list-disc space-y-1 pl-5 text-[var(--color-danger)]">
                {health.alerts.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
            ) : null}
            {!health.ok && health.backfill_hint ? (
              <code className="block overflow-x-auto rounded-md bg-[#161d27] p-2 text-xs">
                {health.backfill_hint}
              </code>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function PaperAutoCard({ auto }: { auto: PaperAutoStatus | null | undefined }) {
  const counts = auto
    ? [
        ["approved", auto.approved?.length ?? 0],
        ["rejected", auto.rejected?.length ?? 0],
        ["deferred", auto.deferred?.length ?? 0],
        ["filtered", auto.filtered_out?.length ?? 0],
        ["failed", auto.apply_failed?.length ?? 0],
      ]
    : [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Paper auto-decide</CardTitle>
        <CardDescription>
          Claude (OpenRouter) second-layer review after each paper run. Paper only; off by default
          (JARVISE_PAPER_AUTO_DECIDE).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {!auto ? (
          <p className="text-[var(--color-muted)]">No auto-decide run recorded yet.</p>
        ) : auto.skipped ? (
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={auto.reason === "auto_decide_disabled" ? "muted" : "danger"}>
              {auto.reason === "auto_decide_disabled" ? "off" : "skipped"}
            </Badge>
            <span className="text-xs text-[var(--color-muted)]">
              {auto.reason} · {relativeAge(auto.at_ms)}
            </span>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={auto.ok ? "ok" : "danger"}>{auto.ok ? "ran" : "error"}</Badge>
              <span className="text-xs text-[var(--color-muted)]">
                {auto.model} · {relativeAge(auto.at_ms)} · {auto.duration_s ?? "—"}s
                {auto.doctrine_unavailable ? " · doctrine unavailable" : ""}
              </span>
            </div>
            <div className="grid grid-cols-5 gap-2">
              {counts.map(([label, n]) => (
                <div key={String(label)}>
                  <div className="text-xs text-[var(--color-muted)]">{label}</div>
                  <div className="font-medium tabular-nums">{n}</div>
                </div>
              ))}
            </div>
            {auto.deferred?.length ? (
              <ul className="list-disc space-y-1 pl-5">
                {auto.deferred.slice(0, 5).map((d) => (
                  <li key={d.id}>
                    <span className="font-medium">{d.symbol}</span> — {d.reason}
                  </li>
                ))}
              </ul>
            ) : null}
            {auto.apply_failed?.length ? (
              <ul className="list-disc space-y-1 pl-5 text-[var(--color-danger)]">
                {auto.apply_failed.slice(0, 5).map((f) => (
                  <li key={f.id}>
                    <span className="font-medium">{f.symbol}</span> — {f.error}
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}

export function OpsPage({ onStatusChange }: { onStatusChange?: () => void }) {
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [rawOpen, setRawOpen] = useState(false);

  async function load() {
    try {
      setStatus(await api.status());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function setKill(state: "on" | "off") {
    setBusy(true);
    try {
      await api.killSwitch(state);
      await load();
      onStatusChange?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const jobs = [
    { key: "ingest", title: "Ingest", data: status?.ingest },
    { key: "rag", title: "RAG", data: status?.rag },
    { key: "paper", title: "Paper enqueue", data: status?.paper },
    { key: "paper_expire", title: "Paper expire", data: status?.paper_expire },
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Ops</h1>
        <p className="text-sm text-[var(--color-muted)]">
          Kill switch, pipeline health, doctrine index — not day-to-day trading.
        </p>
      </div>

      {error ? <p className="text-sm text-[var(--color-danger)]">{error}</p> : null}

      <Card>
        <CardHeader>
          <CardTitle>Kill switch</CardTitle>
          <CardDescription>
            Halts automated paper schedules that respect the Redis kill key. Live stays gated separately.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3">
          <Badge variant={status?.kill_switch ? "danger" : "ok"}>
            {status?.kill_switch ? "ENGAGED" : "clear"}
          </Badge>
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => void setKill("on")}
          >
            Engage
          </Button>
          <Button variant="ok" disabled={busy} onClick={() => void setKill("off")}>
            Clear
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Risk caps</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-2 text-sm sm:grid-cols-3">
          {status?.risk_caps
            ? Object.entries(status.risk_caps).map(([k, v]) => (
                <div key={k}>
                  <div className="text-xs text-[var(--color-muted)]">{k}</div>
                  <div className="font-medium tabular-nums">{String(v)}</div>
                </div>
              ))
            : "—"}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Pipeline</CardTitle>
          <CardDescription>Last job outcomes from Redis</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {jobs.map((j) => {
            const s = jobSummary(j.data);
            return (
              <div
                key={j.key}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-[var(--color-border)] px-3 py-2"
              >
                <div>
                  <div className="text-sm font-medium">{j.title}</div>
                  <div className="text-xs text-[var(--color-muted)]">{s.label}</div>
                </div>
                <Badge
                  variant={
                    s.ok === true ? "ok" : s.ok === false ? "danger" : "muted"
                  }
                >
                  {s.ok === true ? "OK" : s.ok === false ? "Fail" : "—"}
                </Badge>
              </div>
            );
          })}
          <Button size="sm" variant="ghost" onClick={() => setRawOpen((v) => !v)}>
            {rawOpen ? "Hide raw JSON" : "Show raw JSON"}
          </Button>
          {rawOpen ? (
            <pre className="overflow-x-auto rounded-md bg-[#161d27] p-3 text-xs text-[var(--color-muted)]">
              {JSON.stringify(
                {
                  ingest: status?.ingest,
                  rag: status?.rag,
                  paper: status?.paper,
                  paper_expire: status?.paper_expire,
                  ingest_health: status?.ingest_health,
                  paper_auto: status?.paper_auto,
                  qdrant: status?.qdrant,
                },
                null,
                2,
              )}
            </pre>
          ) : null}
        </CardContent>
      </Card>

      <IngestHealthCard health={status?.ingest_health} />

      <PaperAutoCard auto={status?.paper_auto} />

      <Card>
        <CardHeader>
          <CardTitle>Qdrant doctrine</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-[var(--color-muted)]">
          {status?.qdrant
            ? `exists=${String(status.qdrant.exists)} · points=${String(status.qdrant.points ?? "—")} · status=${String(status.qdrant.status ?? "—")}`
            : "—"}
        </CardContent>
      </Card>
    </div>
  );
}
