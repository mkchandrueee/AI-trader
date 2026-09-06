"use client";

import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend, ReferenceLine,
} from "recharts";

interface Point { time: string; equity: number }

interface Props {
  curves: { low?: Point[]; medium?: Point[]; high?: Point[] };
  selected?: "low" | "medium" | "high" | "all";
}

const COLORS = { low: "#4da6ff", medium: "#e8c300", high: "#00e87b" };

const fmt = (v: number) =>
  v >= 0 ? `₹+${v.toLocaleString("en-IN")}` : `₹${v.toLocaleString("en-IN")}`;

export default function EquityChart({ curves, selected = "all" }: Props) {
  const maxLen = Math.max(
    curves.low?.length ?? 0,
    curves.medium?.length ?? 0,
    curves.high?.length ?? 0,
  );

  if (maxLen === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-[11px]" style={{ color: '#3d4450' }}>
        NO EQUITY DATA
      </div>
    );
  }

  const data = Array.from({ length: maxLen }, (_, i) => ({
    i: i + 1,
    low:    curves.low?.[i]?.equity,
    medium: curves.medium?.[i]?.equity,
    high:   curves.high?.[i]?.equity,
  }));

  const show = (k: "low" | "medium" | "high") =>
    selected === "all" || selected === k;

  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart data={data} margin={{ top: 5, right: 20, bottom: 5, left: 10 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e222c" />
        <XAxis
          dataKey="i"
          tick={{ fill: "#5a6270", fontSize: 10 }}
          label={{ value: "Trade #", position: "insideBottom", fill: "#3d4450", fontSize: 10, offset: -2 }}
        />
        <YAxis
          tick={{ fill: "#5a6270", fontSize: 10 }}
          tickFormatter={(v) => `₹${(v / 1000).toFixed(0)}K`}
          width={55}
        />
        <Tooltip
          contentStyle={{ background: "#181c24", border: "1px solid #252a33", borderRadius: 0, fontSize: 11, fontFamily: "JetBrains Mono" }}
          labelStyle={{ color: "#5a6270" }}
          formatter={(v: any) => [fmt(Number(v)), ""]}
        />
        <ReferenceLine y={0} stroke="#252a33" strokeDasharray="4 4" />
        <Legend wrapperStyle={{ fontSize: 10, color: "#5a6270" }} />
        {show("low") && (
          <Line type="monotone" dataKey="low" stroke={COLORS.low} dot={false}
            strokeWidth={1.5} name="Low" connectNulls />
        )}
        {show("medium") && (
          <Line type="monotone" dataKey="medium" stroke={COLORS.medium} dot={false}
            strokeWidth={1.5} name="Medium" connectNulls />
        )}
        {show("high") && (
          <Line type="monotone" dataKey="high" stroke={COLORS.high} dot={false}
            strokeWidth={1.5} name="High" connectNulls />
        )}
      </LineChart>
    </ResponsiveContainer>
  );
}
