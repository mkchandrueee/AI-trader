"use client";

import { useEffect, useState, useCallback } from "react";
import Sidebar from "@/components/Sidebar";
import { fetchJSON, postJSON, type RLStatus } from "@/lib/api";
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

interface CoverageBlock {
  days?: number; bars?: number; rows?: number; symbols?: number;
  first_day?: string; last_day?: string; error?: string;
}

interface Coverage {
  candles?: CoverageBlock;
  ticks?: CoverageBlock;
  option_candles?: CoverageBlock;
  collector_running?: boolean;
  market_hours?: boolean;
  tick_backfill_supported?: boolean;
}

interface JobProgress {
  running?: boolean; job?: string | null; label?: string;
  status?: string; output_lines?: string[]; exit_code?: number;
  started?: string; finished?: string;
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
  const [coverage, setCoverage] = useState<Coverage>({});
  const [job, setJob] = useState<JobProgress>({});
  const [backfillDays, setBackfillDays] = useState(30);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [rlData, modelData, covData] = await Promise.all([
        fetchJSON<RLStatus>("/api/rl/status").catch(() => ({})),
        fetchJSON<ModelsStatus>("/api/models/status").catch(() => ({})),
        fetchJSON<Coverage>("/api/data/coverage").catch(() => ({})),
      ]);
      setRl(rlData);
      setModels(modelData);
      setCoverage(covData);
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll job output while something is running, then refresh the panels so
  // a finished backfill/retrain is reflected without a manual click.
  useEffect(() => {
    if (!job.running) return;
    const t = setInterval(async () => {
      const p = await fetchJSON<JobProgress>("/api/maintenance/progress")
        .catch(() => ({} as JobProgress));
      setJob(p);
      if (!p.running) load();
    }, 2000);
    return () => clearInterval(t);
  }, [job.running, load]);

