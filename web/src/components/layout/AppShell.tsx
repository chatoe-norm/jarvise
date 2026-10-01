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
}: {
  status: StatusPayload | null;
  pendingCount: number;
}) {
  const kill = Boolean(status?.kill_switch);
  const live = Boolean(status?.live_trading);

  return (
    <div className="flex min-h-full">
      <aside className="flex w-56 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-sidebar)]">
        <div className="border-b border-[var(--color-border)] px-4 py-5">
          <div className="text-lg font-semibold tracking-tight">Jarvise</div>
          <div className="text-xs text-[var(--color-muted)]">Command Dashboard</div>
        </div>
        <nav className="flex flex-1 flex-col gap-1 p-3">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium text-[var(--color-muted)] hover:bg-[var(--color-card)] hover:text-[var(--color-foreground)]",
                  isActive &&
                    "bg-[var(--color-card)] text-[var(--color-foreground)] outline outline-1 outline-[var(--color-ok)]/40",
                )
              }
            >
              <item.icon className="h-4 w-4" />
              {item.label}
              {item.to === "/" && pendingCount > 0 ? (
                <span className="ml-auto rounded-md bg-[var(--color-accent)]/20 px-1.5 text-xs text-[var(--color-accent)]">
                  {pendingCount}
                </span>
              ) : null}
            </NavLink>
          ))}
        </nav>
        <div className="space-y-2 border-t border-[var(--color-border)] p-3">
          <Badge variant={live ? "danger" : "ok"}>
            {live ? "LIVE APPROVAL" : "PAPER ONLY"}
          </Badge>
          <Badge variant={kill ? "danger" : "muted"}>
            Kill {kill ? "ENGAGED" : "clear"}
          </Badge>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center gap-3 border-b border-[var(--color-border)] px-6 py-3">
          <Badge variant={live ? "danger" : "ok"}>
            {live
              ? "LIVE APPROVAL ENABLED — Approve may place size-capped spot orders"
              : "PAPER ONLY — no order placement"}
          </Badge>
          {kill ? (
            <Badge variant="danger">Kill switch engaged — approve blocked</Badge>
          ) : null}
        </header>
        <main className="flex-1 overflow-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
