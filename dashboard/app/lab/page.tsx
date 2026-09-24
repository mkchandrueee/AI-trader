"use client";

import { useCallback, useEffect, useState } from "react";
import Sidebar from "@/components/Sidebar";
import { API_BASE } from "@/lib/api";
import { RefreshCw, Microscope, Check, X, Minus, Lock, Ruler, SlidersHorizontal, AlertTriangle } from "lucide-react";

const GREEN = "#00e87b";
const RED = "#ff3e3e";
const AMBER = "#e8a200";
const MUTED = "#5a6270";
const DIM = "#3d4450";
const TEXT = "#c8cdd5";
const BORDER = "#252a33";
const INK = "#0e1117";

interface ParamRow {
  key: string; group: string; category: "tactical" | "locked" | "measured";
  unit: string; desc: string; module: string; attr: string;
  value: number | string | null; lo: number | null; hi: number | null; violation: string | null;
  provenance: string | null; assumed: boolean;
}

interface BacktestRow {
  at: string; script: string; headline: string; headline_value: number | null;
  out_of_sample: boolean; cost_pct: number; verdict: string;
  split: { train_sessions: number; test_sessions: number; train_signals: number; test_signals: number; period: string };
  detail: {
    configurations_tried_per_pair?: number; cells_evaluated?: number;
    pct_cells_profitable_train?: number | null; pct_cells_profitable_test?: number | null;
    train_test_net_correlation?: number | null;
    per_pair?: { pattern: string; entry: string; train_net_pct: number; test_net_pct: number; test_n: number }[];
  };
}

interface Evidence {
  basis: string; n: number; sufficient_sample: boolean;
  win_rate?: number; win_rate_ci95?: [number, number];
  expectancy_pct?: number | null; avg_win_pct?: number | null; avg_loss_pct?: number | null;
  profit_factor?: number | null; max_drawdown_rupees?: number; error?: string;
}

interface GateCheck { label: string; status: "pass" | "fail" | "pending"; detail: string }

interface Strategy {
  id: string; title: string; trades: boolean; instrument: string; what: string; not: string;
  params: ParamRow[];
  costs?: ParamRow[];   // absent when the backend predates the cost panel
  backtest: BacktestRow | null;
  backtest_history: BacktestRow[];
  evidence: Evidence;
  registry: {
    key: string; state: string; updated_at: string | null;
    history: { from: string; to: string; reason: string; actor: string; at: string }[];
  };
  gate: GateCheck[];
  gate_summary: { passed: number; failed: number; pending: number; total: number };
}

const CAT_META: Record<string, { color: string; icon: typeof Lock; label: string }> = {
  tactical: { color: GREEN, icon: SlidersHorizontal, label: "TACTICAL" },
  locked: { color: AMBER, icon: Lock, label: "LOCKED" },
  measured: { color: "#4aa3ff", icon: Ruler, label: "MEASURED" },
};

const STATE_COLOR: Record<string, string> = {
  PAPER_TESTING: AMBER,
  LIVE_ASSISTED_ELIGIBLE: GREEN,
  SUSPENDED: RED,
};

const STAGES = ["SPEC", "PARAMS", "BACKTEST", "EVIDENCE", "REGISTRY"];

function pct(v: number | null | undefined, digits = 2) {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;
}