  const startJob = useCallback(async (path: string, body: object) => {
    try {
      await postJSON(path, body);
      setJob({ running: true, status: "running", output_lines: [] });
    } catch (e) {
      setJob({ running: false, status: "error", output_lines: [String(e)] });
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
              <>
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
                {/* A file existing is not the same as an agent that works.
                    Trained on a handful of episodes the Q-table collapses to
                    one action for every state — it never exits — and without
                    this the card would just read green. */}
                {(() => {
                  const dist = (rl.tabular as unknown as { policy_distribution?: Record<string, number> }).policy_distribution;
                  const acted = dist ? Object.values(dist).filter(v => v > 0).length : 2;
                  const eps = rl.tabular?.episodes ?? 0;
                  if (acted > 1 && eps >= 1000) return null;
                  const only = dist ? Object.entries(dist).find(([, v]) => v > 0)?.[0] : null;
                  return (
                    <p className="text-[10px] mt-3 p-2" style={{ background: '#2a0a0a', border: '1px solid #5c1a1a', color: '#ff6b6b' }}>
                      Degenerate policy — trained on {eps.toLocaleString()} episodes
                      {only ? `, and it picks ${only} in every one of ${rl.tabular?.states} states` : ""}.
                      It is loaded but has learned nothing useful; treat it as untrained until there
                      are far more backtest journeys to learn from.
                    </p>
                  );
                })()}
              </>
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

        {/* Training data — what the models actually have to learn from */}
        <div className="t-panel p-5 mb-4">
          <div className="flex items-center justify-between mb-1">
            <h3 className="text-[12px] font-bold uppercase tracking-wider" style={{ color: '#e8c300' }}>
              Market Data — Model Training Input
            </h3>
            {job.running && (
              <span className="text-[10px]" style={{ color: '#e8c300' }}>
                ⏳ {job.label || job.job} — running
              </span>
            )}
          </div>
          <p className="text-[10px] mb-4" style={{ color: '#5a6270' }}>
            The models can only be as good as what is in the database. Read live from Postgres.
          </p>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-[1px] mb-4">
            {/* Candles */}
            <div className="p-3" style={{ background: '#111318', border: '1px solid #1e222c' }}>
              <p className="text-[10px] uppercase tracking-wider mb-1" style={{ color: '#5a6270' }}>
                NIFTY-I 1-min candles
              </p>
              <p className="text-[16px] font-bold" style={{ color: (coverage.candles?.days ?? 0) >= 60 ? '#00e87b' : '#e8c300' }}>
                {coverage.candles?.days ?? "—"} <span className="text-[10px] font-normal" style={{ color: '#5a6270' }}>days</span>
              </p>
              <p className="text-[10px]" style={{ color: '#5a6270' }}>
                {(coverage.candles?.bars ?? 0).toLocaleString()} bars
              </p>
              <p className="text-[10px]" style={{ color: '#3d4450' }}>
                {coverage.candles?.first_day} → {coverage.candles?.last_day}
              </p>
              <p className="text-[9px] mt-1" style={{ color: '#3d4450' }}>Feeds macro + strategy models</p>
            </div>

            {/* Ticks */}
            <div className="p-3" style={{ background: '#111318', border: '1px solid #1e222c' }}>
              <p className="text-[10px] uppercase tracking-wider mb-1" style={{ color: '#5a6270' }}>
                Tick history
              </p>
              <p className="text-[16px] font-bold" style={{ color: (coverage.ticks?.days ?? 0) >= 5 ? '#00e87b' : '#ff3e3e' }}>
                {coverage.ticks?.days ?? "—"} <span className="text-[10px] font-normal" style={{ color: '#5a6270' }}>days</span>
              </p>
              <p className="text-[10px]" style={{ color: '#5a6270' }}>
                {(coverage.ticks?.rows ?? 0).toLocaleString()} ticks
              </p>
              <p className="text-[10px]" style={{ color: '#3d4450' }}>
                {coverage.ticks?.first_day} → {coverage.ticks?.last_day}
              </p>
              <p className="text-[9px] mt-1" style={{ color: '#3d4450' }}>Feeds the micro model</p>
            </div>

            {/* Option candles */}
            <div className="p-3" style={{ background: '#111318', border: '1px solid #1e222c' }}>
              <p className="text-[10px] uppercase tracking-wider mb-1" style={{ color: '#5a6270' }}>
                Option candles
              </p>
              <p className="text-[16px] font-bold" style={{ color: '#4da6ff' }}>
                {coverage.option_candles?.symbols ?? "—"} <span className="text-[10px] font-normal" style={{ color: '#5a6270' }}>contracts</span>
              </p>
              <p className="text-[10px]" style={{ color: '#5a6270' }}>
                {(coverage.option_candles?.bars ?? 0).toLocaleString()} bars
              </p>
              <p className="text-[9px] mt-1" style={{ color: '#3d4450' }}>Backtest option premiums</p>
            </div>
          </div>

          {/* Honest note: this is a hard platform limit, not a missing button */}
          {coverage.tick_backfill_supported === false && (
            <p className="text-[10px] mb-3 p-2" style={{ background: '#1a1a0a', border: '1px solid #5c5c1a', color: '#e8c300' }}>
              Tick history cannot be backfilled — AngelOne&apos;s free tier has no historical tick endpoint.
              It only accumulates forward from the live collector during market hours
              (collector now: <b>{coverage.collector_running ? "running" : "not running"}</b>
              {coverage.market_hours ? "" : ", market closed"}). Candles are unaffected and backfill normally.
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider" style={{ color: '#5a6270' }}>Backfill days</label>
            <input type="number" min={1} max={365} value={backfillDays}
              onChange={e => setBackfillDays(Number(e.target.value))}
              className="px-2 py-[5px] text-[11px] w-20"
              style={{ background: '#0e1117', border: '1px solid #252a33', color: '#c8cdd5' }} />
            <button onClick={() => startJob("/api/data/backfill", { days: backfillDays })}
              disabled={job.running}
              className="t-btn px-3 py-[5px] text-[10px] font-semibold uppercase tracking-wider disabled:opacity-40">
              Load Market Data
            </button>
            <button onClick={() => startJob("/api/models/train", { target: "macro" })}
              disabled={job.running}
              className="t-btn px-3 py-[5px] text-[10px] font-semibold uppercase tracking-wider disabled:opacity-40">
              Retrain Macro + Strategy
            </button>
            <button onClick={() => startJob("/api/models/train", { target: "rl" })}
              disabled={job.running}
              className="t-btn px-3 py-[5px] text-[10px] font-semibold uppercase tracking-wider disabled:opacity-40">
              Train RL Exit Agent
            </button>
          </div>

          {(job.output_lines?.length ?? 0) > 0 && (
            <div className="mt-3 p-2 max-h-48 overflow-y-auto text-[10px]"
              style={{ background: '#0d1117', border: '1px solid #1e222c', color: '#8b949e', fontFamily: 'JetBrains Mono' }}>
              {job.output_lines?.map((l, i) => <div key={i}>{l}</div>)}
              {!job.running && job.status && (
                <div style={{ color: job.status === "done" ? '#00e87b' : '#ff3e3e' }}>
                  — {job.status.toUpperCase()}{job.finished ? ` at ${job.finished}` : ""}
                </div>
              )}
            </div>
          )}
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
