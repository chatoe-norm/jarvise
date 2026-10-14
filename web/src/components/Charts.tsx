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

const tooltipStyle = {
  background: "#121820",
  border: "1px solid #1e2a38",
  borderRadius: 8,
  fontSize: 12,
};

export function EquityChart({
  points,
}: {
  points: Array<{ t: string; equity: number }>;
}) {
  if (points.length === 0) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-[var(--color-muted)]">
        No fills yet — equity curve appears after paper orders.
      </div>
    );
  }
  return (
    <div className="h-48 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points}>
          <defs>
            <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#3d9cf0" stopOpacity={0.35} />
              <stop offset="100%" stopColor="#3d9cf0" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#1e2a38" strokeDasharray="3 3" />
          <XAxis dataKey="t" tick={{ fill: "#8b9aab", fontSize: 11 }} />
          <YAxis tick={{ fill: "#8b9aab", fontSize: 11 }} width={56} />
          <Tooltip contentStyle={tooltipStyle} />
          <Area
            type="monotone"
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