export default function LabPage() {
  const [data, setData] = useState<Strategy[]>([]);
  const [legend, setLegend] = useState<Record<string, string>>({});
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [active, setActive] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [p, q] = await Promise.all([
        fetch(`${API_BASE}/api/lab/pipeline`).then(r => r.json()),
        fetch(`${API_BASE}/api/lab/params`).then(r => r.json()),
      ]);
      if (p.error) throw new Error(p.error);
      setData(p.strategies || []);
      setLegend(q.legend || {});
      setCounts(q.counts || {});
      setActive(a => a || (p.strategies?.[0]?.id ?? ""));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const s = data.find(d => d.id === active);

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col min-h-screen overflow-hidden">
        <main className="flex-1 p-5 overflow-y-auto">
          <div className="mb-5 flex items-end justify-between flex-wrap gap-3">
            <div>
              <h1 className="text-sm font-bold uppercase tracking-wider flex items-center gap-2" style={{ color: GREEN }}>
                <Microscope className="w-4 h-4" /> Strategy Lab
              </h1>
              <p className="text-[10px] mt-0.5" style={{ color: MUTED }}>
                Every strategy the platform runs, and exactly how far each one has got towards being trusted with
                real money. Nothing on this page promotes anything.
              </p>
            </div>
            <button onClick={load} disabled={loading}
              className="t-btn t-btn-green flex items-center gap-1.5 px-4 py-[6px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50">
              <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} /> Refresh
            </button>
          </div>

          {error && (
            <div className="t-panel p-4 mb-5" style={{ borderColor: RED }}>
              <p className="text-[11px]" style={{ color: RED }}>ERROR: {error}</p>
            </div>
          )}

          {/* strategy selector */}
          <div className="flex gap-1 mb-5 flex-wrap" style={{ borderBottom: `1px solid ${BORDER}` }}>
            {data.map(d => (
              <button key={d.id} onClick={() => setActive(d.id)}
                className="px-4 py-2 text-[11px] font-semibold uppercase tracking-wider flex items-center gap-2"
                style={{
                  color: active === d.id ? GREEN : MUTED,
                  borderBottom: active === d.id ? `2px solid ${GREEN}` : "2px solid transparent",
                }}>
                {d.title}
                <span className="px-1.5 py-[1px] text-[8px]"
                  style={{ border: `1px solid ${STATE_COLOR[d.registry.state] ?? MUTED}`, color: STATE_COLOR[d.registry.state] ?? MUTED }}>
                  {d.registry.state.replace(/_/g, " ")}
                </span>
              </button>
            ))}
          </div>

          {!s && !loading && !error && (
            <p className="text-[11px] text-center py-10" style={{ color: DIM }}>No strategies registered.</p>
          )}

          {s && (
            <>
              {/* ── stage rail ─────────────────────────────────────────────── */}
              <div className="flex items-stretch gap-1 mb-5 flex-wrap">
                {STAGES.map((label, i) => {
                  const done = [
                    true,
                    s.params.length > 0,
                    !!s.backtest,
                    s.evidence.n >= 20,
                    s.registry.state === "LIVE_ASSISTED_ELIGIBLE",
                  ][i];
                  const bad = i === 2 ? (s.backtest?.headline_value ?? 1) <= 0
                    : i === 3 ? (s.evidence.profit_factor ?? 1) < 1 && s.evidence.n >= 20
                    : i === 4 ? s.registry.state === "SUSPENDED" : false;
                  const c = bad ? RED : done ? GREEN : DIM;
                  return (
                    <div key={label} className="flex-1 min-w-[110px] p-2" style={{ background: INK, border: `1px solid ${BORDER}`, borderLeft: `2px solid ${c}` }}>
                      <div className="text-[8px]" style={{ color: DIM }}>STAGE {i + 1}</div>
                      <div className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: c }}>{label}</div>
                    </div>
                  );
                })}
              </div>

              {/* ── the gate ───────────────────────────────────────────────── */}
              <div className="t-panel p-4 mb-5">
                <div className="flex items-center justify-between flex-wrap gap-2 mb-3">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: MUTED }}>
                    Promotion gate — what stands between this and live-assisted
                  </h3>
                  <span className="text-[9px]" style={{ color: DIM }}>
                    {s.gate_summary.passed} passed · {s.gate_summary.failed} failed · {s.gate_summary.pending} not yet measured
                  </span>
                </div>
                <div className="grid gap-[2px]">
                  {s.gate.map(c => {
                    const col = c.status === "pass" ? GREEN : c.status === "fail" ? RED : MUTED;
                    const Icon = c.status === "pass" ? Check : c.status === "fail" ? X : Minus;
                    return (
                      <div key={c.label} className="flex gap-2 items-start py-[6px] px-2" style={{ background: INK }}>
                        <Icon className="w-3 h-3 mt-[1px] shrink-0" style={{ color: col }} />
                        <div className="min-w-0">
                          <div className="text-[10px] font-semibold" style={{ color: col }}>{c.label}</div>
                          <div className="text-[10px] leading-snug" style={{ color: MUTED }}>{c.detail}</div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="grid gap-5 lg:grid-cols-2">
                {/* ── 1 SPEC ───────────────────────────────────────────────── */}
                <div className="t-panel p-4">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider mb-3" style={{ color: MUTED }}>
                    1 · Spec
                  </h3>
                  <div className="flex gap-2 mb-3 flex-wrap">
                    <span className="px-2 py-[2px] text-[9px] uppercase tracking-wider"
                      style={{ border: `1px solid ${s.trades ? GREEN : DIM}`, color: s.trades ? GREEN : DIM }}>
                      {s.trades ? "places paper orders" : "research only — places no orders"}
                    </span>
                    <span className="px-2 py-[2px] text-[9px]" style={{ border: `1px solid ${BORDER}`, color: MUTED }}>
                      {s.instrument}
                    </span>
                  </div>
                  <p className="text-[11px] leading-relaxed mb-2" style={{ color: TEXT }}>{s.what}</p>
                  <p className="text-[10px] leading-relaxed" style={{ color: MUTED }}>
                    <span style={{ color: AMBER }}>Does not: </span>{s.not}
                  </p>
                </div>

                {/* ── 5 REGISTRY ───────────────────────────────────────────── */}
                <div className="t-panel p-4">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider mb-3" style={{ color: MUTED }}>
                    5 · Registry state
                  </h3>
                  <div className="flex items-baseline gap-3 mb-1">
                    <span className="text-[15px] font-bold tracking-wider"
                      style={{ color: STATE_COLOR[s.registry.state] ?? MUTED }}>
                      {s.registry.state.replace(/_/g, " ")}
                    </span>
                    <span className="text-[9px]" style={{ color: DIM }}>
                      {s.registry.updated_at ? `since ${s.registry.updated_at.slice(0, 16).replace("T", " ")}` : "never changed"}
                    </span>
                  </div>
                  <p className="text-[9px] mb-3" style={{ color: DIM }}>key: {s.registry.key}</p>
                  {s.registry.history.length === 0 ? (
                    <p className="text-[10px]" style={{ color: DIM }}>No state changes recorded — still at its default.</p>
                  ) : (
                    <div className="grid gap-[2px]">
                      {[...s.registry.history].reverse().map((h, i) => (
                        <div key={i} className="p-2" style={{ background: INK }}>
                          <div className="text-[9px] mb-[2px]">
                            <span style={{ color: DIM }}>{h.at.slice(0, 16).replace("T", " ")} · {h.actor} · </span>
                            <span style={{ color: STATE_COLOR[h.from] ?? MUTED }}>{h.from.replace(/_/g, " ")}</span>
                            <span style={{ color: DIM }}> → </span>
                            <span style={{ color: STATE_COLOR[h.to] ?? MUTED }}>{h.to.replace(/_/g, " ")}</span>
                          </div>
                          <div className="text-[10px] leading-snug" style={{ color: MUTED }}>{h.reason}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* ── 3 BACKTEST ───────────────────────────────────────────── */}
                <div className="t-panel p-4">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider mb-3" style={{ color: MUTED }}>
                    3 · Backtest on record
                  </h3>
                  {!s.backtest ? (
                    <p className="text-[10px]" style={{ color: DIM }}>
                      No run recorded. Research that is only printed to a terminal does not count here —
                      the run has to call <code style={{ color: MUTED }}>models.backtest_record.record()</code>.
                    </p>
                  ) : (
                    <>
                      <div className="flex items-baseline gap-2 mb-1">
                        <span className="text-[22px] font-bold"
                          style={{ color: (s.backtest.headline_value ?? 0) > 0 ? GREEN : RED }}>
                          {s.backtest.headline_value === null ? "—" : `${s.backtest.headline_value > 0 ? "+" : ""}${s.backtest.headline_value.toFixed(3)}%`}
                        </span>
                        <span className="text-[9px]" style={{ color: MUTED }}>per trade</span>
                      </div>
                      <p className="text-[10px] mb-2" style={{ color: MUTED }}>{s.backtest.headline}</p>
                      <div className="grid grid-cols-2 gap-x-3 gap-y-[2px] text-[10px] mb-3">
                        <span style={{ color: DIM }}>out of sample</span>
                        <span style={{ color: s.backtest.out_of_sample ? GREEN : RED }}>
                          {s.backtest.out_of_sample ? "yes — held-out sessions" : "no — in-sample only"}
                        </span>
                        <span style={{ color: DIM }}>round-trip cost applied</span>
                        <span style={{ color: TEXT }}>{(s.backtest.cost_pct * 100).toFixed(3)}%</span>
                        <span style={{ color: DIM }}>train / test sessions</span>
                        <span style={{ color: TEXT }}>{s.backtest.split.train_sessions} / {s.backtest.split.test_sessions}</span>
                        <span style={{ color: DIM }}>train / test signals</span>
                        <span style={{ color: TEXT }}>{s.backtest.split.train_signals} / {s.backtest.split.test_signals}</span>
                        <span style={{ color: DIM }}>period</span>
                        <span style={{ color: TEXT }}>{s.backtest.split.period}</span>
                        <span style={{ color: DIM }}>recorded</span>
                        <span style={{ color: TEXT }}>{s.backtest.at.slice(0, 16).replace("T", " ")} · {s.backtest.script}</span>
                      </div>
                      <p className="text-[10px] leading-snug p-2 mb-2" style={{ background: INK, color: TEXT }}>
                        {s.backtest.verdict}
                      </p>
                      {s.backtest.detail.per_pair && s.backtest.detail.per_pair.length > 0 && (
                        <details>
                          <summary className="text-[10px] cursor-pointer" style={{ color: MUTED }}>
                            Per pattern / entry ({s.backtest.detail.cells_evaluated ?? 0} cells evaluated,
                            {" "}{s.backtest.detail.configurations_tried_per_pair ?? 0} stop/target configurations each)
                          </summary>
                          <table className="w-full mt-2 text-[10px]">
                            <thead>
                              <tr style={{ color: DIM }}>
                                <th className="text-left font-normal">pattern</th>
                                <th className="text-left font-normal">entry</th>
                                <th className="text-right font-normal">train net%</th>
                                <th className="text-right font-normal">test net%</th>
                                <th className="text-right font-normal">test n</th>
                              </tr>
                            </thead>
                            <tbody>
                              {s.backtest.detail.per_pair.map((r, i) => (
                                <tr key={i}>
                                  <td style={{ color: TEXT }}>{r.pattern}</td>
                                  <td style={{ color: MUTED }}>{r.entry}</td>
                                  <td className="text-right" style={{ color: r.train_net_pct > 0 ? GREEN : RED }}>{r.train_net_pct.toFixed(3)}</td>
                                  <td className="text-right" style={{ color: r.test_net_pct > 0 ? GREEN : RED }}>{r.test_net_pct.toFixed(3)}</td>
                                  <td className="text-right" style={{ color: MUTED }}>{r.test_n}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                          {s.backtest.detail.pct_cells_profitable_test !== null && s.backtest.detail.pct_cells_profitable_test !== undefined && (
                            <p className="text-[9px] mt-2" style={{ color: DIM }}>
                              cells profitable on train {s.backtest.detail.pct_cells_profitable_train?.toFixed(0)}% ·
                              {" "}on test {s.backtest.detail.pct_cells_profitable_test.toFixed(0)}% ·
                              {" "}train→test correlation {s.backtest.detail.train_test_net_correlation?.toFixed(2)}
                            </p>
                          )}
                        </details>
                      )}
                    </>
                  )}
                </div>

                {/* ── 4 EVIDENCE ───────────────────────────────────────────── */}
                <div className="t-panel p-4">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider mb-3" style={{ color: MUTED }}>
                    4 · Live paper evidence
                  </h3>
                  {s.evidence.n === 0 ? (
                    <p className="text-[10px]" style={{ color: DIM }}>
                      {s.trades
                        ? "No closed paper trades recorded yet."
                        : "This strategy places no orders, so it has no paper record — and cannot acquire one without being wired to the agent first."}
                    </p>
                  ) : (
                    <>
                      <div className="grid grid-cols-2 gap-x-3 gap-y-[3px] text-[11px]">
                        <span style={{ color: DIM }}>closed trades</span>
                        <span style={{ color: TEXT }}>{s.evidence.n}</span>
                        <span style={{ color: DIM }}>win rate</span>
                        <span style={{ color: TEXT }}>
                          {pct(s.evidence.win_rate, 1)}
                          {s.evidence.win_rate_ci95 && (
                            <span style={{ color: DIM }}> (95% CI {pct(s.evidence.win_rate_ci95[0], 0)}–{pct(s.evidence.win_rate_ci95[1], 0)})</span>
                          )}
                        </span>
                        <span style={{ color: DIM }}>profit factor</span>
                        <span style={{ color: (s.evidence.profit_factor ?? 0) >= 1 ? GREEN : RED }}>
                          {s.evidence.profit_factor === null || s.evidence.profit_factor === undefined ? "—" : s.evidence.profit_factor.toFixed(2)}
                        </span>
                        <span style={{ color: DIM }}>expectancy</span>
                        <span style={{ color: (s.evidence.expectancy_pct ?? 0) >= 0 ? GREEN : RED }}>
                          {pct(s.evidence.expectancy_pct)} / trade
                        </span>
                        <span style={{ color: DIM }}>avg win / avg loss</span>
                        <span style={{ color: TEXT }}>{pct(s.evidence.avg_win_pct)} / {pct(s.evidence.avg_loss_pct)}</span>
                        <span style={{ color: DIM }}>max drawdown</span>
                        <span style={{ color: RED }}>{(s.evidence.max_drawdown_rupees ?? 0) < 0 ? "-" : ""}₹{Math.abs(s.evidence.max_drawdown_rupees ?? 0).toLocaleString("en-IN")}</span>
                      </div>
                      {!s.evidence.sufficient_sample && (
                        <p className="text-[9px] mt-3 p-2" style={{ background: INK, color: AMBER }}>
                          Sample too small to draw a conclusion from — the confidence interval above is the honest
                          width of what this evidence actually supports.
                        </p>
                      )}
                    </>
                  )}
                </div>
              </div>

              {/* ── costs this strategy's numbers are net of ─────────────────── */}
              {(s.costs?.length ?? 0) > 0 && (
                <div className="t-panel p-4 mt-5">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: MUTED }}>
                    Costs these numbers are net of
                  </h3>
                  <p className="text-[9px] mb-3" style={{ color: DIM }}>
                    A result is only as trustworthy as the cost it is net of. Anything marked ASSUMED has not been
                    measured against real data.
                  </p>
                  <div className="grid gap-[2px]">
                    {s.costs!.map(c => (
                      <div key={c.key} className="p-2" style={{ background: INK, borderLeft: `2px solid ${c.assumed ? AMBER : "#4aa3ff"}` }}>
                        <div className="flex items-baseline justify-between gap-3 flex-wrap">
                          <span className="text-[10px] font-semibold flex items-center gap-1.5" style={{ color: TEXT }}>
                            {c.assumed
                              ? <AlertTriangle className="w-3 h-3" style={{ color: AMBER }} />
                              : <Ruler className="w-3 h-3" style={{ color: "#4aa3ff" }} />}
                            {c.key}
                          </span>
                          <span className="text-[12px] font-bold" style={{ color: c.assumed ? AMBER : "#4aa3ff" }}>
                            {typeof c.value === "number" ? `${(c.value * 100).toFixed(3)}%` : String(c.value)}
                            <span className="text-[9px] font-normal ml-1" style={{ color: DIM }}>round trip</span>
                          </span>
                        </div>
                        <div className="text-[10px] leading-snug mt-[2px]" style={{ color: MUTED }}>{c.desc}</div>
                        {c.provenance && (
                          <div className="text-[9px] mt-1" style={{ color: c.assumed ? AMBER : DIM }}>{c.provenance}</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* ── 2 PARAMS ─────────────────────────────────────────────────── */}
              <div className="t-panel p-4 mt-5">
                <div className="flex items-center justify-between flex-wrap gap-2 mb-3">
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: MUTED }}>
                    2 · Parameters ({s.params.length} of {counts.total ?? 0} platform-wide)
                  </h3>
                  <div className="flex gap-2 flex-wrap">
                    {Object.entries(CAT_META).map(([k, m]) => (
                      <span key={k} className="flex items-center gap-1 text-[9px] uppercase tracking-wider" style={{ color: m.color }}>
                        <m.icon className="w-3 h-3" /> {m.label}
                      </span>
                    ))}
                  </div>
                </div>
                <div className="grid gap-[2px]">
                  {s.params.map(p => {
                    const m = CAT_META[p.category];
                    const num = typeof p.value === "number";
                    const frac = num && p.lo !== null && p.hi !== null && p.hi > p.lo
                      ? Math.min(1, Math.max(0, ((p.value as number) - p.lo) / (p.hi - p.lo))) : null;
                    return (
                      <div key={p.key} className="p-2" style={{ background: INK, borderLeft: `2px solid ${m.color}` }}>
                        <div className="flex items-baseline justify-between gap-3 flex-wrap">
                          <span className="text-[10px] font-semibold" style={{ color: TEXT }}>
                            {p.key}
                            <span className="ml-2 text-[9px]" style={{ color: DIM }}>{p.module.split(".").pop()}.{p.attr}</span>
                          </span>
                          <span className="text-[11px] font-bold" style={{ color: m.color }}>
                            {String(p.value)} <span className="text-[9px] font-normal" style={{ color: DIM }}>{p.unit}</span>
                          </span>
                        </div>
                        {frac !== null && (
                          <div className="relative h-[3px] my-[5px]" style={{ background: BORDER }}>
                            <div className="absolute h-[3px] w-[3px]" style={{ background: m.color, left: `calc(${frac * 100}% - 1px)` }} />
                          </div>
                        )}
                        <div className="flex items-baseline justify-between gap-3 flex-wrap">
                          <span className="text-[10px] leading-snug" style={{ color: MUTED }}>{p.desc}</span>
                          {p.lo !== null && p.hi !== null && (
                            <span className="text-[9px] shrink-0" style={{ color: DIM }}>bounds {p.lo} … {p.hi}</span>
                          )}
                        </div>
                        {p.provenance && (
                          <div className="text-[9px] mt-1" style={{ color: p.assumed ? AMBER : DIM }}>{p.provenance}</div>
                        )}
                        {p.violation && (
                          <div className="text-[9px] mt-1" style={{ color: RED }}>OUT OF BOUNDS: {p.violation}</div>
                        )}
                      </div>
                    );
                  })}
                </div>
                <div className="grid gap-1 mt-3">
                  {Object.entries(legend).map(([k, v]) => (
                    <p key={k} className="text-[9px]" style={{ color: DIM }}>
                      <span style={{ color: CAT_META[k]?.color ?? MUTED }}>{CAT_META[k]?.label ?? k}</span> — {v}
                    </p>
                  ))}
                </div>
              </div>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
