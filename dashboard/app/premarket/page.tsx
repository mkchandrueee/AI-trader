"use client";

import { useCallback, useEffect, useState } from "react";
import Sidebar from "@/components/Sidebar";
import { API_BASE } from "@/lib/api";
import { RefreshCw, Sunrise, Zap } from "lucide-react";

interface NextDayReading {
  symbol: string;
  session_date: string;
  prev_high: number;
  prev_low: number;
  prev_close: number;
  direction: "bullish" | "bearish" | null;
  entry: number;
  stop: number;
  target1: number;
  target2: number;
  risk: number;
  reward: number;
  rr: number;
  error?: string;
}

interface LiveConfirmation {
  symbol: string;
  timeframe: string;
  mode: "opening" | "latest";
  session_date: string;
  is_live: boolean;
  spot: number;
  atm: number;
  expiry: string;
  ce_symbol: string;
  pe_symbol: string;
  ce_candle: { timestamp: string; open: number; high: number; low: number; close: number };
  pe_candle: { timestamp: string; open: number; high: number; low: number; close: number };
  side: "call" | "put" | null;
  tradable: boolean;
  entry: number;
  partial: number;
  target: number;
  stop: number;
  risk: number;
  rr: number;
  confidence: number;
  tier: "strong" | "moderate" | "avoid";
  verdict: "take" | "caution" | "wait";
  blockers: string[];
  warnings: string[];
  nextday_direction: "bullish" | "bearish" | null;
  opening_side: "call" | "put" | null;
  agrees_with_nextday: boolean | null;
  agrees_with_opening: boolean | null;
  error?: string;
}

const INDEX_OPTIONS = [
  { value: "NIFTY", label: "NIFTY" },
  { value: "BANKNIFTY", label: "BANKNIFTY" },
  { value: "FINNIFTY", label: "FINNIFTY" },
  { value: "MIDCPNIFTY", label: "MIDCPNIFTY" },
  { value: "NIFTYNXT50", label: "NIFTY NEXT 50" },
  { value: "SENSEX", label: "SENSEX" },
];

interface AgentPosition {
  symbol: string;
  option_symbol: string;
  side: "call" | "put";
  mode: string;
  qty: number;
  entry: number;
  exit_target: number;
  stop: number;
  confidence: number | null;
  status: string;
  current_premium?: number;
  unrealised_pnl?: number;
  exit_price?: number;
  exit_reason?: string;
  pnl?: number;
}

interface AgentStatus {
  armed: boolean;
  session_date: string | null;
  open_positions: AgentPosition[];
  closed_today: AgentPosition[];
  log: { time: string; symbol: string; action: string; detail: string }[];
  last_cycle: string | null;
  symbols: string[];
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  return res.json(); // read the body regardless of status — error payloads carry {error}
}

async function postJSONRaw<T>(path: string, body: object): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

function AgreementChip({ label, value }: { label: string; value: boolean | null }) {
  const color = value === null ? "#5a6270" : value ? "#00e87b" : "#ff3e3e";
  const text = value === null ? "N/A" : value ? "AGREES" : "DISAGREES";
  return (
    <span className="px-2 py-[3px] text-[9px] uppercase tracking-wider" style={{ background: "#181c24", border: `1px solid ${color}`, color }}>
      {label}: <b>{text}</b>
    </span>
  );
}

