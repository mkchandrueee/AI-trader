"use client";

import { useCallback, useEffect, useState } from "react";
import Sidebar from "@/components/Sidebar";
import { API_BASE } from "@/lib/api";
import { RefreshCw, ShieldCheck } from "lucide-react";

interface Pick {
  symbol: string;
  kind: string;
  sector: string;
  side: "LONG" | "SHORT";
  scanner_confidence: number;
  forecast_label: string;
  forecast_confidence: number | null;
  forecast_close: number | null;
  close: number;
  entry: number;
  target1: number;
  target2: number;
  stop_loss: number;
  rr: number | null;
  qty: number;
}

interface CheckedRow {
  symbol: string;
  kept: boolean;
  reason: string;
  scanner_side?: string;
  forecast_label?: string;
}

interface PicksResponse {
  session_date?: string;
  min_confidence?: number;
  scanned?: number;
  qualified?: number;
  considered?: number;
  picks?: Pick[];
  checked?: CheckedRow[];
  forecast_errors?: string[];
  generated_at?: string;
  error?: string;
}

interface DeliveryPosition {
  id: string;
  symbol: string;
  side: "LONG" | "SHORT";
  qty: number;
  entry_price: number;
  status: string;
  entry_time: string;
  exit_price?: number;
  pnl?: number;
  scanner_confidence?: number;
  forecast_label?: string;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  return res.json();
}

async function postJSON<T>(path: string, body: object): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

