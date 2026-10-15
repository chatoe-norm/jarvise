import { NavLink, Outlet } from "react-router-dom";
import {
  Activity,
  LayoutDashboard,
  LineChart,
  Shield,
  Wallet,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import type { StatusPayload } from "@/lib/api";

const nav = [
  { to: "/", label: "Home", icon: LayoutDashboard, end: true },
  { to: "/paper", label: "Paper", icon: LineChart },
  { to: "/decisions", label: "Decisions", icon: Activity },
  { to: "/exchange", label: "Exchange", icon: Wallet },
  { to: "/ops", label: "Ops", icon: Shield },
];

export function AppShell({
  status,
  pendingCount,
  openPositionsCount = 0,
}: {
  status: StatusPayload | null;
  pendingCount: number;
  openPositionsCount?: number;
}) {
  const kill = Boolean(status?.kill_switch);
  const live = Boolean(status?.live_trading);

  return (
    <div className="flex min-h-screen flex-col md:flex-row bg-[var(--color-background)]">
      {/* Desktop Sidebar (md+) */}
      <aside className="hidden md:flex w-60 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-sidebar)]">
        <div className="border-b border-[var(--color-border)] px-4 py-5">
          <div className="flex items-center gap-2">
            <div className="h-2.5 w-2.5 rounded-full bg-[var(--color-ok)] animate-pulse" />
            <div className="text-lg font-bold tracking-tight text-[var(--color-foreground)]">Jarvise</div>
          </div>
          <div className="mt-0.5 text-xs text-[var(--color-muted)] font-mono">Command Dashboard</div>
        </div>
        <nav className="flex flex-1 flex-col gap-1.5 p-3">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors text-[var(--color-muted)] hover:bg-[var(--color-card)] hover:text-[var(--color-foreground)]",
                  isActive &&
                    "bg-[var(--color-card)] text-[var(--color-foreground)] shadow-sm border border-[var(--color-border)] font-semibold",
                )
              }
            >
              <item.icon className="h-4 w-4 shrink-0 text-[var(--color-accent)]" />
              <span>{item.label}</span>
              {item.to === "/" && pendingCount > 0 ? (
                <span className="ml-auto rounded-full bg-[var(--color-accent)]/20 px-2 py-0.5 text-xs font-mono font-medium text-[var(--color-accent)] animate-pulse">
                  {pendingCount}
                </span>
              ) : null}
              {item.to === "/paper" && openPositionsCount > 0 ? (
                <span className="ml-auto rounded-full bg-[var(--color-ok)]/15 px-2 py-0.5 text-xs font-mono font-medium text-[var(--color-ok)]">
                  {openPositionsCount}
                </span>
              ) : null}
              {item.to === "/ops" ? (
                <span
                  className={cn(
                    "ml-auto h-2 w-2 rounded-full",
                    kill ? "bg-[var(--color-danger)] animate-ping" : "bg-[var(--color-ok)]/80",
                  )}
                />
              ) : null}
            </NavLink>
          ))}
        </nav>
        <div className="space-y-2 border-t border-[var(--color-border)] p-3">
          <div className="flex flex-col gap-1.5">
            <Badge variant={live ? "danger" : "ok"} className="w-full justify-center py-1">
              {live ? "LIVE APPROVAL" : "PAPER SHIELD ACTIVE"}
            </Badge>
            <Badge variant={kill ? "danger" : "muted"} className="w-full justify-center py-1">
              Kill Switch: {kill ? "ENGAGED" : "CLEAR"}
            </Badge>
          </div>
          <div className="pt-1 text-[11px] text-[var(--color-muted)] text-center font-mono">
            Hostinger KVM · :8080
          </div>
        </div>
      </aside>

      {/* Main Area */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* Mobile Top Header (< md) */}
        <header className="flex md:hidden items-center justify-between border-b border-[var(--color-border)] bg-[var(--color-sidebar)] px-4 py-2.5 sticky top-0 z-40 backdrop-blur-md">
          <div className="flex items-center gap-2">
            <div className="h-2 w-2 rounded-full bg-[var(--color-ok)] animate-pulse" />
            <span className="text-base font-bold tracking-tight text-[var(--color-foreground)]">Jarvise</span>
            <span className="rounded bg-[var(--color-card)] px-1.5 py-0.5 text-[10px] font-mono text-[var(--color-muted)] border border-[var(--color-border)]">
              paper_liquid
            </span>
          </div>
          <div className="flex items-center gap-2">
            {kill ? (
              <Badge variant="danger" className="text-[10px] py-0 px-1.5 animate-pulse">
                KILL ENGAGED
              </Badge>
            ) : (
              <div className="flex items-center gap-1.5 text-[11px] text-[var(--color-muted)] font-mono">
                <span className="h-1.5 w-1.5 rounded-full bg-[var(--color-ok)]" />
                <span>NOC Green</span>
              </div>
            )}
            <Badge variant={live ? "danger" : "ok"} className="text-[10px] py-0 px-1.5">
              {live ? "LIVE" : "PAPER"}
            </Badge>
          </div>
        </header>

        {/* Desktop Header (md+) */}
        <header className="hidden md:flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] bg-[var(--color-sidebar)]/60 backdrop-blur px-6 py-2.5">
          <div className="flex flex-wrap items-center gap-2.5">
            <Badge variant={live ? "danger" : "ok"}>
              {live
                ? "LIVE APPROVAL ENABLED — Size-capped spot"
                : "PAPER ONLY — No order placement"}
            </Badge>
            <Badge variant="outline" className="border-[var(--color-border)] font-mono text-xs">
              Universe: paper_liquid (BTC/ETH/SOL/BNB)
            </Badge>
            <Badge variant="outline" className="border-[var(--color-border)] font-mono text-xs">
              HTF: 1d EMA200 active
            </Badge>
          </div>
          {kill ? (
            <Badge variant="danger" className="animate-pulse">
              Kill switch engaged — pipeline blocked
            </Badge>
          ) : (
            <div className="flex items-center gap-2 text-xs text-[var(--color-muted)] font-mono">
              <span className="inline-block h-2 w-2 rounded-full bg-[var(--color-ok)]" />
              <span>NOC Systems Green</span>
            </div>
          )}
        </header>

        {/* Page Content with safe padding for mobile bottom bar */}
        <main className="flex-1 overflow-auto p-3.5 sm:p-5 md:p-6 pb-24 md:pb-6">
          <Outlet />
        </main>

        {/* Mobile Bottom Navigation Bar (< md) */}
        <nav className="fixed bottom-0 left-0 right-0 z-50 flex md:hidden items-center justify-around border-t border-[var(--color-border)] bg-[var(--color-sidebar)]/95 backdrop-blur-lg px-1 py-1 safe-area-bottom shadow-lg">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  "relative flex flex-1 flex-col items-center justify-center gap-1 py-1.5 text-[11px] font-medium transition-all text-[var(--color-muted)] active:scale-95",
                  isActive
                    ? "text-[var(--color-foreground)] font-semibold"
                    : "hover:text-[var(--color-foreground)]",
                )
              }
            >
              {({ isActive }) => (
                <>
                  <div
                    className={cn(
                      "flex items-center justify-center rounded-full p-1 transition-colors",
                      isActive
                        ? "bg-[var(--color-accent)]/20 text-[var(--color-accent)]"
                        : "text-[var(--color-muted)]",
                    )}
                  >
                    <item.icon className="h-5 w-5 shrink-0" />
                  </div>
                  <span className="tracking-tight">{item.label}</span>
                  {item.to === "/" && pendingCount > 0 ? (
                    <span className="absolute top-1 right-3 rounded-full bg-[var(--color-accent)] px-1.5 py-0.2 text-[9px] font-bold text-black animate-pulse">
                      {pendingCount}
                    </span>
                  ) : null}
                  {item.to === "/paper" && openPositionsCount > 0 ? (
                    <span className="absolute top-1 right-3 rounded-full bg-[var(--color-ok)] px-1.5 py-0.2 text-[9px] font-bold text-black">
                      {openPositionsCount}
                    </span>
                  ) : null}
                  {item.to === "/ops" ? (
                    <span
                      className={cn(
                        "absolute top-1.5 right-4 h-1.5 w-1.5 rounded-full",
                        kill ? "bg-[var(--color-danger)] animate-ping" : "bg-[var(--color-ok)]",
                      )}
                    />
                  ) : null}
                </>
              )}
            </NavLink>
          ))}
        </nav>
      </div>
    </div>
  );
}
