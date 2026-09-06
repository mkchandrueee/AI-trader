"use client";

import { useState, useCallback } from "react";
import Sidebar from "@/components/Sidebar";
import { fetchJSON } from "@/lib/api";
import { RefreshCw } from "lucide-react";

interface ScanRow {
  symbol: string;
  kind: "index" | "stock";
  open: number;
  high: number;
  low: number;
  close: number;
  prev_close: number;
  change_pct: number;
  confidence: number;
  bullish: boolean;
  body_ratio: number;
  close_pos: number;
}

interface ScanResponse {
  date: string | null;
  rows: ScanRow[];
  total_before_filter: number;
  returned: number;
  message?: string;
  error?: string;
}

export default function ScannerPage() {
  const [universe, setUniverse] = useState<"all" | "indices" | "stocks">("all");
  const [direction, setDirection] = useState<"all" | "bullish" | "bearish">("all");
  const [minConfidence, setMinConfidence] = useState(60);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ScanResponse | null>(null);

  const runScan = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        universe, direction, min_confidence: String(minConfidence), search, limit: "150",
      });
      const data = await fetchJSON<ScanResponse>(`/api/scanner/scan?${params}`);
      if (data.error) throw new Error(data.error);
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setLoading(false);
    }
  }, [universe, direction, minConfidence, search]);

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col min-h-screen overflow-hidden">
        <main className="flex-1 p-5 overflow-y-auto">
          <div className="mb-5">
            <h1 className="text-sm font-bold uppercase tracking-wider" style={{ color: "#00e87b" }}>
              Market Scanner
            </h1>
            <p className="text-[10px] mt-0.5" style={{ color: "#5a6270" }}>
              Same candle-quality score the Math Decision Engine uses (direction 20% + body 40% + close position 40%),
              applied across the whole NSE board via free EOD bhavcopy. Completed candles from the last published
              session, not a live intraday read.
            </p>
          </div>

          {/* Filters */}
          <div className="t-panel p-4 mb-5 flex flex-wrap items-end gap-3">
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Universe</label>
              <select value={universe} onChange={(e) => setUniverse(e.target.value as typeof universe)}
                className="px-3 py-[6px] text-[12px]" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }}>
                <option value="all">All</option>
                <option value="indices">Indices only</option>
                <option value="stocks">Stocks only</option>
              </select>
            </div>
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Direction</label>
              <select value={direction} onChange={(e) => setDirection(e.target.value as typeof direction)}
                className="px-3 py-[6px] text-[12px]" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }}>
                <option value="all">All</option>
                <option value="bullish">Bullish</option>
                <option value="bearish">Bearish</option>
              </select>
            </div>
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Min Confidence</label>
              <input type="number" min={0} max={100} value={minConfidence}
                onChange={(e) => setMinConfidence(Number(e.target.value))}
                className="px-3 py-[6px] text-[12px] w-24" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }} />
            </div>
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Search</label>
              <input value={search} onChange={(e) => setSearch(e.target.value.toUpperCase())}
                onKeyDown={(e) => e.key === "Enter" && runScan()}
                placeholder="Symbol contains…"
                className="px-3 py-[6px] text-[12px] w-36" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }} />
            </div>
            <button onClick={runScan} disabled={loading} className="t-btn t-btn-green flex items-center gap-1.5 px-4 py-[6px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50">
              <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} /> {loading ? "Scanning…" : "Scan"}
            </button>
          </div>

          {error && (
            <div className="t-panel p-4 mb-5" style={{ borderColor: "#ff3e3e" }}>
              <p className="text-[11px]" style={{ color: "#ff3e3e" }}>ERROR: {error}</p>
            </div>
          )}

          {result && (
            <div className="t-panel p-4">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "#5a6270" }}>
                  {result.returned} of {result.total_before_filter} — session {result.date || "—"}
                </h3>
              </div>
              {result.message ? (
                <p className="text-[11px] text-center py-6" style={{ color: "#3d4450" }}>{result.message}</p>
              ) : (
                <div className="overflow-x-auto">
                  <table>
                    <thead>
                      <tr>
                        {["Symbol", "Type", "Close", "Chg %", "Confidence", "Direction"].map((h) => (
                          <th key={h}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((r) => (
                        <tr key={r.symbol}>
                          <td style={{ fontWeight: 600 }}>{r.symbol}</td>
                          <td className="uppercase" style={{ color: "#5a6270" }}>{r.kind}</td>
                          <td>₹{r.close.toLocaleString("en-IN")}</td>
                          <td style={{ color: r.change_pct >= 0 ? "#00e87b" : "#ff3e3e" }}>
                            {r.change_pct >= 0 ? "+" : ""}{r.change_pct}%
                          </td>
                          <td style={{ fontWeight: 600 }}>{r.confidence}</td>
                          <td style={{ color: r.bullish ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                            {r.bullish ? "BULLISH" : "BEARISH"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="text-[9px] mt-3" style={{ color: "#3d4450" }}>
                Candle quality measures the shape of one completed candle — how much of its range was body, and how
                near the top it closed. It is not a probability the move continues. Verify before trusting a screen.
              </p>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
