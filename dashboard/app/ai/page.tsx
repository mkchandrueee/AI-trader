"use client";

import { useEffect, useState, useCallback } from "react";
import Sidebar from "@/components/Sidebar";
import { fetchJSON, type RLStatus } from "@/lib/api";
import { RefreshCw } from "lucide-react";

interface ModelInfo {
  path: string;
  exists: boolean;
  algorithm?: string | null;
  n_features?: number | null;
  n_samples?: number;
  trained_at?: string;
  modified?: string;
  metrics?: Record<string, number | null>;
  error?: string;
}

interface ModelsStatus {
  macro?: ModelInfo;
  micro?: ModelInfo;
  strategy?: Record<string, ModelInfo>;
  rl_exit_agent?: ModelInfo;
  dqn_exit_agent?: ModelInfo;
}

const fmtMetric = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : v.toFixed(4);

/** AUC below ~0.55 is barely better than a coin flip; say so rather than
 *  printing a number that reads like a score. */
const aucColor = (v: number | null | undefined) =>
  v === null || v === undefined ? "#5a6270" : v >= 0.7 ? "#00e87b" : v >= 0.6 ? "#e8c300" : "#ff3e3e";

export default function AIPage() {
  const [rl, setRl] = useState<RLStatus>({});
  const [models, setModels] = useState<ModelsStatus>({});
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [rlData, modelData] = await Promise.all([
        fetchJSON<RLStatus>("/api/rl/status").catch(() => ({})),
        fetchJSON<ModelsStatus>("/api/models/status").catch(() => ({})),
      ]);
      setRl(rlData);
      setModels(modelData);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-5 overflow-y-auto">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h1 className="text-sm font-bold uppercase tracking-wider" style={{ color: '#00e87b' }}>AI Models</h1>
            <p className="text-[10px] mt-0.5" style={{ color: '#3d4450' }}>ML MODELS, RL AGENTS & TRAINING STATUS</p>
          </div>
          <button onClick={load} className="t-btn flex items-center gap-1.5">
            <RefreshCw className="w-3 h-3" /> REFRESH
          </button>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-[1px] mb-4">
          {/* Tabular RL agent */}
          <div className="t-panel p-5">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="text-[12px] font-bold uppercase tracking-wider" style={{ color: '#4da6ff' }}>Tabular Q-Learning</h3>
                <p className="text-[10px] mt-0.5" style={{ color: '#3d4450' }}>models/saved/rl_exit_agent.pkl</p>
              </div>
              <span className="w-[6px] h-[6px]" style={{ background: rl.tabular ? '#00e87b' : '#ff3e3e' }} />
            </div>
            {rl.tabular ? (
              <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-[11px]">
                {[
                  ["States",    rl.tabular.states?.toLocaleString() ?? "--"],
                  ["Episodes",  rl.tabular.episodes?.toLocaleString() ?? "--"],
                  ["Actions",   "HOLD / EXIT / TIGHTEN"],
                  ["Type",      "Tabular Q-Table"],
                ].map(([k, v]) => (
                  <div key={String(k)}>
                    <span style={{ color: '#5a6270' }}>{k}</span>
                    <p className="font-semibold" style={{ color: '#c8cdd5' }}>{v}</p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-[11px]" style={{ color: '#3d4450' }}>
                NOT LOADED — <code style={{ color: '#4da6ff' }}>python scripts/train_rl_exit.py --epochs 10</code>
              </p>
            )}
            <div className="mt-4 pt-4" style={{ borderTop: '1px solid #252a33' }}>
              <p className="text-[10px]" style={{ color: '#5a6270' }}>
                Used by the tick-replay <span style={{ color: '#c8cdd5' }}>backtest only</span> — the live exit path
                (trailing SL, breakeven lock, regime tiers) does not consult it, so an untrained agent does not
                affect live trading. The backtest simply skips RL_EXIT when it is missing.
              </p>
            </div>
          </div>

          {/* DQN agent */}
          <div className="t-panel p-5">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="text-[12px] font-bold uppercase tracking-wider" style={{ color: '#b388ff' }}>DQN Agent</h3>
                <p className="text-[10px] mt-0.5" style={{ color: '#3d4450' }}>models/saved/dqn_exit_agent.pt</p>
              </div>
              <span className="w-[6px] h-[6px]" style={{ background: rl.dqn ? '#00e87b' : '#ff3e3e' }} />
            </div>
            {rl.dqn ? (
              <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-[11px]">
                {[
                  ["Episodes",        rl.dqn.episodes?.toLocaleString() ?? "--"],
                  ["Training Steps",  rl.dqn.training_steps?.toLocaleString() ?? "--"],
                  ["Epsilon",         rl.dqn.epsilon?.toFixed(4) ?? "--"],
                  ["Parameters",      rl.dqn.params?.toLocaleString() ?? "--"],
                  ["Architecture",    "64→64→32 LayerNorm"],
                  ["Algorithm",       "Double DQN + Huber"],
                ].map(([k, v]) => (
                  <div key={String(k)}>
                    <span style={{ color: '#5a6270' }}>{k}</span>
                    <p className="font-semibold" style={{ color: '#c8cdd5' }}>{v}</p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-[11px]" style={{ color: '#3d4450' }}>
                NOT LOADED — <code style={{ color: '#4da6ff' }}>python scripts/train_dqn_exit.py --epochs 10</code>
              </p>
            )}
            <div className="mt-4 pt-4" style={{ borderTop: '1px solid #252a33' }}>
              <p className="text-[10px]" style={{ color: '#5a6270' }}>ADVANTAGES OVER TABULAR:</p>
              <div className="text-[10px] mt-1 space-y-0.5">
                <p><span style={{ color: '#c8cdd5' }}>Continuous states</span> <span style={{ color: '#3d4450' }}>— no discretization</span></p>
                <p><span style={{ color: '#c8cdd5' }}>Double DQN</span> <span style={{ color: '#3d4450' }}>— reduces overestimation</span></p>
                <p><span style={{ color: '#c8cdd5' }}>Replay buffer</span> <span style={{ color: '#3d4450' }}>— 50K stable training</span></p>
                <p><span style={{ color: '#c8cdd5' }}>Generalizes</span> <span style={{ color: '#3d4450' }}>— unseen markets</span></p>
              </div>
            </div>
          </div>
        </div>

        {/* Trained models — read from the files on disk, never hardcoded */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-[1px] mb-4">
          {/* Macro */}
          <div className="t-panel p-4">
            <h3 className="text-[12px] font-bold uppercase tracking-wider mb-1" style={{ color: '#4da6ff' }}>Macro ML Model</h3>
            <p className="text-[10px] mb-2" style={{ color: '#3d4450' }}>{models.macro?.path ?? "models/saved/macro_model.pkl"}</p>
            {models.macro?.exists ? (
              <>
                <p className="text-[11px] mb-2" style={{ color: '#c8cdd5' }}>
                  {models.macro.algorithm} — {models.macro.n_features} features
                </p>
                <p className="text-[10px]" style={{ color: '#5a6270' }}>
                  › AUC <span style={{ color: aucColor(models.macro.metrics?.auc_roc), fontWeight: 600 }}>
                    {fmtMetric(models.macro.metrics?.auc_roc)}
                  </span> (walk-forward)
                </p>
                <p className="text-[10px]" style={{ color: '#5a6270' }}>› Accuracy {fmtMetric(models.macro.metrics?.accuracy)} · F1 {fmtMetric(models.macro.metrics?.f1)}</p>
                <p className="text-[10px]" style={{ color: '#5a6270' }}>› Trained {models.macro.modified?.replace("T", " ")}</p>
              </>
            ) : (
              <p className="text-[11px]" style={{ color: '#ff3e3e' }}>
                NOT TRAINED — <code style={{ color: '#4da6ff' }}>python scripts/retrain_full.py</code>
              </p>
            )}
          </div>

          {/* Strategy models */}
          <div className="t-panel p-4">
            <h3 className="text-[12px] font-bold uppercase tracking-wider mb-1" style={{ color: '#e8c300' }}>Strategy Models</h3>
            <p className="text-[10px] mb-2" style={{ color: '#3d4450' }}>models/saved/strategy/*_model.pkl</p>
            {Object.keys(models.strategy ?? {}).length > 0 ? (
              <div className="space-y-0.5">
                {Object.entries(models.strategy ?? {}).map(([name, m]) => (
                  <p key={name} className="text-[10px]" style={{ color: '#5a6270' }}>
                    › {name} — AUC{" "}
                    <span style={{ color: aucColor(m.metrics?.auc_roc), fontWeight: 600 }}>
                      {fmtMetric(m.metrics?.auc_roc)}
                    </span>
                    {m.n_samples ? ` · n=${m.n_samples.toLocaleString()}` : ""}
                  </p>
                ))}
              </div>
            ) : (
              <p className="text-[11px]" style={{ color: '#ff3e3e' }}>
                NOT TRAINED — <code style={{ color: '#4da6ff' }}>python scripts/retrain_full.py</code>
              </p>
            )}
          </div>

          {/* Micro */}
          <div className="t-panel p-4">
            <h3 className="text-[12px] font-bold uppercase tracking-wider mb-1" style={{ color: '#00e87b' }}>Micro ML Model</h3>
            <p className="text-[10px] mb-2" style={{ color: '#3d4450' }}>{models.micro?.path ?? "models/saved/micro_model.pkl"}</p>
            {models.micro?.exists ? (
              <>
                <p className="text-[11px] mb-2" style={{ color: '#c8cdd5' }}>
                  {models.micro.algorithm} — {models.micro.n_features} features
                </p>
                <p className="text-[10px]" style={{ color: '#5a6270' }}>
                  › AUC <span style={{ color: aucColor(models.micro.metrics?.auc_roc), fontWeight: 600 }}>
                    {fmtMetric(models.micro.metrics?.auc_roc)}
                  </span>
                </p>
              </>
            ) : (
              <p className="text-[11px]" style={{ color: '#5a6270' }}>
                NOT TRAINED — needs tick history. Not blended into live scoring anyway
                (disabled pending AUC &gt; 0.55), so this being absent changes nothing today.
              </p>
            )}
          </div>
        </div>

        <p className="text-[9px] mb-4" style={{ color: '#3d4450' }}>
          Every figure above is read from the model file on disk at load time. AUC is walk-forward, on the data this
          install actually trained on — it is not a win rate and not a probability of profit. Anything near 0.50 is
          close to a coin flip regardless of how the trades then perform.
        </p>

        {/* Kelly position sizer */}
        <div className="t-panel p-5">
          <h3 className="text-[12px] font-bold uppercase tracking-wider mb-3" style={{ color: '#00e87b' }}>Kelly Criterion Sizer</h3>
          <p className="text-[11px] mb-3" style={{ color: '#5a6270' }}>
            Capital-aware sizing. Half-Kelly for safety, capped by <code style={{ color: '#4da6ff' }}>max_capital_per_trade</code>.
          </p>
          <div className="p-4 text-[11px]" style={{ background: '#111318', border: '1px solid #1e222c' }}>
            <span style={{ color: '#4da6ff' }}>f*</span> <span style={{ color: '#5a6270' }}>=</span> (p × b − q) / b × <span style={{ color: '#00e87b' }}>0.5</span> × regime_mult<br/>
            <span className="text-[10px]" style={{ color: '#3d4450' }}>p=win_rate, q=1−p, b=avg_win/avg_loss</span><br/>
            <span className="text-[10px]" style={{ color: '#3d4450' }}>Clamped: min 1 lot (65), max 5 lots (325)</span>
          </div>
          <div className="grid grid-cols-3 gap-4 mt-4 text-[11px]">
            {[
              { label: "Initial Capital", value: "₹50,000" },
              { label: "Lot Size", value: "65 units" },
              { label: "Rolling Window", value: "Last 20 trades" },
            ].map(({ label, value }) => (
              <div key={label}>
                <span style={{ color: '#5a6270' }}>{label}</span>
                <p className="font-semibold" style={{ color: '#c8cdd5' }}>{value}</p>
              </div>
            ))}
          </div>
        </div>
      </main>
    </div>
  );
}