export default function DeliveryPage() {
  const [minConfidence, setMinConfidence] = useState(100);
  const [topN, setTopN] = useState(10);
  const [data, setData] = useState<PicksResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [positions, setPositions] = useState<DeliveryPosition[]>([]);
  const [entering, setEntering] = useState<string | null>(null);

  const loadPositions = useCallback(() => {
    getJSON<{ positions: DeliveryPosition[] }>("/api/agent/delivery/positions")
      .then((d) => setPositions(d.positions || []))
      .catch(() => {});
  }, []);

  useEffect(() => { loadPositions(); }, [loadPositions]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await getJSON<PicksResponse>(
        `/api/agent/delivery/picks?min_confidence=${minConfidence}&top_n=${topN}`
      );
      if (d.error) throw new Error(d.error);
      setData(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [minConfidence, topN]);

  const placeOrder = useCallback(async (p: Pick) => {
    setEntering(p.symbol);
    try {
      await postJSON("/api/agent/delivery/enter", {
        symbol: p.symbol, side: p.side, qty: p.qty, entry_price: p.close,
        target1: p.target1, target2: p.target2, stop_loss: p.stop_loss,
        scanner_confidence: p.scanner_confidence, forecast_label: p.forecast_label,
      });
      loadPositions();
    } catch { /* positions reload will resync */ }
    finally { setEntering(null); }
  }, [loadPositions]);

  const dropped = (data?.checked || []).filter((c) => !c.kept);

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col min-h-screen overflow-hidden">
        <main className="flex-1 p-5 overflow-y-auto">
          <div className="mb-5">
            <h1 className="text-sm font-bold uppercase tracking-wider flex items-center gap-2" style={{ color: "#00e87b" }}>
              <ShieldCheck className="w-4 h-4" /> Delivery / Swing
            </h1>
            <p className="text-[10px] mt-0.5" style={{ color: "#5a6270" }}>
              Market Scanner&apos;s highest candle-quality rows, kept only where AI Forecast&apos;s next-day call points the
              same way. Two independent readings agreeing — one candle-shape, one trained sequence model. Suggest-only:
              nothing is placed until you click. Paper positions.
            </p>
          </div>

          {/* Controls */}
          <div className="t-panel p-4 mb-5 flex flex-wrap items-end gap-3">
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Min candle quality</label>
              <input type="number" min={0} max={100} value={minConfidence}
                onChange={(e) => setMinConfidence(Number(e.target.value))}
                className="px-3 py-[6px] text-[12px] w-24" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }} />
            </div>
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Confirm top N</label>
              <input type="number" min={1} max={25} value={topN}
                onChange={(e) => setTopN(Number(e.target.value))}
                className="px-3 py-[6px] text-[12px] w-24" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }} />
            </div>
            <button onClick={refresh} disabled={loading}
              className="t-btn t-btn-green flex items-center gap-1.5 px-4 py-[6px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50">
              <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} /> {loading ? "Confirming…" : "Refresh picks"}
            </button>
            <span className="text-[9px]" style={{ color: "#3d4450" }}>
              Each confirmation trains or loads a per-symbol model — slow by nature, hence the cap.
            </span>
          </div>

          {error && (
            <div className="t-panel p-4 mb-5" style={{ borderColor: "#ff3e3e" }}>
              <p className="text-[11px]" style={{ color: "#ff3e3e" }}>ERROR: {error}</p>
            </div>
          )}

          {data && (
            <div className="t-panel p-4 mb-5">
              <div className="flex flex-wrap items-center gap-2 mb-3">
                <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "#5a6270" }}>
                  Confirmed picks ({data.picks?.length ?? 0})
                </h3>
                <span className="text-[9px]" style={{ color: "#3d4450" }}>
                  session {data.session_date} · {data.scanned} scanned · {data.qualified} at/above {data.min_confidence}% · {data.considered} confirmed against forecast
                </span>
              </div>

              {!data.picks?.length ? (
                <p className="text-[11px] text-center py-6" style={{ color: "#3d4450" }}>
                  No row had both readings agree. {dropped.length > 0 && `${dropped.length} candidate(s) checked and dropped — see below.`}
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table>
                    <thead>
                      <tr>
                        {["Symbol", "Sector", "Side", "Scan", "Forecast", "Close", "Entry", "T1", "T2", "Stop", "R:R", "Qty", ""].map((h) => <th key={h}>{h}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {data.picks.map((p) => (
                        <tr key={p.symbol}>
                          <td style={{ fontWeight: 600 }}>{p.symbol}</td>
                          <td style={{ color: "#5a6270" }}>{p.sector || "—"}</td>
                          <td style={{ color: p.side === "LONG" ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>{p.side}</td>
                          <td style={{ fontWeight: 600 }}>{p.scanner_confidence}%</td>
                          <td style={{ color: p.forecast_label === "BUY" ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                            {p.forecast_label}
                            {p.forecast_confidence != null && (
                              <span style={{ color: "#5a6270", fontWeight: 400 }}> {Math.round(p.forecast_confidence * 100)}%</span>
                            )}
                          </td>
                          <td>₹{p.close.toLocaleString("en-IN")}</td>
                          <td>₹{p.entry.toLocaleString("en-IN")}</td>
                          <td style={{ color: "#00e87b" }}>₹{p.target1.toLocaleString("en-IN")}</td>
                          <td style={{ color: "#00e87b" }}>₹{p.target2.toLocaleString("en-IN")}</td>
                          <td style={{ color: "#ff3e3e" }}>₹{p.stop_loss.toLocaleString("en-IN")}</td>
                          <td style={{ color: "#5a6270" }}>{p.rr ?? "—"}</td>
                          <td>{p.qty}</td>
                          <td>
                            <button onClick={() => placeOrder(p)} disabled={entering === p.symbol}
                              className="t-btn t-btn-green px-2 py-[3px] text-[9px] font-semibold uppercase tracking-wider disabled:opacity-50">
                              {entering === p.symbol ? "…" : "Place"}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {dropped.length > 0 && (
                <details className="mt-3">
                  <summary className="text-[10px] cursor-pointer" style={{ color: "#5a6270" }}>
                    Dropped ({dropped.length}) — checked but the two readings disagreed
                  </summary>
                  <div className="mt-2">
                    {dropped.map((c) => (
                      <div key={c.symbol} className="text-[9px] py-[2px]" style={{ color: "#5a6270" }}>
                        <b style={{ color: "#c8cdd5" }}>{c.symbol}</b> — {c.reason}
                      </div>
                    ))}
                  </div>
                </details>
              )}

              <p className="text-[9px] mt-3" style={{ color: "#3d4450" }}>
                Candle quality measures one completed candle&apos;s shape; the forecast is a trained model over daily
                bars. Neither is a probability of profit, and agreement between them is not one either.
              </p>
            </div>
          )}

          {/* Paper positions */}
          {positions.length > 0 && (
            <div className="t-panel p-4">
              <h3 className="text-[11px] font-semibold uppercase tracking-wider mb-3" style={{ color: "#5a6270" }}>
                Delivery paper positions ({positions.length})
              </h3>
              <div className="overflow-x-auto">
                <table>
                  <thead>
                    <tr>{["Symbol", "Side", "Qty", "Entry", "Status", "Entered", "P&L"].map((h) => <th key={h}>{h}</th>)}</tr>
                  </thead>
                  <tbody>
                    {positions.map((p) => (
                      <tr key={p.id}>
                        <td style={{ fontWeight: 600 }}>{p.symbol}</td>
                        <td style={{ color: p.side === "LONG" ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>{p.side}</td>
                        <td>{p.qty}</td>
                        <td>₹{p.entry_price.toLocaleString("en-IN")}</td>
                        <td style={{ color: p.status === "OPEN" ? "#e8c300" : "#5a6270" }}>{p.status}</td>
                        <td style={{ color: "#5a6270" }}>{p.entry_time?.slice(0, 16).replace("T", " ")}</td>
                        <td style={{ color: (p.pnl ?? 0) >= 0 ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                          {p.pnl != null ? `₹${p.pnl >= 0 ? "+" : ""}${p.pnl.toLocaleString("en-IN")}` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
