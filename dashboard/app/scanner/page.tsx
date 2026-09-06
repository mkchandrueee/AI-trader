"use client";

import { useState, useCallback } from "react";
import Sidebar from "@/components/Sidebar";
import { fetchJSON } from "@/lib/api";
import { RefreshCw } from "lucide-react";

interface ScanRow {
  symbol: string;
  kind: "index" | "stock" | "option";
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
  rr: number | null;
  turnover: number;
  delivery_pct: number;
  industry: string;
  tradable_fno: boolean;
  underlying?: string;
  strike?: number;
  option_type?: string;
  expiry?: string;
  oi?: number;
  volume?: number;
}

interface OptionLeg {
  underlying: string;
  underlying_confidence: number;
  side: "call" | "put";
  entry: number;
  partial: number;
  target: number;
  stop: number;
  rr: number;
  confidence: number;
  tier: "strong" | "moderate" | "avoid";
}

interface ScanResponse {
  date: string | null;
  rows: ScanRow[];
  total_before_filter: number;
  returned: number;
  counts?: Record<string, number>;
  sectors_available?: string[];
  option_legs?: OptionLeg[];
  message?: string;
  error?: string;
}

const SORT_OPTIONS = [
  { value: "confidence", label: "Quality (Confidence)" },
  { value: "rr", label: "Risk : Reward" },
  { value: "change", label: "% Change" },
  { value: "turnover", label: "Turnover" },
];

const INDEX_TYPES = [
  { value: "", label: "All indices" },
  { value: "fno", label: "F&O tradable only" },
];

const INDEX_LISTS = [
  { value: "", label: "All stocks" },
  { value: "nifty50", label: "NIFTY 50" },
  { value: "nifty100", label: "NIFTY 100" },
  { value: "nifty200", label: "NIFTY 200" },
  { value: "nifty500", label: "NIFTY 500" },
];

