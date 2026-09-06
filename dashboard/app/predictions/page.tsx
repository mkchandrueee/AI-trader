"use client";

import { useState } from "react";
import Sidebar from "@/components/Sidebar";
import { fetchJSON } from "@/lib/api";

interface ForecastDay {
  date: string;
  high: number;
  low: number;
  close: number;
  prev_close: number;
  signal: {
    label: string;
    confidence: number;
    regime_adjusted: boolean;
    regime_direction: string;
  } | null;
}

interface RegimeInfo {
  regime: string;
  recommended_model: string;
  sufficient_data: boolean;
  sma_fast: number | null;
  sma_slow: number | null;
  description: string;
}

interface ForecastResponse {
  symbol: string;
  algorithm: string;
  display_name: string;
  forecast: ForecastDay[];
  rmse: number;
  regime: RegimeInfo;
  cache_status: string;
  error?: string;
}

const ALGOS = [
  { value: "lstm", label: "LSTM" },
  { value: "bilstm", label: "BiLSTM" },
  { value: "gru", label: "GRU" },
  { value: "cnn_lstm", label: "CNN-LSTM" },
];

const regimeColor: Record<string, string> = {
  BULL: "#00e87b", BEAR: "#ff3e3e", SIDEWAYS: "#e8c300", UNKNOWN: "#5a6270",
};

export default function PredictionsPage() {
  const [symbol, setSymbol] = useState("NIFTY");
  const [algorithm, setAlgorithm] = useState("lstm");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ForecastResponse | null>(null);

  const runForecast = async (forceRetrain = false) => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchJSON<ForecastResponse>(
        `/api/predictions/forecast?symbol=${encodeURIComponent(symbol)}&algorithm=${algorithm}&force_retrain=${forceRetrain}`
      );
      if (data.error) throw new Error(data.error);
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col min-h-screen overflow-hidden">
        <main className="flex-1 p-5 overflow-y-auto">
          <div className="mb-5">
            <h1 className="text-sm font-bold uppercase tracking-wider" style={{ color: "#00e87b" }}>
              AI Forecast
            </h1>
            <p className="text-[10px] mt-0.5" style={{ color: "#5a6270" }}>
              5-day High/Low/Close forecast + BUY/HOLD/SELL signal, via LSTM/BiLSTM/GRU/CNN-LSTM
              (NSE-Neuron, free jugaad-data historical bars). Not investment advice.
            </p>
          </div>

          {/* Controls */}
          <div className="t-panel p-4 mb-5 flex flex-wrap items-end gap-3">
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Symbol</label>
              <input
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                onKeyDown={(e) => e.key === "Enter" && runForecast(false)}
                className="px-3 py-[6px] text-[12px] w-40"
                style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }}
                placeholder="NIFTY, RELIANCE, ..."
              />
            </div>
            <div>
              <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Model</label>
              <select
                value={algorithm}
                onChange={(e) => setAlgorithm(e.target.value)}
                className="px-3 py-[6px] text-[12px]"
                style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }}
              >
                {ALGOS.map((a) => (
                  <option key={a.value} value={a.value}>{a.label}</option>
                ))}
              </select>
            </div>
            <button onClick={() => runForecast(false)} disabled={loading} className="t-btn t-btn-green px-4 py-[6px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50">
              {loading ? "Running…" : "Run Forecast"}
            </button>
            <button onClick={() => runForecast(true)} disabled={loading} className="t-btn px-4 py-[6px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50">
              Force Retrain
            </button>
            <p className="text-[9px]" style={{ color: "#3d4450" }}>
              First run trains from scratch (can take a while) — cached afterwards until stale.
            </p>
          </div>

          {error && (
            <div className="t-panel p-4 mb-5" style={{ borderColor: "#ff3e3e" }}>
              <p className="text-[11px]" style={{ color: "#ff3e3e" }}>ERROR: {error}</p>
            </div>
          )}

          {result && (
            <>
              {/* Regime + meta */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-[1px] mb-5">
                <div className="t-panel p-3">
                  <div className="text-[9px] uppercase" style={{ color: "#5a6270" }}>Regime</div>
                  <div className="text-[14px] font-bold" style={{ color: regimeColor[result.regime.regime] || "#c8cdd5" }}>
                    {result.regime.regime}
                  </div>
                </div>
                <div className="t-panel p-3">
                  <div className="text-[9px] uppercase" style={{ color: "#5a6270" }}>Recommended Model</div>
                  <div className="text-[14px] font-bold" style={{ color: "#4da6ff" }}>{result.regime.recommended_model}</div>
                </div>
                <div className="t-panel p-3">
                  <div className="text-[9px] uppercase" style={{ color: "#5a6270" }}>RMSE</div>
                  <div className="text-[14px] font-bold">{result.rmse}</div>
                </div>
                <div className="t-panel p-3">
                  <div className="text-[9px] uppercase" style={{ color: "#5a6270" }}>Cache</div>
                  <div className="text-[14px] font-bold uppercase">{result.cache_status}</div>
                </div>
              </div>

              <p className="text-[10px] mb-4" style={{ color: "#5a6270" }}>{result.regime.description}</p>

              {/* Forecast table */}
              <div className="t-panel p-4">
                <h3 className="text-[11px] font-semibold mb-3 uppercase tracking-wider" style={{ color: "#5a6270" }}>
                  {result.display_name} — 5-Day Forecast for {result.symbol}
                </h3>
                <div className="overflow-x-auto">
                  <table>
                    <thead>
                      <tr>
                        {["Date", "High", "Low", "Close", "Signal", "Confidence"].map((h) => (
                          <th key={h}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {result.forecast.map((day) => (
                        <tr key={day.date}>
                          <td>{day.date}</td>
                          <td>₹{day.high.toLocaleString("en-IN")}</td>
                          <td>₹{day.low.toLocaleString("en-IN")}</td>
                          <td style={{ fontWeight: 600 }}>₹{day.close.toLocaleString("en-IN")}</td>
                          <td>
                            {day.signal ? (
                              <span style={{
                                color: day.signal.label === "BUY" ? "#00e87b" : day.signal.label === "SELL" ? "#ff3e3e" : "#e8c300",
                                fontWeight: 600,
                              }}>
                                {day.signal.label}
                              </span>
                            ) : "—"}
                          </td>
                          <td>{day.signal ? `${day.signal.confidence}%` : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="text-[9px] mt-3" style={{ color: "#3d4450" }}>
                  A model fit to past price shape, not a guarantee — see the Regime + RMSE above for how
                  much to trust it. Not investment advice; this project only paper-trades.
                </p>
              </div>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
