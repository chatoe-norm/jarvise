import { lazy, Suspense, useCallback, useEffect, useState, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { api, type DashboardPayload } from "@/lib/api";
import { HomePage } from "@/pages/HomePage";

// Home (the approval queue) ships in the first bundle; the other pages, and Recharts with
// them, load on first visit.
const PaperPage = lazy(() => import("@/pages/PaperPage").then((m) => ({ default: m.PaperPage })));
const DecisionsPage = lazy(() => import("@/pages/DecisionsPage").then((m) => ({ default: m.DecisionsPage })));
const ExchangePage = lazy(() => import("@/pages/ExchangePage").then((m) => ({ default: m.ExchangePage })));
const OpsPage = lazy(() => import("@/pages/OpsPage").then((m) => ({ default: m.OpsPage })));

function PageLoad({ children }: { children: ReactNode }) {
  return (
    <Suspense fallback={<p className="text-sm text-[var(--color-muted)]">กำลังโหลดหน้า…</p>}>{children}</Suspense>
  );
}

const DASHBOARD_POLL_MS = 30_000;

export default function App() {
  const [dash, setDash] = useState<DashboardPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.dashboard();
      setDash(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    // Queue, positions and the kill switch change server-side (jobs, risk monitor): poll,
    // but only while the tab is visible, and catch up as soon as it becomes visible again.
    const timer = window.setInterval(() => {
      if (!document.hidden) void refresh();
    }, DASHBOARD_POLL_MS);
    const onVisible = () => {
      if (!document.hidden) void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh]);

  return (
    <Routes>
      <Route
        element={
          <AppShell
            status={dash?.status ?? null}
            pendingCount={dash?.pending_count ?? 0}
            openPositionsCount={(dash?.paper?.positions as unknown[])?.length ?? 0}
          />
        }
      >
        <Route
          path="/"
          element={
            <HomePage
              data={dash}
              loading={loading}
              error={error}
              onRefresh={() => void refresh()}
            />
          }
        />
        <Route path="/paper" element={<PageLoad><PaperPage onPositionsChanged={() => void refresh()} /></PageLoad>} />
        <Route path="/decisions" element={<PageLoad><DecisionsPage /></PageLoad>} />
        <Route path="/exchange" element={<PageLoad><ExchangePage dashboard={dash} /></PageLoad>} />
        <Route path="/ops" element={<PageLoad><OpsPage onStatusChange={() => void refresh()} /></PageLoad>} />
        <Route path="/analytics" element={<Navigate to="/" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