export default function PreMarketPage() {
  const [symbol, setSymbol] = useState("NIFTY");
  const [nextday, setNextday] = useState<NextDayReading | null>(null);
  const [nextdayLoading, setNextdayLoading] = useState(false);
  const [nextdayError, setNextdayError] = useState<string | null>(null);

  const [live, setLive] = useState<LiveConfirmation | null>(null);
  const [liveLoading, setLiveLoading] = useState<string | null>(null); // which button is loading
  const [liveError, setLiveError] = useState<string | null>(null);

  const [agent, setAgent] = useState<AgentStatus | null>(null);
  const [agentBusy, setAgentBusy] = useState(false);

  // Poll the agent every 10s — it acts on its own schedule, so the panel has
  // to pull rather than only refreshing on user action.
  useEffect(() => {
    let alive = true;
    const pull = () => {
      getJSON<AgentStatus>("/api/agent/intraday/status")
        .then((d) => { if (alive && d && typeof d.armed === "boolean") setAgent(d); })
        .catch(() => {});
    };
    pull();
    const id = setInterval(pull, 10_000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const toggleAgent = useCallback(async (armed: boolean) => {
    setAgentBusy(true);
    try {
      const d = await postJSONRaw<AgentStatus>("/api/agent/intraday/arm", { armed });
      if (d && typeof d.armed === "boolean") setAgent(d);
    } catch { /* status poll will resync */ }
    finally { setAgentBusy(false); }
  }, []);

  const runNextday = useCallback(async (sym: string) => {
    setNextdayLoading(true);
    setNextdayError(null);
    try {
      const data = await getJSON<NextDayReading>(`/api/premarket/nextday?symbol=${sym}`);
      if (data.error) throw new Error(data.error);
      setNextday(data);
    } catch (e) {
      setNextdayError(e instanceof Error ? e.message : String(e));
      setNextday(null);
    } finally {
      setNextdayLoading(false);
    }
  }, []);

  const runLive = useCallback(async (timeframe: string, mode: "opening" | "latest") => {
    const key = `${mode}-${timeframe}`;
    setLiveLoading(key);
    setLiveError(null);
    try {
      const data = await getJSON<LiveConfirmation>(`/api/premarket/live?symbol=${symbol}&timeframe=${timeframe}&mode=${mode}`);
      if (data.error) throw new Error(data.error);
      setLive(data);
    } catch (e) {
      setLiveError(e instanceof Error ? e.message : String(e));
      setLive(null);
    } finally {
      setLiveLoading(null);
    }
  }, [symbol]);

  const dirColor = (d: string | null) => (d === "bullish" ? "#00e87b" : d === "bearish" ? "#ff3e3e" : "#5a6270");
  const sideLabel = (s: string | null) => (s === "call" ? "BUY CALL" : s === "put" ? "BUY PUT" : "NO CLEAR SIDE");
  const sideColor = (s: string | null) => (s === "call" ? "#00e87b" : s === "put" ? "#ff3e3e" : "#5a6270");

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col min-h-screen overflow-hidden">
        <main className="flex-1 p-5 overflow-y-auto">
          <div className="mb-5">
            <h1 className="text-sm font-bold uppercase tracking-wider flex items-center gap-2" style={{ color: "#00e87b" }}>
              <Sunrise className="w-4 h-4" /> Pre Market
            </h1>
            <p className="text-[10px] mt-0.5" style={{ color: "#5a6270" }}>
              NextDay Direction Analyser reads the last completed session's pivots before the bell. The Option Trade
              Decision Engine — Live re-runs the same engine as the Options tab against the opening or latest closed
              ATM candle, and checks whether it still agrees.
            </p>
          </div>

          {/* Intraday agent */}
          {agent && (
            <div className="t-panel p-4 mb-5" style={{ borderColor: agent.armed ? "#1a5c3a" : "#252a33" }}>
              <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
                <div className="flex items-center gap-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "#5a6270" }}>
                    Intraday Agent
                  </h2>
                  <span className="px-2 py-[3px] text-[9px] font-bold uppercase tracking-wider" style={{
                    background: agent.armed ? "#0a2a18" : "#2a0a0a",
                    border: `1px solid ${agent.armed ? "#00e87b" : "#5c1a1a"}`,
                    color: agent.armed ? "#00e87b" : "#ff3e3e",
                  }}>
                    {agent.armed ? "● ARMED" : "○ DISARMED"}
                  </span>
                  <span className="text-[9px]" style={{ color: "#3d4450" }}>PAPER ONLY · 1 LOT · EXITS AT PARTIAL</span>
                </div>
                <button onClick={() => toggleAgent(!agent.armed)} disabled={agentBusy}
                  className={`px-4 py-[6px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50 ${agent.armed ? "" : "t-btn t-btn-green"}`}
                  style={agent.armed ? { background: "#2a0a0a", border: "1px solid #5c1a1a", color: "#ff3e3e" } : undefined}>
                  {agentBusy ? "…" : agent.armed ? "Stop agent" : "Arm agent"}
                </button>
              </div>

              <p className="text-[10px] mb-3" style={{ color: "#5a6270" }}>
                Reads the Trade Decision Engine on {`${agent.symbols.join(" / ")} `}— the fixed 09:15–09:20 opening candle
                and each newly closed 5-minute candle. On a tradable side it takes one lot and closes the whole
                position at the engine&apos;s partial-book level. Starts disarmed after every backend restart.
              </p>

              {agent.open_positions.length > 0 && (
                <div className="overflow-x-auto mb-3">
                  <table>
                    <thead>
                      <tr>{["Symbol", "Leg", "Side", "Qty", "Entry", "Exit at", "Stop", "Now", "Unreal. P&L"].map((h) => <th key={h}>{h}</th>)}</tr>
                    </thead>
                    <tbody>
                      {agent.open_positions.map((p) => (
                        <tr key={p.option_symbol}>
                          <td style={{ fontWeight: 600 }}>{p.symbol}</td>
                          <td style={{ color: "#5a6270" }}>{p.option_symbol}</td>
                          <td style={{ color: p.side === "call" ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                            {p.side === "call" ? "BUY CE" : "BUY PE"}
                          </td>
                          <td>{p.qty}</td>
                          <td>₹{p.entry}</td>
                          <td style={{ color: "#00e87b" }}>₹{p.exit_target}</td>
                          <td style={{ color: "#ff3e3e" }}>₹{p.stop}</td>
                          <td>{p.current_premium != null ? `₹${p.current_premium}` : "—"}</td>
                          <td style={{ color: (p.unrealised_pnl ?? 0) >= 0 ? "#00e87b" : "#ff3e3e", fontWeight: 600 }}>
                            {p.unrealised_pnl != null ? `₹${p.unrealised_pnl >= 0 ? "+" : ""}${p.unrealised_pnl.toLocaleString("en-IN")}` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {agent.closed_today.length > 0 && (
                <div className="flex flex-wrap gap-2 mb-3">
                  {agent.closed_today.map((p, i) => (
                    <span key={`${p.option_symbol}-${i}`} className="px-2 py-[3px] text-[9px] uppercase tracking-wider"
                      style={{
                        background: "#181c24",
                        border: `1px solid ${(p.pnl ?? 0) >= 0 ? "#1a5c3a" : "#5c1a1a"}`,
                        color: (p.pnl ?? 0) >= 0 ? "#00e87b" : "#ff3e3e",
                      }}>
                      {p.symbol} {p.exit_reason} ₹{(p.pnl ?? 0) >= 0 ? "+" : ""}{(p.pnl ?? 0).toLocaleString("en-IN")}
                    </span>
                  ))}
                </div>
              )}

              {agent.log.length > 0 && (
                <details>
                  <summary className="text-[10px] cursor-pointer" style={{ color: "#5a6270" }}>
                    Decision log ({agent.log.length}) — every check, fired or skipped
                  </summary>
                  <div className="mt-2 max-h-40 overflow-y-auto">
                    {agent.log.map((l, i) => (
                      <div key={i} className="text-[9px] py-[2px]" style={{ color: "#5a6270" }}>
                        <span style={{ color: "#3d4450" }}>{l.time}</span>{" "}
                        <b style={{ color: l.action === "ENTER" ? "#00e87b" : l.action === "EXIT" ? "#4da6ff" : "#5a6270" }}>
                          {l.symbol} {l.action}
                        </b>{" "}
                        {l.detail}
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          )}

          {/* NextDay Direction Analyser */}
          <div className="t-panel p-4 mb-5">
            <h2 className="text-[11px] font-semibold uppercase tracking-wider mb-3" style={{ color: "#5a6270" }}>
              NextDay Direction Analyser
            </h2>
            <div className="flex flex-wrap items-end gap-3 mb-4">
              <div>
                <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Index</label>
                <select value={symbol} onChange={(e) => setSymbol(e.target.value)}
                  className="px-3 py-[6px] text-[12px]" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#c8cdd5" }}>
                  {INDEX_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </div>
              <button onClick={() => runNextday(symbol)} disabled={nextdayLoading}
                className="t-btn t-btn-green flex items-center gap-1.5 px-4 py-[6px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50">
                <RefreshCw className={`w-3 h-3 ${nextdayLoading ? "animate-spin" : ""}`} /> {nextdayLoading ? "Reading…" : "Fetch Reading"}
              </button>
            </div>

            {nextdayError && <p className="text-[11px]" style={{ color: "#ff3e3e" }}>ERROR: {nextdayError}</p>}

            {nextday && (
              <>
                <div className="flex items-center gap-3 mb-3">
                  <span className="px-3 py-1 text-[13px] font-bold uppercase tracking-wider"
                    style={{ background: "#181c24", border: `1px solid ${dirColor(nextday.direction)}`, color: dirColor(nextday.direction) }}>
                    {nextday.direction ? nextday.direction.toUpperCase() : "NO CLEAR DIRECTION"}
                  </span>
                  <span className="text-[10px]" style={{ color: "#5a6270" }}>
                    session {nextday.session_date} — prev H {nextday.prev_high} / L {nextday.prev_low} / C {nextday.prev_close}
                  </span>
                </div>
                <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
                  {[
                    ["Pivot (Entry)", nextday.entry], ["Stop", nextday.stop],
                    ["Target 1", nextday.target1], ["Target 2", nextday.target2],
                    ["Risk", nextday.risk], ["R:R", nextday.rr],
                  ].map(([label, value]) => (
                    <div key={label as string} className="p-2" style={{ background: "#181c24", border: "1px solid #252a33" }}>
                      <div className="text-[8px] uppercase tracking-wider" style={{ color: "#5a6270" }}>{label}</div>
                      <div className="text-[13px] font-semibold">{value}</div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>

          {/* Option Trade Decision Engine — Live */}
          <div className="t-panel p-4">
            <div className="flex items-center gap-2 mb-1">
              <span className="px-2 py-[2px] text-[9px] font-semibold uppercase tracking-wider" style={{ background: "#181c24", border: "1px solid #e8c300", color: "#e8c300" }}>
                Live Confirmation
              </span>
            </div>
            <h2 className="text-[13px] font-bold flex items-center gap-1.5 mb-1">
              <Zap className="w-3.5 h-3.5" style={{ color: "#4da6ff" }} /> Option Trade Decision Engine — Live
            </h2>
            <p className="text-[10px] mb-4" style={{ color: "#5a6270" }}>
              Uses the latest fully closed 5-minute ATM CE and PE candle to confirm whether the current market
              direction still agrees with the opening and Pre Market readings. Reads whichever index is selected
              above (NIFTY / BANKNIFTY / SENSEX have listed options) — needs an active AngelOne session
              (Connect via the sidebar).
            </p>

            <h3 className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: "#5a6270" }}>
              Opening and latest closed option candles
            </h3>
            <p className="text-[9px] mb-3" style={{ color: "#3d4450" }}>
              Fetch the fixed 09:15–09:20 opening candle or the latest fully closed 5, 15, 30 or 60-minute ATM call
              and put candle. A candle still forming is never analysed.
            </p>
            <div className="flex flex-wrap gap-2 mb-4">
              {[
                { key: "opening-5min", label: "Fetch first 5-min candle", tf: "5min", mode: "opening" as const },
                { key: "latest-5min", label: "Fetch latest closed 5-min candle", tf: "5min", mode: "latest" as const },
                { key: "latest-15min", label: "Fetch latest closed 15-min candle", tf: "15min", mode: "latest" as const },
                { key: "latest-30min", label: "Fetch latest closed 30-min candle", tf: "30min", mode: "latest" as const },
                { key: "latest-60min", label: "Fetch latest closed 1-hour candle", tf: "60min", mode: "latest" as const },
              ].map((b) => (
                <button key={b.key} onClick={() => runLive(b.tf, b.mode)} disabled={liveLoading !== null}
                  className={`px-3 py-[8px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50 ${b.mode === "latest" && b.tf === "5min" ? "t-btn-green" : ""}`}
                  style={b.mode === "latest" && b.tf === "5min"
                    ? undefined
                    : { background: "#181c24", border: "1px solid #252a33", color: "#c8cdd5" }}>
                  {liveLoading === b.key ? "Fetching…" : b.label}
                </button>
              ))}
            </div>

            {liveError && <p className="text-[11px] mb-3" style={{ color: "#ff3e3e" }}>ERROR: {liveError}</p>}

            {live && (
              <div className="pt-3" style={{ borderTop: `1px solid ${live.is_live ? "#252a33" : "#e8c30055"}` }}>
                <div className="flex flex-wrap items-center gap-2 mb-2">
                  <span className="px-2 py-[3px] text-[9px] font-bold uppercase tracking-wider"
                    style={{
                      background: live.is_live ? "#00e87b22" : "#e8c30022",
                      border: `1px solid ${live.is_live ? "#00e87b" : "#e8c300"}`,
                      color: live.is_live ? "#00e87b" : "#e8c300",
                    }}>
                    {live.is_live ? "● LIVE" : `○ PREVIOUS SESSION — ${live.session_date}`}
                  </span>
                  {!live.is_live && (
                    <span className="text-[9px]" style={{ color: "#e8c300" }}>
                      Market is closed — showing the last session's data, not a live read.
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-2 mb-3">
                  <span className="px-3 py-1 text-[13px] font-bold uppercase tracking-wider"
                    style={{ background: "#181c24", border: `1px solid ${sideColor(live.side)}`, color: sideColor(live.side) }}>
                    {live.side ? `${sideLabel(live.side)} — ${live.verdict === "take" ? "CONFIRMED BREAKOUT" : live.verdict.toUpperCase()}` : "WAIT — NO CLEAR SIDE"}
                  </span>
                  <span className="text-[10px]" style={{ color: "#5a6270" }}>
                    {live.mode === "opening" ? "09:15–09:20 opening candle" : `latest closed ${live.timeframe}`} — candle {live.ce_candle.timestamp}
                  </span>
                </div>

                <div className="flex flex-wrap gap-2 mb-3">
                  <AgreementChip label="vs Pre-Market (NextDay)" value={live.agrees_with_nextday} />
                  {live.mode !== "opening" && <AgreementChip label="vs Opening Reading" value={live.agrees_with_opening} />}
                </div>

                <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 mb-3">
                  {[
                    ["Entry", live.entry], ["Partial", live.partial], ["Target", live.target],
                    ["Stop", live.stop], ["Risk", live.risk], ["R:R", live.rr],
                  ].map(([label, value]) => (
                    <div key={label as string} className="p-2" style={{ background: "#181c24", border: "1px solid #252a33" }}>
                      <div className="text-[8px] uppercase tracking-wider" style={{ color: "#5a6270" }}>{label}</div>
                      <div className="text-[13px] font-semibold">{value}</div>
                    </div>
                  ))}
                </div>

                <div className="flex flex-wrap items-center gap-3 mb-3 text-[10px]" style={{ color: "#5a6270" }}>
                  <span>ATM {live.atm} · Expiry {live.expiry}</span>
                  <span>Confidence <b style={{ color: "#c8cdd5" }}>{live.confidence}</b></span>
                  <span className="uppercase" style={{ color: live.tier === "strong" ? "#00e87b" : live.tier === "moderate" ? "#e8c300" : "#ff3e3e" }}>
                    {live.tier}
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-3 text-[10px]">
                  <div>
                    <div className="uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>CE {live.ce_symbol}</div>
                    <div style={{ color: "#c8cdd5" }}>O {live.ce_candle.open} H {live.ce_candle.high} L {live.ce_candle.low} C {live.ce_candle.close}</div>
                  </div>
                  <div>
                    <div className="uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>PE {live.pe_symbol}</div>
                    <div style={{ color: "#c8cdd5" }}>O {live.pe_candle.open} H {live.pe_candle.high} L {live.pe_candle.low} C {live.pe_candle.close}</div>
                  </div>
                </div>

                {(live.blockers.length > 0 || live.warnings.length > 0) && (
                  <div className="flex flex-wrap gap-1.5 mt-3">
                    {live.blockers.map((b) => (
                      <span key={b} className="px-2 py-[2px] text-[9px] uppercase tracking-wider" style={{ background: "#181c24", border: "1px solid #ff3e3e", color: "#ff3e3e" }}>{b}</span>
                    ))}
                    {live.warnings.map((w) => (
                      <span key={w} className="px-2 py-[2px] text-[9px] uppercase tracking-wider" style={{ background: "#181c24", border: "1px solid #e8c300", color: "#e8c300" }}>{w}</span>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
