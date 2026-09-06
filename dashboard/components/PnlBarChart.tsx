"use client";

import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell, ReferenceLine,
} from "recharts";
import type { Trade } from "@/lib/api";

interface Props { trades: Trade[] }

export default function PnlBarChart({ trades }: Props) {
  if (!trades.length) return null;

  const data = trades.map((t, i) => ({
    i: i + 1,
    pnl: t.pnl,
    label: t.symbol,
  }));

  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ top: 5, right: 10, bottom: 5, left: 10 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e222c" />
        <XAxis dataKey="i" tick={{ fill: "#5a6270", fontSize: 9 }} label={{ value: "Trade #", position: "insideBottom", fill: "#3d4450", fontSize: 9, offset: -2 }} />
        <YAxis tick={{ fill: "#5a6270", fontSize: 9 }} tickFormatter={(v) => `₹${(v / 1000).toFixed(0)}K`} width={50} />
        <Tooltip
          contentStyle={{ background: "#181c24", border: "1px solid #252a33", borderRadius: 0, fontSize: 11, fontFamily: "JetBrains Mono" }}
          formatter={(v: any) => [`₹${Number(v) >= 0 ? "+" : ""}${Number(v).toLocaleString("en-IN")}`, "P&L"]}
        />
        <ReferenceLine y={0} stroke="#252a33" />
        <Bar dataKey="pnl">
          {data.map((d, i) => (
            <Cell key={i} fill={d.pnl >= 0 ? "#00e87b" : "#ff3e3e"} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