export default function ScannerPage() {
  const [universe, setUniverse] = useState<"all" | "indices" | "stocks" | "options">("all");
  const [direction, setDirection] = useState<"all" | "bullish" | "bearish">("all");
  const [minConfidence, setMinConfidence] = useState(60);
  const [search, setSearch] = useState("");
  const [indexList, setIndexList] = useState("");
  const [optionType, setOptionType] = useState<"" | "CE" | "PE">("");
  const [nearestExpiryOnly, setNearestExpiryOnly] = useState(true);
  const [sector, setSector] = useState("");
  const [indexType, setIndexType] = useState<"" | "fno">("");
  const [sortBy, setSortBy] = useState<"confidence" | "rr" | "change" | "turnover">("confidence");
  const [includeOptionLegs, setIncludeOptionLegs] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ScanResponse | null>(null);

  const runScan = useCallback(async (overrides?: { universe?: typeof universe; search?: string }) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        universe: overrides?.universe ?? universe,
        direction,
        min_confidence: String(minConfidence),
        search: overrides?.search ?? search,
        limit: "150",
        index_list: indexList,
        option_type: optionType,
        nearest_expiry_only: String(nearestExpiryOnly),
        sector,
        index_type: indexType,
        sort_by: sortBy,
        include_option_legs: String(includeOptionLegs),
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
  }, [universe, direction, minConfidence, search, indexList, optionType, nearestExpiryOnly, sector, indexType, sortBy, includeOptionLegs]);

  const jumpToIndex = (symbol: string) => {
    setUniverse("indices");
    setSearch(symbol);
    runScan({ universe: "indices", search: symbol });
  };

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
                <option value="options">Options (CE/PE strikes)</option>
              </select>
            </div>
            {universe === "options" && (
              <>
                <div>
                  <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Option Type</label>
                  <select value={optionType} onChange={(e) => setOptionType(e.target.value as typeof optionType)}
                    className="px-3 py-[6px] text-[12px]" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }}>
                    <option value="">CE + PE</option>
                    <option value="CE">CE only</option>
                    <option value="PE">PE only</option>
                  </select>
                </div>
                <div className="flex items-center gap-1.5 pb-[6px]">
                  <input type="checkbox" id="nearest-expiry" checked={nearestExpiryOnly}
                    onChange={(e) => setNearestExpiryOnly(e.target.checked)} />
                  <label htmlFor="nearest-expiry" className="text-[10px]" style={{ color: "#5a6270" }}>
                    Nearest expiry only
                  </label>
                </div>
              </>
            )}
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
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Index List</label>
              <select value={indexList} onChange={(e) => setIndexList(e.target.value)}
                className="px-3 py-[6px] text-[12px]" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }}>
                {INDEX_LISTS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </div>
            {universe !== "options" && (
              <div>
                <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Sector</label>
                <select value={sector} onChange={(e) => setSector(e.target.value)}
                  className="px-3 py-[6px] text-[12px] max-w-[160px]" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }}>
                  <option value="">All sectors</option>
                  {(result?.sectors_available ?? []).map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
            )}
            {(universe === "all" || universe === "indices") && (
              <div>
                <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Index Type</label>
                <select value={indexType} onChange={(e) => setIndexType(e.target.value as typeof indexType)}
                  className="px-3 py-[6px] text-[12px]" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }}>
                  {INDEX_TYPES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </div>
            )}
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Min Confidence</label>
              <input type="number" min={0} max={100} value={minConfidence}
                onChange={(e) => setMinConfidence(Number(e.target.value))}
                className="px-3 py-[6px] text-[12px] w-24" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }} />
            </div>
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Sort By</label>
              <select value={sortBy} onChange={(e) => setSortBy(e.target.value as typeof sortBy)}
                className="px-3 py-[6px] text-[12px]" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }}>
                {SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Search</label>
              <input value={search} onChange={(e) => setSearch(e.target.value.toUpperCase())}
                onKeyDown={(e) => e.key === "Enter" && runScan()}
                placeholder="Symbol contains…"
                className="px-3 py-[6px] text-[12px] w-36" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }} />
            </div>
            {universe !== "options" && (
              <div className="flex items-center gap-1.5 pb-[6px]">
                <input type="checkbox" id="option-legs" checked={includeOptionLegs}
                  onChange={(e) => setIncludeOptionLegs(e.target.checked)} />
                <label htmlFor="option-legs" className="text-[10px]" style={{ color: "#5a6270" }}>
                  Option legs on top picks
                </label>
              </div>
            )}
            <button onClick={() => runScan()} disabled={loading} className="t-btn t-btn-green flex items-center gap-1.5 px-4 py-[6px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50">
              <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} /> {loading ? "Scanning…" : "Scan"}
            </button>
          </div>

          {/* Quick index jumps */}
          <div className="flex items-center gap-2 mb-5">
            <span className="text-[9px] uppercase tracking-wider" style={{ color: "#5a6270" }}>Quick jump:</span>
            <button onClick={() => jumpToIndex("NIFTY")} disabled={loading}
              className="px-3 py-[4px] text-[10px] font-semibold uppercase tracking-wider" style={{ background: "#181c24", border: "1px solid #252a33", color: "#c8cdd5" }}>
              NIFTY
            </button>
            <button onClick={() => jumpToIndex("BANKNIFTY")} disabled={loading}
              className="px-3 py-[4px] text-[10px] font-semibold uppercase tracking-wider" style={{ background: "#181c24", border: "1px solid #252a33", color: "#c8cdd5" }}>
              BANKNIFTY
            </button>
            <button disabled title="No free BSE/SENSEX data source is wired up yet — jugaad-data only covers NSE."
              className="px-3 py-[4px] text-[10px] font-semibold uppercase tracking-wider cursor-not-allowed" style={{ background: "#181c24", border: "1px solid #252a33", color: "#3d4450" }}>
              SENSEX (unavailable)
            </button>
          </div>

          {error && (
            <div className="t-panel p-4 mb-5" style={{ borderColor: "#ff3e3e" }}>
              <p className="text-[11px]" style={{ color: "#ff3e3e" }}>ERROR: {error}</p>
            </div>
          )}

          {result && (
            <div className="t-panel p-4 mb-5">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "#5a6270" }}>
                  {result.returned} of {result.total_before_filter} — session {result.date || "—"}
                </h3>
              </div>
              {result.counts && (
                <div className="flex flex-wrap gap-2 mb-3">
                  {Object.entries(result.counts).map(([key, value]) => (
                    <span key={key} className="px-2 py-[3px] text-[9px] uppercase tracking-wider"
                      style={{ background: "#181c24", border: "1px solid #252a33", color: "#5a6270" }}>
                      {key.replace(/_/g, " ")}: <b style={{ color: "#c8cdd5" }}>{value}</b>
                    </span>
                  ))}
                </div>
              )}
              {result.message ? (
                <p className="text-[11px] text-center py-6" style={{ color: "#3d4450" }}>{result.message}</p>
              ) : (
                <div className="overflow-x-auto">
                  <table>
                    <thead>
                      <tr>
                        {(universe === "options"
                          ? ["Underlying", "Strike", "Type", "Expiry", "Premium", "Chg %", "OI", "Volume", "Confidence", "Direction"]
                          : ["Symbol", "Type", "Sector", "Close", "Chg %", "Turnover (₹L)", "Deliv %", "R:R", "Confidence", "Direction"]
                        ).map((h) => <th key={h}>{h}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((r) => (
                        universe === "options" ? (
                          <tr key={r.symbol}>
                            <td style={{ fontWeight: 600 }}>{r.underlying}</td>
                            <td>{r.strike?.toLocaleString("en-IN")}</td>
                            <td style={{ color: r.option_type === "CE" ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>{r.option_type}</td>
                            <td style={{ color: "#5a6270" }}>{r.expiry}</td>
                            <td>₹{r.close.toLocaleString("en-IN")}</td>
                            <td style={{ color: r.change_pct >= 0 ? "#00e87b" : "#ff3e3e" }}>
                              {r.change_pct >= 0 ? "+" : ""}{r.change_pct}%
                            </td>
                            <td style={{ color: "#5a6270" }}>{r.oi?.toLocaleString("en-IN")}</td>
                            <td style={{ color: "#5a6270" }}>{r.volume?.toLocaleString("en-IN")}</td>
                            <td style={{ fontWeight: 600 }}>{r.confidence}</td>
                            <td style={{ color: r.bullish ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                              {r.bullish ? "BULLISH" : "BEARISH"}
                            </td>
                          </tr>
                        ) : (
                          <tr key={r.symbol}>
                            <td style={{ fontWeight: 600 }}>
                              {r.symbol}
                              {r.kind === "index" && r.tradable_fno && (
                                <span className="ml-1.5 px-1 text-[8px]" style={{ background: "#181c24", border: "1px solid #252a33", color: "#4da6ff" }}>F&O</span>
                              )}
                            </td>
                            <td className="uppercase" style={{ color: "#5a6270" }}>{r.kind}</td>
                            <td style={{ color: "#5a6270" }}>{r.industry || "—"}</td>
                            <td>₹{r.close.toLocaleString("en-IN")}</td>
                            <td style={{ color: r.change_pct >= 0 ? "#00e87b" : "#ff3e3e" }}>
                              {r.change_pct >= 0 ? "+" : ""}{r.change_pct}%
                            </td>
                            <td style={{ color: "#5a6270" }}>{r.turnover ? r.turnover.toLocaleString("en-IN", { maximumFractionDigits: 0 }) : "—"}</td>
                            <td style={{ color: "#5a6270" }}>{r.delivery_pct ? `${r.delivery_pct}%` : "—"}</td>
                            <td style={{ color: "#5a6270" }}>{r.rr ?? "—"}</td>
                            <td style={{ fontWeight: 600 }}>{r.confidence}</td>
                            <td style={{ color: r.bullish ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                              {r.bullish ? "BULLISH" : "BEARISH"}
                            </td>
                          </tr>
                        )
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

          {result && includeOptionLegs && (
            <div className="t-panel p-4">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider mb-3" style={{ color: "#5a6270" }}>
                Option Legs — Trade Decision Engine on Top Picks ({result.option_legs?.length ?? 0})
              </h3>
              {!result.option_legs || result.option_legs.length === 0 ? (
                <p className="text-[11px] text-center py-6" style={{ color: "#3d4450" }}>
                  No top-ranked underlying's ATM option pair cleared the Trade Decision Engine's own side pick.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table>
                    <thead>
                      <tr>
                        {["Underlying", "Underlying Qty", "Side", "Entry", "Partial", "Target", "Stop", "R:R", "Confidence", "Tier"].map((h) => (
                          <th key={h}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {result.option_legs.map((leg) => (
                        <tr key={leg.underlying}>
                          <td style={{ fontWeight: 600 }}>{leg.underlying}</td>
                          <td style={{ color: "#5a6270" }}>{leg.underlying_confidence}</td>
                          <td style={{ color: leg.side === "call" ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                            {leg.side === "call" ? "BUY CE" : "BUY PE"}
                          </td>
                          <td>₹{leg.entry}</td>
                          <td style={{ color: "#5a6270" }}>₹{leg.partial}</td>
                          <td style={{ color: "#5a6270" }}>₹{leg.target}</td>
                          <td style={{ color: "#5a6270" }}>₹{leg.stop}</td>
                          <td>{leg.rr}</td>
                          <td style={{ fontWeight: 600 }}>{leg.confidence}</td>
                          <td className="uppercase" style={{
                            color: leg.tier === "strong" ? "#00e87b" : leg.tier === "moderate" ? "#e8c300" : "#ff3e3e",
                            fontWeight: 600,
                          }}>
                            {leg.tier}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="text-[9px] mt-3" style={{ color: "#3d4450" }}>
                For each top-ranked underlying with a clear direction, resolves the ATM CE (bullish) or PE (bearish) at
                the nearest expiry and runs it through the same Trade Decision Engine as the Options tab. Only kept
                when the engine's own side pick agrees with the underlying's direction.
              </p>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
