import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type IngestHealthStatus, type PaperAutoStatus, type StatusPayload } from "@/lib/api";
import { resolveReasonLabel } from "@/lib/copy";
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
              {resolveReasonLabel(auto.reason)} · {relativeAge(auto.at_ms)}
            </span>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={auto.ok ? "ok" : "danger"}>{auto.ok ? "ran" : "error"}</Badge>
              <span className="text-xs text-[var(--color-muted)]">
                {auto.model} · {relativeAge(auto.at_ms)} · {auto.duration_s ?? "—"}s
                {auto.doctrine_unavailable ? " · trading rules unavailable" : ""}
                {auto.error ? ` · ${auto.error}` : ""}
                {auto.halted ? ` · halted: ${auto.halted}` : ""}
                {auto.auto_ev_gate?.blocked
                  ? ` · EV gate blocked (N=${auto.auto_ev_gate.n}, EV=${auto.auto_ev_gate.ev})`
                  : ""}
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
                    <span className="font-medium">{d.symbol}</span> — {resolveReasonLabel(d.reason)}
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
    { key: "ingest", title: "Market Ingest (1h / 4h / 1d)", data: status?.ingest },
    { key: "rag", title: "Trading Rules & RAG Refresh", data: status?.rag },
    { key: "paper", title: "Paper Enqueue & Signal Scan", data: status?.paper },
    { key: "paper_expire", title: "Paper Expire & Timeout Handler", data: status?.paper_expire },
    { key: "risk_monitor", title: "Risk Monitor (MTF & Stops)", data: status?.risk_monitor },
  ];

  return (
    <div className="space-y-6 max-w-7xl mx-auto">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-[var(--color-foreground)]">
            Network Operations Center (NOC)
          </h1>
          <p className="text-sm text-[var(--color-muted)]">
            Infrastructure health, market safety kill-switch, telemetry, and pipeline schedulers.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => void load()}
          className="border-[var(--color-border)] text-xs font-mono"
        >
          {busy ? "Polling…" : "↻ Refresh Telemetry"}
        </Button>
      </div>

      {error ? <p className="text-sm text-[var(--color-danger)]">{error}</p> : null}

      <Card className="border-[var(--color-border)]">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Market Safety Kill-Switch</CardTitle>
            <Badge variant={status?.kill_switch ? "danger" : "ok"}>
              {status?.kill_switch ? "ENGAGED — Pipeline Blocked" : "CLEAR — Systems Nominal"}
            </Badge>
          </div>
          <CardDescription>
            Master circuit breaker. When engaged, automated paper schedules are paused and order placement is blocked.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/40 p-4">
            <div className="space-y-1">
              <div className="text-xs text-[var(--color-muted)]">Circuit Breaker Status</div>
              <div className="text-sm font-medium">
                {status?.kill_switch ? (
                  <span className="text-rose-400 font-mono">
                    Engaged: {(status as Record<string, unknown>).kill_switch_state ? String(((status as Record<string, unknown>).kill_switch_state as Record<string, unknown>).reason || "Safety Triggered") : "Safety Triggered"}
                  </span>
                ) : (
                  <span className="text-[var(--color-ok)] font-mono">
                    Normal · All fail-closed safety checks green
                  </span>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="danger"
                size="sm"
                disabled={busy || status?.kill_switch}
                onClick={() => void setKill("on")}
              >
                Engage Kill-Switch
              </Button>
              <Button
                variant="ok"
                size="sm"
                disabled={busy || !status?.kill_switch}
                onClick={() => void setKill("off")}
              >
                Clear &amp; Resume
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card className="border-[var(--color-border)]">
        <CardHeader>
          <CardTitle>Risk Caps &amp; Guardrails</CardTitle>
          <CardDescription>Hard limits enforced by the engine across both paper and live execution</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 text-sm">
          {status?.risk_caps
            ? Object.entries(status.risk_caps).map(([k, v]) => (
                <div key={k} className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]/30 p-3">
                  <div className="text-xs text-[var(--color-muted)] font-mono">{k}</div>
                  <div className="mt-1 font-mono font-bold text-base text-[var(--color-foreground)]">{String(v)}</div>
                </div>
              ))
            : "—"}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Observability</CardTitle>
          <CardDescription>
            Prometheus scrapes <code>/metrics</code> on web and jobs. Start with{" "}
            <code>docker compose --profile obs up -d</code> (Tailscale host only).
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-3 text-sm">
          <a
            className="underline"
            href={`http://${typeof window !== "undefined" ? window.location.hostname : "127.0.0.1"}:3000`}
            target="_blank"
            rel="noreferrer"
          >
            Grafana :3000
          </a>
          <a
            className="underline"
            href={`http://${typeof window !== "undefined" ? window.location.hostname : "127.0.0.1"}:9090`}
            target="_blank"
            rel="noreferrer"
          >
            Prometheus :9090
          </a>
          <a className="underline" href="/metrics" target="_blank" rel="noreferrer">
            Web /metrics
          </a>
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
                  risk_monitor: status?.risk_monitor,
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
          <CardTitle>Qdrant trading rules</CardTitle>
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
