import { useCallback, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { api, type DashboardPayload } from "@/lib/api";
import { DecisionsPage } from "@/pages/DecisionsPage";
import { ExchangePage } from "@/pages/ExchangePage";
import { HomePage } from "@/pages/HomePage";
import { OpsPage } from "@/pages/OpsPage";
import { PaperPage } from "@/pages/PaperPage";

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
  }, [refresh]);

  return (
    <Routes>
      <Route
        element={
          <AppShell
            status={dash?.status ?? null}
            pendingCount={dash?.pending_count ?? 0}
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
        <Route path="/paper" element={<PaperPage />} />
        <Route path="/decisions" element={<DecisionsPage />} />
        <Route path="/exchange" element={<ExchangePage />} />
        <Route path="/ops" element={<OpsPage onStatusChange={() => void refresh()} />} />
        <Route path="/analytics" element={<Navigate to="/" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
