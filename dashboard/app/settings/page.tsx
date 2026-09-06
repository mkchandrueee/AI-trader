"use client";

import { useEffect, useState, useCallback } from "react";
import Sidebar from "@/components/Sidebar";
import RiskProfileCard from "@/components/RiskProfileCard";
import { fetchJSON, postJSON, type RiskProfile } from "@/lib/api";
import { Play } from "lucide-react";

type RiskLevel = "low" | "medium" | "high";
const riskColors: Record<string, string> = { low: "#4da6ff", medium: "#e8c300", high: "#00e87b" };

export default function SettingsPage() {
  const [profiles, setProfiles] = useState<Record<RiskLevel, RiskProfile> | null>(null);
  const [activeRisk, setActiveRisk] = useState<RiskLevel>("medium");
  const [runMsg, setRunMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);

  const load = useCallback(async () => {
    const p = await fetchJSON<Record<RiskLevel, RiskProfile>>("/api/risk/profiles").catch(() => null);
    if (p) setProfiles(p as Record<RiskLevel, RiskProfile>);
  }, []);

  useEffect(() => { load(); }, [load]);

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
              { cmd: "python frontend/app.py",                                   desc: "Flask API (5050)" },
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
