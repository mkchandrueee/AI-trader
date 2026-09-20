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

interface LadderTarget { level: number; pts: number; action: "BOOK" | "HOLD"; pct: number }
interface Ladder { entry: number; targets: LadderTarget[]; stop_loss: number; stop_pts: number }
interface ChecklistRow { key: string; label: string; state: "pass" | "fail" | "warn" | "skip"; value: string }
interface AnalyzerVerdict {
  decision: "clear" | "tie" | "noEdge" | "insufficient";
  side: "call" | "put" | null;
  leader: "call" | "put";
  signal: string;
  headline: string;
  confidence: number;
  reason: string;
  entry: number | null;
  entry_note: string;
  rules: string[];
}
interface AnalyzerBreakdown {
  verdict: AnalyzerVerdict;
  scores: {
    call: number; put: number; margin: number;
    required_margin: number; required_confidence: number;
    leader: "call" | "put"; decision: string; side: "call" | "put" | null;
  };
  strength: { call_pct: number; put_pct: number; call_bullish: boolean; put_bullish: boolean };
  pcr: number | null;
  pcr_bias: string;
  checklist: ChecklistRow[];
  call_ladder: Ladder;
  put_ladder: Ladder;
}
interface PullbackEntry {
  side: "call" | "put" | null;
  wait: boolean;
  zones?: { zone1: number; zone2: number; zone3: number };
  stop_loss?: number;
  stop_pts?: number;
  targets?: { level: number; pts: number }[];
  rr?: number | null;
  cautions?: string[];
  rules?: string[];
}
interface ValueCalc {
  inputs: number[];
  average: number;
  sqrt_of_avg: number;
  call_level: number;
  call_target: number;
  put_level: number;
  put_target: number;
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
  analyzer: AnalyzerBreakdown;
  value_calc: ValueCalc;
  pullback: PullbackEntry;
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

interface PendingApproval {
  approval_id: string;
  strategy: string;
  symbol: string;
  side: string;
  option_symbol: string;
  entry: number;
  stop: number;
  target: number;
  confidence: number | null;
  tier: string | null;
  requested_at: string;
  expires_at: string;
  evidence_bundle: {
    n?: number;
    sufficient_sample?: boolean;
    win_rate?: number;
    win_rate_ci95?: [number, number];
    profit_factor?: number | null;
    expectancy_pct?: number | null;
  };
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

const STATE_ICON: Record<ChecklistRow["state"], { icon: string; color: string }> = {
  pass: { icon: "✓", color: "#00e87b" },
  fail: { icon: "✕", color: "#ff3e3e" },
  warn: { icon: "⚠", color: "#e8c300" },
  skip: { icon: "·", color: "#5a6270" },
};

function LadderTable({ title, ladder, color }: { title: string; ladder: Ladder; color: string }) {
  return (
    <div className="p-3" style={{ background: "#181c24", border: `1px solid ${color}44` }}>
      <div className="text-[10px] font-bold uppercase tracking-wider mb-2" style={{ color }}>● {title}</div>
      <table className="w-full text-[11px]">
        <tbody>
          <tr>
            <td style={{ color: "#5a6270" }}>ENTRY</td>
            <td className="text-right font-semibold" colSpan={3}>₹{ladder.entry.toFixed(2)}</td>
          </tr>
          {ladder.targets.map((t, i) => (
            <tr key={i}>
              <td style={{ color: "#5a6270" }}>TARGET {i + 1}</td>
              <td className="text-right font-semibold" style={{ color: "#00e87b" }}>₹{t.level.toFixed(2)}</td>
              <td className="text-right" style={{ color: "#3d4450" }}>+{t.pts}pts</td>
              <td className="text-right">
                <span className="px-1.5 py-[1px] text-[8px] font-bold uppercase tracking-wider"
                  style={{ background: t.action === "BOOK" ? "#0a2a18" : "#1a1a2a", color: t.action === "BOOK" ? "#00e87b" : "#4da6ff",
                           border: `1px solid ${t.action === "BOOK" ? "#1a5c3a" : "#252a5c"}` }}>
                  {t.action} {t.pct}%
                </span>
              </td>
            </tr>
          ))}
          <tr>
            <td style={{ color: "#5a6270" }}>STOP LOSS</td>
            <td className="text-right font-semibold" style={{ color: "#ff3e3e" }}>₹{ladder.stop_loss.toFixed(2)}</td>
            <td className="text-right" style={{ color: "#3d4450" }}>−{ladder.stop_pts}pts</td>
            <td className="text-right">
              <span className="px-1.5 py-[1px] text-[8px] font-bold uppercase tracking-wider"
                style={{ background: "#2a0a0a", color: "#ff3e3e", border: "1px solid #5c1a1a" }}>EXIT ALL</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function StrengthBar({ label, pct, bullish, color }: { label: string; pct: number; bullish: boolean; color: string }) {
  return (
    <div className="p-2" style={{ background: "#181c24", border: "1px solid #252a33" }}>
      <div className="flex items-center justify-between text-[9px] uppercase tracking-wider mb-1">
        <span style={{ color }}>{label}</span>
        <span style={{ color: "#c8cdd5" }}>{bullish ? "Bullish" : "Bearish"} {pct}%</span>
      </div>
      <div style={{ height: 4, background: "#0d0f14" }}>
        <div style={{ width: `${Math.min(100, Math.max(0, pct))}%`, height: 4, background: color }} />
      </div>
    </div>
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

  // Value Calculator - auto-fed from the fetched candles (live.value_calc);
  // the six inputs stay editable for what-if recalculation.
  const [vcInputs, setVcInputs] = useState<string[]>([]);
  const [vcResult, setVcResult] = useState<ValueCalc | null>(null);
  const [vcBusy, setVcBusy] = useState(false);
  const [vcError, setVcError] = useState<string | null>(null);
  const [vcEdited, setVcEdited] = useState(false);

  const [agent, setAgent] = useState<AgentStatus | null>(null);
  const [agentBusy, setAgentBusy] = useState(false);

  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [approvalBusy, setApprovalBusy] = useState<string | null>(null); // approval_id currently being decided
  const [now, setNow] = useState(() => Date.now()); // ticks the countdowns below

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

  // Pending approvals (AI-platform roadmap Phase 3) have a 120s TTL, so a
  // faster poll than the agent's own status — a stale approval sitting
  // unnoticed for 10s is a meaningfully worse experience than for a
  // passive status panel.
  useEffect(() => {
    let alive = true;
    const pull = () => {
      getJSON<PendingApproval[]>("/api/agent/intraday/approvals")
        .then((d) => { if (alive && Array.isArray(d)) setApprovals(d); })
        .catch(() => {});
    };
    pull();
    const id = setInterval(pull, 3_000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  // Local 1s ticker for the countdown display only — doesn't refetch.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);

  const toggleAgent = useCallback(async (armed: boolean) => {
    setAgentBusy(true);
    try {
      const d = await postJSONRaw<AgentStatus>("/api/agent/intraday/arm", { armed });
      if (d && typeof d.armed === "boolean") setAgent(d);
    } catch { /* status poll will resync */ }
    finally { setAgentBusy(false); }
  }, []);

  const decideApproval = useCallback(async (approvalId: string, action: "approve" | "reject") => {
    setApprovalBusy(approvalId);
    try {
      await postJSONRaw(`/api/agent/intraday/approvals/${approvalId}/${action}`, {});
    } catch { /* next poll will resync either way */ }
    finally {
      setApprovalBusy(null);
      setApprovals((prev) => prev.filter((a) => a.approval_id !== approvalId));
      getJSON<PendingApproval[]>("/api/agent/intraday/approvals").then(setApprovals).catch(() => {});
    }
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

  useEffect(() => {
    if (live?.value_calc && !live.value_calc.error) {
      setVcInputs(live.value_calc.inputs.map((v) => String(v)));
      setVcResult(live.value_calc);
      setVcEdited(false);
      setVcError(null);
    }
  }, [live]);

  const recalcValue = useCallback(async () => {
    setVcBusy(true);
    setVcError(null);
    try {
      const qs = vcInputs.map((v, i) => `v${i + 1}=${encodeURIComponent(v)}`).join("&");
      const data = await getJSON<ValueCalc>(`/api/premarket/value-calc?${qs}`);
      if (data.error) throw new Error(data.error);
      setVcResult(data);
    } catch (e) {
      setVcError(e instanceof Error ? e.message : String(e));
    } finally {
      setVcBusy(false);
    }
  }, [vcInputs]);

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

          {/* Pending approvals (AI-platform roadmap Phase 3) — only ever
              populated once TRADE_MODE leaves "paper"; empty in paper mode
              since the agent auto-fires directly there. Placed above the
              agent status panel deliberately: a decision waiting on a
              120s TTL is more urgent than passive status. */}
          {approvals.length > 0 && (
            <div className="t-panel p-4 mb-5" style={{ borderColor: "#e8c300" }}>
              <h2 className="text-[11px] font-semibold uppercase tracking-wider mb-3" style={{ color: "#e8c300" }}>
                ⚠ Pending Approval{approvals.length > 1 ? "s" : ""} ({approvals.length})
              </h2>
              {approvals.map((a) => {
                const secsLeft = Math.max(0, Math.round((new Date(a.expires_at).getTime() - now) / 1000));
                const eb = a.evidence_bundle || {};
                const busy = approvalBusy === a.approval_id;
                return (
                  <div key={a.approval_id} className="p-3 mb-2" style={{ background: "#181c24", border: "1px solid #3a3a1a" }}>
                    <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                      <div className="flex items-center gap-2">
                        <span style={{ color: a.side === "call" ? "#00e87b" : "#ff3e3e", fontWeight: 700 }}>
                          {a.side === "call" ? "BUY CE" : "BUY PE"}
                        </span>
                        <span style={{ fontWeight: 600 }}>{a.symbol}</span>
                        <span style={{ color: "#5a6270" }}>{a.option_symbol}</span>
                      </div>
                      <span className="px-2 py-[2px] text-[9px] font-bold uppercase tracking-wider"
                        style={{ background: secsLeft < 30 ? "#2a0a0a" : "#1a1a0a", color: secsLeft < 30 ? "#ff3e3e" : "#e8c300" }}>
                        expires in {secsLeft}s
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-4 text-[10px] mb-2" style={{ color: "#5a6270" }}>
                      <span>Entry <b style={{ color: "#c8cdd5" }}>₹{a.entry}</b></span>
                      <span>Stop <b style={{ color: "#ff3e3e" }}>₹{a.stop}</b></span>
                      <span>Target <b style={{ color: "#00e87b" }}>₹{a.target}</b></span>
                      {a.confidence != null && <span>Confidence <b style={{ color: "#c8cdd5" }}>{a.confidence}%</b> ({a.tier})</span>}
                    </div>
                    {/* Performance Evidence Bundle snapshot — never a bare
                        win rate; suppressed below the sample threshold
                        rather than shown as a precise-looking guess. */}
                    <div className="text-[10px] mb-3 p-2" style={{ background: "#0d0f14", color: "#5a6270" }}>
                      <span style={{ color: "#3d4450", textTransform: "uppercase", letterSpacing: "0.05em" }}>Evidence: </span>
                      {eb.sufficient_sample ? (
                        <>
                          win rate {((eb.win_rate ?? 0) * 100).toFixed(0)}%
                          {eb.win_rate_ci95 && ` (95% CI ${(eb.win_rate_ci95[0]*100).toFixed(0)}–${(eb.win_rate_ci95[1]*100).toFixed(0)}%)`}
                          {" · "}profit factor {eb.profit_factor != null ? eb.profit_factor.toFixed(2) : "undefined"}
                          {" · "}n={eb.n} paper trades
                        </>
                      ) : (
                        <span style={{ color: "#ff3e3e" }}>insufficient sample (n={eb.n ?? 0}) — no reliable track record yet</span>
                      )}
                    </div>
                    {/* Reject and Approve carry equal visual weight — this
                        is not a confirm-dialog with a de-emphasized cancel. */}
                    <div className="flex gap-2">
                      <button onClick={() => decideApproval(a.approval_id, "reject")} disabled={busy}
                        className="flex-1 px-3 py-[6px] text-[11px] font-bold uppercase tracking-wider disabled:opacity-50"
                        style={{ background: "#2a0a0a", border: "1px solid #5c1a1a", color: "#ff3e3e" }}>
                        {busy ? "…" : "Reject"}
                      </button>
                      <button onClick={() => decideApproval(a.approval_id, "approve")} disabled={busy}
                        className="flex-1 px-3 py-[6px] text-[11px] font-bold uppercase tracking-wider disabled:opacity-50"
                        style={{ background: "#0a2a18", border: "1px solid #1a5c3a", color: "#00e87b" }}>
                        {busy ? "…" : "Approve"}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

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

                {live.analyzer && (
                  <div className="grid grid-cols-2 gap-2 mb-3">
                    <StrengthBar label="Call candle strength" pct={live.analyzer.strength.call_pct} bullish={live.analyzer.strength.call_bullish} color="#00e87b" />
                    <StrengthBar label="Put candle strength" pct={live.analyzer.strength.put_pct} bullish={live.analyzer.strength.put_bullish} color="#ff3e3e" />
                  </div>
                )}
                {live.side && (
                  <p className="text-[10px] mb-3" style={{ color: "#5a6270" }}>
                    Enter only when price crosses ₹{live.entry} (candle high + buffer). At the partial level ₹{live.partial} book
                    part of the position and move the stop to cost; full target ₹{live.target}; structure stop ₹{live.stop}.
                  </p>
                )}

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

                {/* Options Analyzer - the reference app's analyzer output, auto-fed
                    from the same fetched candles (no manual entry of the 8
                    O/H/L/C values). Display-only. */}
                {live.analyzer && (() => {
                  const a = live.analyzer;
                  const v = a.verdict;
                  const vColor = v.side === "call" ? "#00e87b" : v.side === "put" ? "#ff3e3e" : "#e8c300";
                  return (
                    <div className="mt-5 pt-4" style={{ borderTop: "1px solid #252a33" }}>
                      <h3 className="text-[11px] font-bold uppercase tracking-wider mb-1 flex items-center gap-2" style={{ color: "#4da6ff" }}>
                        Options Analyzer
                        <span className="px-1.5 py-[1px] text-[8px] tracking-wider" style={{ background: "#0d1a2a", border: "1px solid #1a3a5c", color: "#4da6ff" }}>AUTO-FETCHED</span>
                      </h3>
                      <p className="text-[9px] mb-3" style={{ color: "#3d4450" }}>
                        Scores are a candle-shape checklist (direction 20 + body 25 + close position 35 + PCR agreeing with the side 20) — not a win probability. A side needs a {a.scores.required_margin}%+ lead and a {a.scores.required_confidence}%+ score.
                      </p>

                      <div className="p-3 mb-3" style={{ background: "#181c24", border: `1px solid ${vColor}` }}>
                        <div className="text-[9px] uppercase tracking-[0.2em] mb-1" style={{ color: "#5a6270" }}>{v.signal}</div>
                        <div className="flex flex-wrap items-start justify-between gap-3">
                          <div className="min-w-0 flex-1">
                            <div className="text-[18px] font-bold tracking-wider" style={{ color: vColor }}>{v.headline}</div>
                            <div className="text-[10px] mt-2 leading-relaxed" style={{ color: "#8a93a1" }}>{v.reason}</div>
                          </div>
                          <div className="text-right">
                            <div className="text-[26px] font-bold leading-none" style={{ color: vColor }}>{v.confidence}%</div>
                            <div className="text-[8px] uppercase tracking-wider mt-1" style={{ color: "#5a6270" }}>Confidence</div>
                          </div>
                        </div>
                        <div className="flex gap-2 mt-3">
                          {([["Call score", a.scores.call, "#00e87b"], ["Put score", a.scores.put, "#ff3e3e"], ["Gap", a.scores.margin, "#c8cdd5"]] as [string, number, string][]).map(([l, n, c]) => (
                            <div key={l} className="px-3 py-1 text-center" style={{ background: "#0d0f14", border: "1px solid #252a33" }}>
                              <div className="text-[8px] uppercase tracking-wider" style={{ color: "#5a6270" }}>{l}</div>
                              <div className="text-[14px] font-bold" style={{ color: c }}>{n}%</div>
                            </div>
                          ))}
                        </div>
                      </div>

                      <div className="p-3 mb-3 flex items-center justify-between gap-3" style={{ background: "#181c24", border: "1px solid #3a3a1a" }}>
                        <div>
                          <div className="text-[8px] uppercase tracking-wider" style={{ color: "#5a6270" }}>Entry price</div>
                          <div className="text-[22px] font-bold" style={{ color: "#e8c300" }}>
                            {v.entry != null ? `₹${v.entry.toFixed(2)}` : "— SKIP —"}
                          </div>
                          <div className="text-[9px]" style={{ color: "#3d4450" }}>{v.entry != null ? v.entry_note : "Next candle — wait"}</div>
                        </div>
                        <div className="text-[13px] font-bold tracking-wider text-right" style={{ color: vColor }}>
                          {v.side ? (v.side === "call" ? "BUY CALL" : "BUY PUT") : "NO TRADE"}
                        </div>
                      </div>

                      <div className="text-[9px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Entry conditions</div>
                      <div className="mb-3" style={{ background: "#181c24", border: "1px solid #252a33" }}>
                        {a.checklist.map((r) => {
                          const st = STATE_ICON[r.state];
                          return (
                            <div key={r.key} className="flex items-center justify-between gap-3 px-3 py-[6px] text-[11px]" style={{ borderBottom: "1px solid #1d222b" }}>
                              <span className="flex items-center gap-2">
                                <span className="font-bold w-3 text-center" style={{ color: st.color }}>{st.icon}</span>
                                <span style={{ color: "#c8cdd5" }}>{r.label}</span>
                              </span>
                              <span className="text-[10px] font-semibold" style={{ color: st.color }}>{r.value}</span>
                            </div>
                          );
                        })}
                      </div>

                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
                        <LadderTable title="Call targets" ladder={a.call_ladder} color="#00e87b" />
                        <LadderTable title="Put targets" ladder={a.put_ladder} color="#ff3e3e" />
                      </div>

                      <div className="p-3" style={{ background: "#0d1a14", border: "1px solid #1a3a2a" }}>
                        <div className="text-[9px] uppercase tracking-wider mb-2" style={{ color: "#00e87b" }}>⚡ Exact rules — follow</div>
                        <ol className="text-[11px] space-y-1 list-decimal pl-5" style={{ color: "#c8cdd5" }}>
                          {v.rules.map((r, i) => <li key={i}>{r}</li>)}
                        </ol>
                      </div>
                    </div>
                  );
                })()}

                {/* Pullback Entry - wait for a dip into 25/38/50% of the leading
                    candle's range instead of buying the already-pumped close. */}
                {live.pullback && (
                  <div className="mt-5 pt-4" style={{ borderTop: "1px solid #252a33" }}>
                    <h3 className="text-[11px] font-bold uppercase tracking-wider mb-1 flex items-center gap-2" style={{ color: "#00e87b" }}>
                      Pullback Entry
                      <span className="px-1.5 py-[1px] text-[8px] tracking-wider" style={{ background: "#0a2a18", border: "1px solid #1a5c3a", color: "#00e87b" }}>AUTO-FED</span>
                    </h3>
                    <p className="text-[9px] mb-3" style={{ color: "#3d4450" }}>
                      Mistake: entering at the candle close — it has already pumped and a pull-back hits your SL. Right: spot the strong candle, wait for the dip, enter in the dip zone (25–50% of the range above the low); SL sits below the low only.
                    </p>
                    {live.pullback.wait || !live.pullback.zones ? (
                      <div className="p-3 text-[12px] font-bold tracking-wider" style={{ background: "#181c24", border: "1px solid #e8c300", color: "#e8c300" }}>
                        WAIT — NO CLEAR SIDE
                        <div className="text-[10px] font-normal mt-1" style={{ color: "#5a6270" }}>The analyzer has no clear leader on this candle, so there is no dip to plan. Fetch again after the next candle closes.</div>
                      </div>
                    ) : (() => {
                      const pb = live.pullback;
                      const z = pb.zones!;
                      const isCall = pb.side === "call";
                      const c = isCall ? "#00e87b" : "#ff3e3e";
                      const zoneCards: [string, string, number, string][] = [
                        ["Zone 1 — aggressive", "low + 25% · tightest SL", z.zone1, "#e8c300"],
                        ["Zone 2 — best (38%)", "golden zone · best R:R", z.zone2, "#4da6ff"],
                        ["Zone 3 — last chance", "50% midpoint · above = skip", z.zone3, "#c8cdd5"],
                      ];
                      return (
                        <>
                          <div className="p-3 mb-3 flex flex-wrap items-center justify-between gap-2" style={{ background: "#181c24", border: `1px solid ${c}` }}>
                            <div>
                              <div className="text-[8px] uppercase tracking-wider" style={{ color: "#5a6270" }}>Decision</div>
                              <div className="text-[18px] font-bold tracking-wider" style={{ color: c }}>{isCall ? "BUY CALL" : "BUY PUT"}</div>
                              <div className="text-[10px] mt-1" style={{ color: "#8a93a1" }}>
                                {isCall ? "Call" : "Put"} candle leads — enter when the dip comes.
                              </div>
                            </div>
                            {pb.rr != null && (
                              <div className="text-right">
                                <div className="text-[8px] uppercase tracking-wider" style={{ color: "#5a6270" }}>R : R</div>
                                <div className="text-[16px] font-bold" style={{ color: "#e8c300" }}>1 : {pb.rr}</div>
                              </div>
                            )}
                          </div>
                          {(pb.cautions ?? []).map((w) => (
                            <div key={w} className="px-3 py-2 mb-3 text-[11px]" style={{ background: "#1a1a0a", border: "1px solid #3a3a1a", color: "#e8c300" }}>⚠ {w}</div>
                          ))}
                          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 mb-3">
                            {zoneCards.map(([label, note, value, color]) => (
                              <div key={label} className="p-3" style={{ background: "#181c24", border: `1px solid ${color}44` }}>
                                <div className="text-[9px] uppercase tracking-wider" style={{ color }}>{label}</div>
                                <div className="text-[20px] font-bold" style={{ color }}>₹{value}</div>
                                <div className="text-[9px]" style={{ color: "#3d4450" }}>{note}</div>
                              </div>
                            ))}
                          </div>
                          <div className="mb-3" style={{ background: "#181c24", border: "1px solid #252a33" }}>
                            <div className="px-3 py-2 text-[9px] uppercase tracking-wider" style={{ color: "#5a6270", borderBottom: "1px solid #1d222b" }}>
                              Stop loss &amp; targets (measured from Zone 2)
                            </div>
                            <div className="flex items-center justify-between px-3 py-[6px] text-[11px]" style={{ borderBottom: "1px solid #1d222b" }}>
                              <span style={{ color: "#5a6270" }}>STOP LOSS</span>
                              <span><b style={{ color: "#ff3e3e" }}>₹{pb.stop_loss}</b> <span style={{ color: "#3d4450" }}>−{pb.stop_pts}pts</span></span>
                            </div>
                            {(pb.targets ?? []).map((t, i) => (
                              <div key={i} className="flex items-center justify-between px-3 py-[6px] text-[11px]" style={{ borderBottom: "1px solid #1d222b" }}>
                                <span style={{ color: "#5a6270" }}>TARGET {i + 1}</span>
                                <span><b style={{ color: "#00e87b" }}>₹{t.level}</b> <span style={{ color: "#3d4450" }}>+{t.pts}pts</span></span>
                              </div>
                            ))}
                          </div>
                          <div className="p-3" style={{ background: "#0d1a14", border: "1px solid #1a3a2a" }}>
                            <div className="text-[9px] uppercase tracking-wider mb-2" style={{ color: "#00e87b" }}>Step by step</div>
                            <ol className="text-[11px] space-y-1 list-decimal pl-5" style={{ color: "#c8cdd5" }}>
                              {(pb.rules ?? []).map((r, i) => <li key={i}>{r}</li>)}
                            </ol>
                          </div>
                        </>
                      );
                    })()}
                  </div>
                )}

                {/* Value Calculator - auto-fed from the ladders above. */}
                {vcResult && (
                  <div className="mt-5 pt-4" style={{ borderTop: "1px solid #252a33" }}>
                    <h3 className="text-[11px] font-bold uppercase tracking-wider mb-1 flex items-center gap-2" style={{ color: "#e8c300" }}>
                      Value Calculator
                      <span className="px-1.5 py-[1px] text-[8px] tracking-wider" style={{ background: "#1a1a0a", border: "1px solid #3a3a1a", color: "#e8c300" }}>
                        {vcEdited ? "EDITED" : "AUTO-FED"}
                      </span>
                    </h3>
                    <p className="text-[9px] mb-3" style={{ color: "#3d4450" }}>
                      Six values from the Analyzer (call entry / T1 / T2, put entry / T1 / T2) → average → √avg → CALL level = avg − √avg → PUT = CALL − 55%.
                    </p>
                    <div className="grid grid-cols-2 sm:grid-cols-6 gap-2 mb-3">
                      {["Call entry", "Call T1", "Call T2", "Put entry", "Put T1", "Put T2"].map((label, i) => (
                        <div key={label}>
                          <label className="block text-[8px] uppercase tracking-wider mb-1" style={{ color: "#5a6270" }}>Value {i + 1} ({label})</label>
                          <input value={vcInputs[i] ?? ""} inputMode="decimal"
                            onChange={(e) => { const next = [...vcInputs]; next[i] = e.target.value; setVcInputs(next); setVcEdited(true); }}
                            className="w-full px-2 py-[5px] text-[12px]" style={{ background: "#0e1117", border: "1px solid #252a33", color: "#e8c300" }} />
                        </div>
                      ))}
                    </div>
                    <div className="flex flex-wrap gap-2 mb-3">
                      <button onClick={recalcValue} disabled={vcBusy}
                        className="t-btn t-btn-green px-4 py-[6px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50">
                        {vcBusy ? "Calculating…" : "Recalculate"}
                      </button>
                      {vcEdited && live.value_calc && (
                        <button onClick={() => { setVcInputs(live.value_calc.inputs.map((v) => String(v))); setVcResult(live.value_calc); setVcEdited(false); setVcError(null); }}
                          className="px-3 py-[6px] text-[11px] font-semibold uppercase tracking-wider"
                          style={{ background: "#181c24", border: "1px solid #252a33", color: "#c8cdd5" }}>
                          Reset to auto-fed
                        </button>
                      )}
                    </div>
                    {vcError && <p className="text-[11px] mb-2" style={{ color: "#ff3e3e" }}>ERROR: {vcError}</p>}
                    <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                      {([
                        ["Average (÷6)", vcResult.average, "#4da6ff"],
                        ["Square root of avg", vcResult.sqrt_of_avg, "#4da6ff"],
                        ["Call (avg − √avg)", vcResult.call_level, "#00e87b"],
                        ["Call target (+25%)", vcResult.call_target, "#00e87b"],
                        ["Put (call − 55%)", vcResult.put_level, "#ff3e3e"],
                        ["Put target (+70%)", vcResult.put_target, "#ff3e3e"],
                      ] as [string, number, string][]).map(([label, value, color]) => (
                        <div key={label} className="p-2" style={{ background: "#181c24", border: "1px solid #252a33" }}>
                          <div className="text-[8px] uppercase tracking-wider" style={{ color: "#5a6270" }}>{label}</div>
                          <div className="text-[14px] font-semibold" style={{ color }}>₹{value.toFixed(2)}</div>
                        </div>
                      ))}
                    </div>
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
