"use client";

import { useEffect, useState, useCallback } from "react";
import Sidebar from "@/components/Sidebar";
import RiskProfileCard from "@/components/RiskProfileCard";
import { fetchJSON, postJSON, API_BASE, type RiskProfile } from "@/lib/api";
import { Play, Plug, PlugZap } from "lucide-react";

type RiskLevel = "low" | "medium" | "high";
const riskColors: Record<string, string> = { low: "#4da6ff", medium: "#e8c300", high: "#00e87b" };

interface BrokerAuthStatus {
  connected: boolean;
  broker?: string;
  client_code?: string | null;
  message?: string;
}

export default function SettingsPage() {
  const [profiles, setProfiles] = useState<Record<RiskLevel, RiskProfile> | null>(null);
  const [activeRisk, setActiveRisk] = useState<RiskLevel>("medium");
  const [runMsg, setRunMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  const [brokerStatus, setBrokerStatus] = useState<BrokerAuthStatus | null>(null);
  const [clientCode, setClientCode] = useState("");
  const [pin, setPin] = useState("");
  const [totp, setTotp] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

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
        setBrokerStatus({ connected: true, broker: "AngelOne", client_code: res.client_code });
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
    setBrokerStatus({ connected: false });
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
            {brokerStatus?.connected ? (
              <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: '#00e87b' }}>
                <PlugZap className="w-3.5 h-3.5" /> Connected{brokerStatus.client_code ? ` — ${brokerStatus.client_code}` : ""}
              </span>
            ) : (
              <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: '#5a6270' }}>
                <Plug className="w-3.5 h-3.5" /> Not Connected
              </span>
            )}
          </div>
          <p className="text-[10px] mb-4" style={{ color: '#5a6270' }}>
            Enter your client ID, PIN/password, and the current 6-digit code from your authenticator app.
            This goes straight to your own backend on localhost — never anywhere else. Nothing here is saved to disk;
            re-enter the TOTP code each time it expires.
          </p>

          {brokerStatus?.connected ? (
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
