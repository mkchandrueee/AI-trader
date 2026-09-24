"use client";

import { useEffect, useState, useCallback } from "react";
import Sidebar from "@/components/Sidebar";
import RiskProfileCard from "@/components/RiskProfileCard";
import { fetchJSON, postJSON, API_BASE, type RiskProfile } from "@/lib/api";
import { Play, Plug, PlugZap, RotateCw, AlertTriangle } from "lucide-react";

type RiskLevel = "low" | "medium" | "high";
const riskColors: Record<string, string> = { low: "#4da6ff", medium: "#e8c300", high: "#00e87b" };

interface RestartPreflight {
  market_hours: boolean;
  open_positions: { id: number; symbol: string; direction: string; entry_premium: number;
                    current_premium: number | null; unrealised_pnl: number | null; entry_time: string }[];
  open_position_count: number;
  pending_approvals: number;
  suggestions: number;
  collector_delivering_fresh_prices: boolean;
  effects: string[];
}

interface BrokerAuthStatus {
  connected: boolean;
  broker?: string;
  client_code?: string | null;
  message?: string;
  angelone?: { connected: boolean; client_code?: string | null };
  mstock?: { connected: boolean; user_id?: string | null };
  trade_mode?: string;
}

export default function SettingsPage() {
  const [profiles, setProfiles] = useState<Record<RiskLevel, RiskProfile> | null>(null);
  const [activeRisk, setActiveRisk] = useState<RiskLevel>("medium");
  const [runMsg, setRunMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  const [preflight, setPreflight] = useState<RestartPreflight | null>(null);
  const [restartState, setRestartState] = useState<"idle" | "checking" | "confirm" | "restarting" | "back">("idle");
  const [restartMsg, setRestartMsg] = useState<string | null>(null);

  const checkRestart = useCallback(async () => {
    setRestartState("checking"); setRestartMsg(null);
    try {
      const p: RestartPreflight = await fetch(`${API_BASE}/api/system/restart/preflight`).then(r => r.json());
      setPreflight(p);
      setRestartState("confirm");
    } catch (e) {
      setRestartMsg(e instanceof Error ? e.message : String(e));
      setRestartState("idle");
    }
  }, []);

  const doRestart = useCallback(async () => {
    setRestartState("restarting");
    try {
      const r = await fetch(`${API_BASE}/api/system/restart`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      }).then(r => r.json());
      setRestartMsg(r.message ?? r.error ?? "Restarting...");
    } catch {
      // The process replaces its own image, so this request usually never gets an
      // answer. That is the expected path, not a failure -- fall through to polling.
      setRestartMsg("Restarting...");
    }
    // Poll until the NEW process answers, so the button reports what actually
    // happened rather than assuming the restart worked.
    for (let i = 0; i < 40; i++) {
      await new Promise(res => setTimeout(res, 1000));
      try {
        const ok = await fetch(`${API_BASE}/api/state`, { cache: "no-store" });
        if (ok.ok) { setRestartState("back"); setRestartMsg("Backend is back up."); return; }
      } catch { /* still down, keep waiting */ }
    }
    setRestartState("idle");
    setRestartMsg("Backend did not answer within 40s - check the terminal it was started from.");
  }, []);

  const [brokerStatus, setBrokerStatus] = useState<BrokerAuthStatus | null>(null);
  const [clientCode, setClientCode] = useState("");
  const [pin, setPin] = useState("");
  const [totp, setTotp] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  const [mstockUserId, setMstockUserId] = useState("");
  const [mstockPassword, setMstockPassword] = useState("");
  const [mstockTotp, setMstockTotp] = useState("");
  const [mstockConnecting, setMstockConnecting] = useState(false);
  const [mstockConnectError, setMstockConnectError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const p = await fetchJSON<Record<RiskLevel, RiskProfile>>("/api/risk/profiles").catch(() => null);
    if (p) setProfiles(p as Record<RiskLevel, RiskProfile>);
  }, []);

  const loadBrokerStatus = useCallback(async () => {
    const s = await fetchJSON<BrokerAuthStatus>("/api/broker/auth/status").catch(() => null);
    if (s) setBrokerStatus(s);
  }, []);

  useEffect(() => { load(); loadBrokerStatus(); }, [load, loadBrokerStatus]);

  const connectAngelOne = async () => {
    setConnecting(true);
    setConnectError(null);
    try {
      // Raw fetch, not the postJSON helper: a 400 (e.g. bad credentials) is
      // a normal response with a real error message in the body, not a
      // connectivity failure — postJSON throws on any non-2xx, which would
      // otherwise collapse both cases into the same generic message.
      const r = await fetch(`${API_BASE}/api/broker/angelone/connect`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_code: clientCode, pin, totp }),
      });
      const res: { connected: boolean; client_code: string | null; error: string | null } = await r.json();
      if (res.connected) {
        setBrokerStatus((prev) => ({
          ...(prev || { connected: false }),
          connected: true, broker: "AngelOne", client_code: res.client_code,
          angelone: { connected: true, client_code: res.client_code },
        }));
        // Clear credential fields from memory as soon as they're no longer needed.
        setPin("");
        setTotp("");
      } else {
        setConnectError(res.error || `Connect failed (HTTP ${r.status})`);
      }
    } catch {
      setConnectError("Could not reach the backend — is backend/app.py running?");
    } finally {
      setConnecting(false);
    }
  };

  const disconnectAngelOne = async () => {
    await postJSON("/api/broker/angelone/disconnect").catch(() => null);
    setBrokerStatus((prev) => ({ ...(prev || { connected: false }), angelone: { connected: false } }));
  };

  const connectMstock = async () => {
    setMstockConnecting(true);
    setMstockConnectError(null);
    try {
      // Raw fetch, not postJSON — a 400 (bad credentials) is a normal
      // response with a real error message in the body, not a
      // connectivity failure.
      const r = await fetch(`${API_BASE}/api/broker/mstock/connect`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: mstockUserId, password: mstockPassword, totp: mstockTotp }),
      });
      const res: { connected: boolean; user_id: string | null; error: string | null } = await r.json();
      if (res.connected) {
        setBrokerStatus((prev) => ({
          ...(prev || { connected: false }),
          mstock: { connected: true, user_id: res.user_id },
        }));
        // Clear credential fields from memory as soon as they're no longer needed.
        setMstockPassword("");
        setMstockTotp("");
      } else {
        setMstockConnectError(res.error || `Connect failed (HTTP ${r.status})`);
      }
    } catch {
      setMstockConnectError("Could not reach the backend — is backend/app.py running?");
    } finally {
      setMstockConnecting(false);
    }
  };

  const disconnectMstock = async () => {
    await postJSON("/api/broker/mstock/disconnect").catch(() => null);
    setBrokerStatus((prev) => ({ ...(prev || { connected: false }), mstock: { connected: false } }));
  };

  const runBacktest = async () => {
    setRunMsg(null);
    try {
      await postJSON("/api/backtest/run", { risk: activeRisk });
      setRunMsg({ type: "ok", text: `${activeRisk.toUpperCase()} BACKTEST STARTED` });
    } catch {
      setRunMsg({ type: "err", text: "FAILED TO START" });
    }
  };

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-5 overflow-y-auto">
        <div className="mb-5">
          <h1 className="text-sm font-bold uppercase tracking-wider" style={{ color: '#00e87b' }}>Settings</h1>
          <p className="text-[10px] mt-0.5" style={{ color: '#3d4450' }}>RISK PROFILES, EXECUTION, SYSTEM CONFIG</p>
        </div>

        {/* AngelOne Connect */}
        <div className="t-panel p-5 mb-4">
          <div className="flex items-center justify-between mb-1">
            <h2 className="text-[12px] font-bold uppercase tracking-wider" style={{ color: '#c8cdd5' }}>AngelOne Connect</h2>
            {brokerStatus?.angelone?.connected ? (
              <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: '#00e87b' }}>
                <PlugZap className="w-3.5 h-3.5" /> Connected{brokerStatus.angelone.client_code ? ` — ${brokerStatus.angelone.client_code}` : ""}
              </span>
            ) : (
              <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: '#5a6270' }}>
                <Plug className="w-3.5 h-3.5" /> Not Connected
              </span>
            )}
          </div>
          <p className="text-[10px] mb-4" style={{ color: '#5a6270' }}>
            Powers all live market data (ticks, historical candles, instrument resolution) regardless of trade mode.
            Enter your client ID, PIN/password, and the current 6-digit code from your authenticator app.
            This goes straight to your own backend on localhost — never anywhere else. Nothing here is saved to disk;
            re-enter the TOTP code each time it expires.
          </p>

          {brokerStatus?.angelone?.connected ? (
            <button
              onClick={disconnectAngelOne}
              className="t-btn px-4 py-2 text-[10px] font-bold uppercase tracking-wider"
              style={{ borderColor: '#ff3e3e', color: '#ff3e3e' }}
            >
              Disconnect
            </button>
          ) : (
            <>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-3">
                <div>
                  <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: '#5a6270' }}>Client ID</label>
                  <input
                    value={clientCode}
                    onChange={(e) => setClientCode(e.target.value)}
                    autoComplete="off"
                    className="w-full px-3 py-[6px] text-[12px]"
                    style={{ background: '#0e1117', border: '1px solid #252a33', color: '#c8cdd5' }}
                    placeholder="A123456"
                  />
                </div>
                <div>
                  <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: '#5a6270' }}>PIN / Password</label>
                  <input
                    type="password"
                    value={pin}
                    onChange={(e) => setPin(e.target.value)}
                    autoComplete="off"
                    className="w-full px-3 py-[6px] text-[12px]"
                    style={{ background: '#0e1117', border: '1px solid #252a33', color: '#c8cdd5' }}
                    placeholder="••••"
                  />
                </div>
                <div>
                  <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: '#5a6270' }}>TOTP Code</label>
                  <input
                    type="password"
                    inputMode="numeric"
                    maxLength={6}
                    value={totp}
                    onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
                    onKeyDown={(e) => e.key === "Enter" && connectAngelOne()}
                    autoComplete="off"
                    className="w-full px-3 py-[6px] text-[12px] tracking-[3px]"
                    style={{ background: '#0e1117', border: '1px solid #252a33', color: '#c8cdd5' }}
                    placeholder="000000"
                  />
                </div>
              </div>
              <div className="flex items-center gap-3">
                <button
                  onClick={connectAngelOne}
                  disabled={connecting || !clientCode || !pin || totp.length !== 6}
                  className="flex items-center gap-2 px-4 py-2 text-[10px] font-bold uppercase tracking-wider transition-all disabled:opacity-40"
                  style={{ background: '#00e87b', color: '#000' }}
                >
                  <Plug className="w-3 h-3" />
                  {connecting ? "Connecting…" : "Connect"}
                </button>
                {connectError && (
                  <span className="text-[11px]" style={{ color: '#ff3e3e' }}>{connectError}</span>
                )}
              </div>
            </>
          )}
        </div>

        {/* mStock Connect */}
        <div className="t-panel p-5 mb-4">
          <div className="flex items-center justify-between mb-1">
            <h2 className="text-[12px] font-bold uppercase tracking-wider" style={{ color: '#c8cdd5' }}>
              mStock Connect <span style={{ color: '#3d4450', fontWeight: 400 }}>— live trading broker</span>
            </h2>
            {brokerStatus?.mstock?.connected ? (
              <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: '#00e87b' }}>
                <PlugZap className="w-3.5 h-3.5" /> Connected{brokerStatus.mstock.user_id ? ` — ${brokerStatus.mstock.user_id}` : ""}
              </span>
            ) : (
              <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: '#5a6270' }}>
                <Plug className="w-3.5 h-3.5" /> Not Connected
              </span>
            )}
          </div>
          <p className="text-[10px] mb-4" style={{ color: '#5a6270' }}>
            The broker used for live order execution going forward (set <code>TRADE_MODE=mstock</code> in .env when
            ready to leave paper trading). Enter your user ID, password, and the current 6-digit code from your
            authenticator app. This goes straight to your own backend on localhost — never anywhere else. Nothing
            here is saved to disk; re-enter the TOTP code each time it expires.
          </p>

          {brokerStatus?.mstock?.connected ? (
            <button
              onClick={disconnectMstock}
              className="t-btn px-4 py-2 text-[10px] font-bold uppercase tracking-wider"
              style={{ borderColor: '#ff3e3e', color: '#ff3e3e' }}
            >
              Disconnect
            </button>
          ) : (
            <>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-3">
                <div>
                  <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: '#5a6270' }}>User ID</label>
                  <input
                    value={mstockUserId}
                    onChange={(e) => setMstockUserId(e.target.value)}
                    autoComplete="off"
                    className="w-full px-3 py-[6px] text-[12px]"
                    style={{ background: '#0e1117', border: '1px solid #252a33', color: '#c8cdd5' }}
                    placeholder="MA123456"
                  />
                </div>
                <div>
                  <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: '#5a6270' }}>Password</label>
                  <input
                    type="password"
                    value={mstockPassword}
                    onChange={(e) => setMstockPassword(e.target.value)}
                    autoComplete="off"
                    className="w-full px-3 py-[6px] text-[12px]"
                    style={{ background: '#0e1117', border: '1px solid #252a33', color: '#c8cdd5' }}
                    placeholder="••••"
                  />
                </div>
                <div>
                  <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: '#5a6270' }}>TOTP Code</label>
                  <input
                    type="password"
                    inputMode="numeric"
                    maxLength={6}
                    value={mstockTotp}
                    onChange={(e) => setMstockTotp(e.target.value.replace(/\D/g, ""))}
                    onKeyDown={(e) => e.key === "Enter" && connectMstock()}
                    autoComplete="off"
                    className="w-full px-3 py-[6px] text-[12px] tracking-[3px]"
                    style={{ background: '#0e1117', border: '1px solid #252a33', color: '#c8cdd5' }}
                    placeholder="000000"
                  />
                </div>
              </div>
              <div className="flex items-center gap-3">
                <button
                  onClick={connectMstock}
                  disabled={mstockConnecting || !mstockUserId || !mstockPassword || mstockTotp.length !== 6}
                  className="flex items-center gap-2 px-4 py-2 text-[10px] font-bold uppercase tracking-wider transition-all disabled:opacity-40"
                  style={{ background: '#00e87b', color: '#000' }}
                >
                  <Plug className="w-3 h-3" />
                  {mstockConnecting ? "Connecting…" : "Connect"}
                </button>
                {mstockConnectError && (
                  <span className="text-[11px]" style={{ color: '#ff3e3e' }}>{mstockConnectError}</span>
                )}
              </div>
            </>
          )}
        </div>

        {/* Risk profile selection */}
        <div className="t-panel p-5 mb-4">
          <h2 className="text-[12px] font-bold uppercase tracking-wider mb-1" style={{ color: '#c8cdd5' }}>Risk Profile</h2>
          <p className="text-[10px] mb-4" style={{ color: '#5a6270' }}>
            Controls lot size, stop-loss, targets, max trades, and premium caps.
          </p>

          {profiles ? (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-[1px] mb-4">
              {(["low", "medium", "high"] as RiskLevel[]).map(r => (
                <RiskProfileCard
                  key={r}
                  level={r}
                  profile={profiles[r]}
                  active={activeRisk === r}
                  onSelect={setActiveRisk}
                />
              ))}
            </div>
          ) : (
            <div className="text-[11px]" style={{ color: '#3d4450' }}>LOADING...</div>
          )}

          <div className="flex items-center gap-3 mt-3">
            <button
              onClick={runBacktest}
              className="flex items-center gap-2 px-4 py-2 text-[10px] font-bold uppercase tracking-wider transition-all"
              style={{ background: riskColors[activeRisk], color: '#000' }}
            >
              <Play className="w-3 h-3" />
              RUN {activeRisk.toUpperCase()} BACKTEST
            </button>

            {runMsg && (
              <span className="text-[11px]" style={{ color: runMsg.type === "ok" ? '#00e87b' : '#ff3e3e' }}>
                {runMsg.text}
              </span>
            )}
          </div>
        </div>

        {/* Restart */}
        <div className="t-panel p-5 mb-4">
          <h2 className="text-[12px] font-bold uppercase tracking-wider mb-1" style={{ color: '#c8cdd5' }}>Restart Backend</h2>
          <p className="text-[10px] mb-4" style={{ color: '#5a6270' }}>
            Replaces the Flask process in place so it picks up code changes. Open positions are saved to disk
            and restored on startup; the tick collector is a separate process and keeps running.
          </p>

          {(restartState === "idle" || restartState === "checking") && (
            <button onClick={checkRestart} disabled={restartState === "checking"}
              className="t-btn flex items-center gap-2 px-4 py-[7px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50"
              style={{ border: '1px solid #e8a200', color: '#e8a200' }}>
              <RotateCw className={`w-3.5 h-3.5 ${restartState === "checking" ? "animate-spin" : ""}`} />
              {restartState === "checking" ? "Checking..." : "Restart backend"}
            </button>
          )}

          {restartState === "confirm" && preflight && (
            <div>
              <div className="p-3 mb-3" style={{ background: '#0e1117', border: `1px solid ${preflight.market_hours ? '#e8a200' : '#252a33'}` }}>
                {preflight.market_hours && (
                  <p className="text-[11px] font-semibold flex items-center gap-1.5 mb-2" style={{ color: '#e8a200' }}>
                    <AlertTriangle className="w-3.5 h-3.5" /> Market is OPEN
                  </p>
                )}
                <ul className="space-y-1">
                  {preflight.effects.map((e, i) => (
                    <li key={i} className="text-[10px] flex gap-2" style={{ color: '#5a6270' }}>
                      <span style={{ color: '#3d4450' }}>&bull;</span>{e}
                    </li>
                  ))}
                </ul>
                {preflight.open_positions.length > 0 && (
                  <div className="mt-3 pt-3" style={{ borderTop: '1px solid #252a33' }}>
                    <p className="text-[9px] uppercase tracking-wider mb-1" style={{ color: '#5a6270' }}>
                      Open positions to be saved
                    </p>
                    {preflight.open_positions.map(p => (
                      <div key={p.id} className="flex justify-between text-[10px]" style={{ color: '#c8cdd5' }}>
                        <span>{p.symbol} {p.direction} @ {p.entry_premium} ({p.entry_time})</span>
                        <span style={{ color: (p.unrealised_pnl ?? 0) >= 0 ? '#00e87b' : '#ff3e3e' }}>
                          {(p.unrealised_pnl ?? 0) >= 0 ? '+' : ''}{Math.round(p.unrealised_pnl ?? 0)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex gap-2">
                <button onClick={doRestart}
                  className="t-btn px-4 py-[7px] text-[11px] font-semibold uppercase tracking-wider"
                  style={{ border: '1px solid #ff3e3e', color: '#ff3e3e' }}>
                  Confirm restart
                </button>
                <button onClick={() => { setRestartState("idle"); setPreflight(null); }}
                  className="t-btn px-4 py-[7px] text-[11px] font-semibold uppercase tracking-wider"
                  style={{ border: '1px solid #252a33', color: '#5a6270' }}>
                  Cancel
                </button>
              </div>
            </div>
          )}

          {restartState === "restarting" && (
            <p className="text-[11px] flex items-center gap-2" style={{ color: '#e8a200' }}>
              <RotateCw className="w-3.5 h-3.5 animate-spin" /> Restarting - waiting for the backend to answer...
            </p>
          )}

          {restartState === "back" && (
            <div>
              <p className="text-[11px] mb-2" style={{ color: '#00e87b' }}>{restartMsg}</p>
              <button onClick={() => { setRestartState("idle"); setPreflight(null); setRestartMsg(null); }}
                className="t-btn px-4 py-[7px] text-[11px] font-semibold uppercase tracking-wider"
                style={{ border: '1px solid #252a33', color: '#5a6270' }}>
                Done
              </button>
            </div>
          )}

          {restartMsg && restartState === "idle" && (
            <p className="text-[10px] mt-2" style={{ color: '#ff3e3e' }}>{restartMsg}</p>
          )}
        </div>

        {/* System info */}
        <div className="t-panel p-5 mb-4">
          <h2 className="text-[12px] font-bold uppercase tracking-wider mb-4" style={{ color: '#c8cdd5' }}>System Info</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-[11px]">
            {[
              { label: "Flask API",        value: "http://localhost:5050" },
              { label: "Next.js Frontend", value: "http://localhost:3000" },
              { label: "Database",         value: "PostgreSQL (local)" },
              { label: "Index Symbol",     value: "NIFTY (AngelOne)" },
              { label: "Data Range",       value: "Sep 2025 – Mar 2026" },
              { label: "Option Format",    value: "NIFTY+YYMMDD+STRIKE+CE/PE" },
            ].map(({ label, value }) => (
              <div key={label} className="flex items-start gap-3">
                <span className="w-[5px] h-[5px] mt-1 flex-shrink-0" style={{ background: '#00e87b' }} />
                <div>
                  <p style={{ color: '#5a6270' }}>{label}</p>
                  <p className="font-semibold" style={{ color: '#c8cdd5' }}>{value}</p>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* CLI reference */}
        <div className="t-panel p-5">
          <h2 className="text-[12px] font-bold uppercase tracking-wider mb-4" style={{ color: '#c8cdd5' }}>CLI Reference</h2>
          <div className="space-y-2">
            {[
              { cmd: "python scripts/tick_replay_backtest.py --risk high",      desc: "Full backtest HIGH" },
              { cmd: "python scripts/forward_test.py --risk medium",             desc: "OOS forward test" },
              { cmd: "python scripts/train_rl_exit.py --epochs 15",              desc: "Train tabular RL" },
              { cmd: "python scripts/train_dqn_exit.py --epochs 10",             desc: "Train DQN agent" },
              { cmd: "python scripts/paper_trade.py --replay 2026-03-20",        desc: "Replay paper trade" },
              { cmd: "python scripts/paper_trade.py",                            desc: "Live paper trading" },
              { cmd: "python backend/app.py",                                     desc: "Flask API (5050)" },
              { cmd: "npm run dev",                                               desc: "Next.js dev (3000)" },
            ].map(({ cmd, desc }) => (
              <div key={cmd} className="flex items-start gap-3">
                <code className="text-[10px] px-2 py-1 flex-1" style={{ background: '#111318', border: '1px solid #1e222c', color: '#4da6ff' }}>
                  {cmd}
                </code>
                <span className="text-[10px] w-36 flex-shrink-0 pt-1" style={{ color: '#5a6270' }}>{desc}</span>
              </div>
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}
