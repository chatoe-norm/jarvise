import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { equityDomain, type EquityPoint } from "@/lib/equity";
import { formatNum } from "@/lib/utils";

const tooltipStyle = {
  background: "#121820",
  border: "1px solid #1e2a38",
  borderRadius: 8,
  fontSize: 12,
};

function EquityTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload?: EquityPoint }>;
}) {
  const row = payload?.[0]?.payload;
  if (!active || !row) return null;
  const pnl = row.pnl;
  const pnlText =
    pnl == null ? null : `${pnl > 0 ? "+" : ""}${formatNum(pnl)} ดอลลาร์`;
  return (
    <div style={tooltipStyle} className="px-2 py-1.5">
      <div>{row.t}</div>
      {row.symbol ? (
        <div>
          {row.symbol} {pnlText}
        </div>
      ) : null}
      <div>Equity {formatNum(row.equity)}</div>
    </div>
  );
}

export function EquityChart({ points }: { points: EquityPoint[] }) {
  if (points.length === 0) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-[var(--color-muted)]">
        No fills yet — equity curve appears after paper orders.
      </div>
    );
  }
  const [yMin, yMax] = equityDomain(points);
  return (
    <div className="h-64 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
          <defs>
            <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#3d9cf0" stopOpacity={0.35} />
              <stop offset="100%" stopColor="#3d9cf0" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#1e2a38" strokeDasharray="3 3" />
          <XAxis
            dataKey="t"
            interval={0}
            angle={-40}
            textAnchor="end"
            height={72}
            tick={{ fill: "#8b9aab", fontSize: 10 }}
          />
          <YAxis
            domain={[yMin, yMax]}
            tick={{ fill: "#8b9aab", fontSize: 11 }}
            width={64}
          />
          <Tooltip content={<EquityTooltip />} />
          <Area
            type="stepAfter"
            dataKey="equity"
            stroke="#3d9cf0"
            fill="url(#eq)"
            strokeWidth={2}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export function WinLossChart({
  wins,
  losses,
  scratches,
}: {
  wins: number;
  losses: number;
  scratches: number;
}) {
  const data = [
    { name: "Wins", value: wins, color: "#3ecf8e" },
    { name: "Losses", value: losses, color: "#e35d6a" },
    { name: "Scratches", value: scratches, color: "#8b9aab" },
  ].filter((d) => d.value > 0);
  if (data.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-[var(--color-muted)]">
        No closed round-trips yet.
      </div>
    );
  }
  return (
    <div className="h-40 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ left: 16 }}>
          <CartesianGrid stroke="#1e2a38" strokeDasharray="3 3" />
          <XAxis type="number" tick={{ fill: "#8b9aab", fontSize: 11 }} />
          <YAxis
            type="category"
            dataKey="name"
            tick={{ fill: "#8b9aab", fontSize: 11 }}
            width={72}
          />
          <Tooltip contentStyle={tooltipStyle} />
          <Bar dataKey="value" radius={[0, 4, 4, 0]}>
            {data.map((d) => (
              <Cell key={d.name} fill={d.color} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function UsdBars({
  rows,
  emptyMessage = "No priced balances.",
}: {
  rows: Array<{ asset: string; usd: number }>;
  emptyMessage?: string;
}) {
  if (rows.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-[var(--color-muted)]">
        {emptyMessage}
      </div>
    );
  }
  return (
    <div className="h-48 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows}>
          <CartesianGrid stroke="#1e2a38" strokeDasharray="3 3" />
          <XAxis dataKey="asset" tick={{ fill: "#8b9aab", fontSize: 11 }} />
          <YAxis tick={{ fill: "#8b9aab", fontSize: 11 }} width={56} />
          <Tooltip contentStyle={tooltipStyle} />
          <Bar dataKey="usd" fill="#3d9cf0" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
