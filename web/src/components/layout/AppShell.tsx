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
    <div className="flex min-h-full bg-[var(--color-background)]">
      <aside className="flex w-60 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-sidebar)]">
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
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] bg-[var(--color-sidebar)]/60 backdrop-blur px-6 py-2.5">
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
        <main className="flex-1 overflow-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
