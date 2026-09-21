"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "@/lib/api";
import { RefreshCw } from "lucide-react";

type PatternKey = "ipo" | "vcp" | "horizontal" | "flag" | "cup" | "triangle" | "rsi" | "hammer";
type StateKey = "breakout" | "coiling" | "breakdown";
type SubTab = "regime" | "sectors" | PatternKey;

interface Setup {
  symbol: string;
  pattern: PatternKey;
  state: StateKey;
  score: number;
  entry: number;
  stop: number;
  close: number;
  risk_pct: number | null;
  box: [number, number, number] | null; // [bars_back, top, bottom]
  rs_pct: number | null;
  change_pct: number | null;
  vol_ratio?: number;
  pole_gain_pct?: number;
  retrace_pct?: number;
  flag_bars?: number;
  depth_pct?: number;
  touches?: number;
  contractions_pct?: number[];
  sessions_listed?: number;
  target?: number;
  rr?: number | null;
  variant?: string;
  cup_depth_pct?: number;
  cup_bars?: number;
  handle_bars?: number;
  handle_vol_up?: boolean;
  flag_slope_pct?: number;
  prior_trend_pct?: number;
  vol_dry_up?: boolean;
  rsi?: number;
  rsi_low1?: number;
  rsi_low2?: number;
  rsi_high1?: number;
  rsi_high2?: number;
  near_support_pct?: number;
}

interface Sector {
  sector: string;
  stocks: number;
  ret_1m_pct: number;
  ret_3m_pct: number;
  pct_above_sma50: number;
  setups: number;
  strength: number;
}

interface ReplayRow {
  n: number;
  hit_rate_pct: number | null;
  drawdown_rate_pct: number | null;
  median_fwd_return_pct: number | null;
  hit_lift?: number | null;
  // directional test (added 2026-09-21): the hit-rate lift is NOT one, it rises with volatility alone
  mean_fwd_return_pct?: number | null;
  mean_abs_move_pct?: number | null;
  vs_baseline_pct?: number | null;
  t_stat?: number | null;
  move_ratio?: number | null;
}

interface Replay {
  horizon_sessions: number;
  hit_threshold_pct: number;
  drawdown_threshold_pct: number;
  baseline: ReplayRow;
  patterns: Record<PatternKey, ReplayRow>;
  asof: string;
  sessions: number;
}

interface ScanResponse {
  asof?: string;
  sessions?: number;
  universe?: number;
  regime?: {
    label: "BULLISH" | "NEUTRAL" | "BEARISH" | "UNKNOWN";
    posture?: string;
    breadth: Record<string, number | null>;
  };
  sectors?: Sector[];
  sectors_available?: boolean;
  setups?: Record<PatternKey, Setup[]>;
  counts?: Record<PatternKey, { total: number; breakout: number; breakdown?: number }>;
  replay?: Replay | null;
  notes?: string[];
  error?: string;
}

interface ChartData {
  dates: string[];
  open: number[];
  high: number[];
  low: number[];
  close: number[];
  volume: number[];
  sma21: (number | null)[];
  sma50: (number | null)[];
}

interface SyncStatus {
  running: boolean;
  message: string;
  error: string | null;
  finished: string | null;
}

const PATTERN_META: Record<PatternKey, { label: string; blurb: string }> = {
  ipo: {
    label: "IPO Base",
    blurb: "Recently listed stocks (inside our data window) that have based near their highs — a break of the post-listing high is the trigger.",
  },
  vcp: {
    label: "VCP Setups",
    blurb: "Volatility contraction: an uptrend (price > SMA50 > SMA100) whose last three 15-session ranges keep shrinking, near the high on drying volume. Trigger is the pivot high.",
  },
  horizontal: {
    label: "Horizontal Break",
    blurb: "A tight 25-session box with a flat ceiling touched at least twice. Breakout = close above the ceiling; coiling = within 3% below it.",
  },
  flag: {
    label: "Flag & Pole",
    blurb: "ChartBank rules: a steep 25%+ pole, then a flag that drifts sideways or down against it (retrace under 50%). Enter on a close above the flag top with heavy volume; target = the pole's height added to the breakout; exit on a close below the flag's lower line.",
  },
  cup: {
    label: "Cup & Handle",
    blurb: "ChartBank rules: a rounded (not V-shaped) cup with roughly level rims, then a short handle that gives back no more than a third of the cup's height, on rising volume. Enter on the handle breakout; target = the cup's height added to the breakout; the handle low is the final support.",
  },
  triangle: {
    label: "Triangles",
    blurb: "ChartBank rules: two converging trend lines with 2+ touches each after an established prior trend, volume drying up inside. Ascending and symmetric-in-an-uptrend break up (enter on a close above the top line); descending / symmetric-in-a-downtrend are shown under Breakdown for awareness. Target = the triangle's widest height from the breakout.",
  },
  rsi: {
    label: "RSI Divergence",
    blurb: "ChartBank rules: in a clear trend, price makes a lower low while RSI makes a higher low (bullish), or a higher high on a lower RSI high (bearish, under Breakdown). Confirm by price closing above the swing high between the two lows; target = the nearest resistance. Ignore sideways markets and combine with support/resistance.",
  },
  hammer: {
    label: "Hammer",
    blurb: "ChartBank rules: a T-shaped candle after a decline, near a support zone (RSI under 30 and a volume jump add weight). Aggressive = enter on the completed hammer (Coiling); safe = wait for the next candle to close above its high (Breakout). Stop just under the hammer low; target = the nearest resistance. The author's intraday version uses 30-minute candles — see the NIFTY card below.",
  },
};

const PATTERN_ORDER: PatternKey[] = ["ipo", "vcp", "horizontal", "flag", "cup", "triangle", "rsi", "hammer"];

const C = { green: "#00e87b", red: "#ff3e3e", amber: "#e8c300", blue: "#4da6ff", dim: "#5a6270", faint: "#3d4450", text: "#c8cdd5", panel: "#181c24", line: "#252a33" };

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  return res.json();
}

const inr = (n: number) => `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;

/* ---------- candlestick chart (plain SVG, no chart lib) ---------- */
function CandleChart({ symbol, setup }: { symbol: string; setup: Setup }) {
  const [data, setData] = useState<ChartData | null>(null);
  const [err, setErr] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) {
        io.disconnect();
        getJSON<ChartData & { error?: string }>(`/api/positional/chart?symbol=${encodeURIComponent(symbol)}&sessions=70`)
          .then((d) => { if (live) { if (d.error) setErr(true); else setData(d); } })
          .catch(() => live && setErr(true));
      }
    });
    io.observe(el);
    return () => { live = false; io.disconnect(); };
  }, [symbol]);

  const W = 560, H = 230, PAD_R = 58, PAD_T = 8, VOL_H = 34, PRICE_H = H - VOL_H - PAD_T - 4;

  const body = useMemo(() => {
    if (!data) return null;
    const n = data.close.length;
    const plotW = W - PAD_R;
    const step = plotW / n;
    const lows = [...data.low, setup.stop];
    const showTarget = setup.target != null && setup.target <= Math.max(...data.high) * 1.3 && setup.target >= Math.min(...data.low) * 0.7;
    const highs = [...data.high, setup.entry, ...(showTarget && setup.target ? [setup.target] : [])];
    const min = Math.min(...lows), max = Math.max(...highs);
    const pad = (max - min) * 0.05 || 1;
    const lo = min - pad, hi = max + pad;
    const y = (p: number) => PAD_T + ((hi - p) / (hi - lo)) * PRICE_H;
    const x = (i: number) => i * step + step / 2;
    const vmax = Math.max(...data.volume, 1);
    const line = (arr: (number | null)[]) =>
      arr.map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean).join(" ");
    return { n, step, y, x, vmax, line, lo, hi, showTarget };
  }, [data, setup.entry, setup.stop, setup.target, PRICE_H]);

  return (
    <div ref={ref} style={{ background: "#0e1117", border: `1px solid ${C.line}` }}>
      {!data || !body ? (
        <div className="flex items-center justify-center text-[10px]" style={{ height: H, color: C.faint }}>
          {err ? "chart unavailable" : "loading chart…"}
        </div>
      ) : (
        <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block" }}>
          {setup.box && (
            <rect
              x={body.x(body.n - 1 - setup.box[0])} width={body.x(body.n - 1) - body.x(body.n - 1 - setup.box[0])}
              y={body.y(setup.box[1])} height={Math.max(1, body.y(setup.box[2]) - body.y(setup.box[1]))}
              fill="#4da6ff" opacity={0.12} stroke="#4da6ff" strokeOpacity={0.4} strokeDasharray="3 3"
            />
          )}
          {data.volume.map((v, i) => (
            <rect key={`v${i}`} x={body.x(i) - body.step * 0.35} width={body.step * 0.7}
              y={H - 2 - (v / body.vmax) * VOL_H} height={(v / body.vmax) * VOL_H}
              fill={data.close[i] >= data.open[i] ? C.green : C.red} opacity={0.35} />
          ))}
          {data.close.map((_, i) => {
            const up = data.close[i] >= data.open[i];
            const col = up ? C.green : C.red;
            const top = body.y(Math.max(data.open[i], data.close[i]));
            const bot = body.y(Math.min(data.open[i], data.close[i]));
            return (
              <g key={i}>
                <line x1={body.x(i)} x2={body.x(i)} y1={body.y(data.high[i])} y2={body.y(data.low[i])} stroke={col} strokeWidth={1} />
                <rect x={body.x(i) - body.step * 0.32} width={body.step * 0.64} y={top} height={Math.max(1, bot - top)} fill={col} />
              </g>
            );
          })}
          <polyline points={body.line(data.sma21)} fill="none" stroke={C.amber} strokeWidth={1} />
          <polyline points={body.line(data.sma50)} fill="none" stroke={C.blue} strokeWidth={1} />
          {([[setup.entry, "Entry", C.amber], [setup.stop, "Stop", C.blue],
             ...(body.showTarget && setup.target != null ? [[setup.target, "Target", C.green]] : [])] as [number, string, string][]).map(([p, label, col]) => (
            <g key={label}>
              <line x1={0} x2={W - PAD_R} y1={body.y(p)} y2={body.y(p)} stroke={col} strokeDasharray="4 3" strokeWidth={1} />
              <rect x={W - PAD_R + 2} y={body.y(p) - 7} width={PAD_R - 3} height={14} fill={col} />
              <text x={W - PAD_R + 5} y={body.y(p) + 3} fontSize={8} fill="#0e1117" fontWeight={700}>{label} {Math.round(p)}</text>
            </g>
          ))}
          <g>
            <rect x={W - PAD_R + 2} y={body.y(setup.close) - 7} width={PAD_R - 3} height={14} fill={setup.close >= setup.entry ? C.green : C.dim} />
            <text x={W - PAD_R + 5} y={body.y(setup.close) + 3} fontSize={8} fill="#0e1117" fontWeight={700}>{setup.close.toFixed(1)}</text>
          </g>
        </svg>
      )}
    </div>
  );
}

function SetupCard({ s }: { s: Setup }) {
  const bo = s.state === "breakout";
  return (
    <div className="p-3" style={{ background: C.panel, border: `1px solid ${C.line}` }}>
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-2">
          <b className="text-[13px]" style={{ color: "#fff" }}>{s.symbol}</b>
          <span className="px-1.5 py-[1px] text-[8px] uppercase tracking-wider"
            style={{ background: bo ? "#0a2a18" : s.state === "breakdown" ? "#2a0a0a" : "#1a1a0a",
              border: `1px solid ${bo ? "#1a5c3a" : s.state === "breakdown" ? "#5c1a1a" : "#3a3a1a"}`,
              color: bo ? C.green : s.state === "breakdown" ? C.red : C.amber }}>
            {s.state}{s.variant ? ` · ${s.variant}` : ""}
          </span>
        </div>
        <span className="px-1.5 py-[1px] text-[10px] font-bold" style={{ background: "#0d0f14", border: `1px solid ${C.line}`, color: C.text }}>{s.score}</span>
      </div>
      <div className="text-[10px] mb-2" style={{ color: C.dim }}>
        {inr(s.close)} {s.change_pct != null && <span style={{ color: s.change_pct >= 0 ? C.green : C.red }}>{s.change_pct >= 0 ? "+" : ""}{s.change_pct}%</span>}
        {s.rs_pct != null && <> · RS {s.rs_pct}</>}
        {s.vol_ratio != null && <> · vol {s.vol_ratio}×</>}
        {s.risk_pct != null && <> · risk {s.risk_pct}%</>}
        {s.rr != null && <> · R:R {s.rr}</>}
      </div>
      <CandleChart symbol={s.symbol} setup={s} />
    </div>
  );
}

function EvidenceBanner({ pattern, replay }: { pattern: PatternKey; replay?: Replay | null }) {
  if (!replay) {
    return (
      <div className="p-3 mb-4 text-[10px]" style={{ background: "#181c24", border: `1px solid ${C.line}`, color: C.dim }}>
        No replay evidence yet — press <b>Sync data</b> to measure how this pattern would have performed on the cached history.
      </div>
    );
  }
  const r = replay.patterns[pattern];
  if (!r) {
    return (
      <div className="p-3 mb-4 text-[10px]" style={{ background: "#181c24", border: `1px solid ${C.line}`, color: C.dim }}>
        This pattern isn&apos;t in the cached replay yet — press <b>Sync data</b> to measure it.
      </div>
    );
  }
  const b = replay.baseline;
  const small = r.n < 50;
  const t = r.t_stat ?? null;
  const edge = r.vs_baseline_pct ?? null;
  // A directional edge needs the forward RETURN to beat the baseline with real statistical weight (|t| >= 2).
  const directional = !small && t != null && edge != null && t >= 2 && edge > 0;
  const worse = !small && t != null && edge != null && t <= -2;
  const col = small ? C.amber : directional ? C.green : worse ? C.red : C.dim;
  const bigMoves = r.hit_lift != null && r.hit_lift >= 1.2 && !directional;
  return (
    <div className="p-3 mb-4 text-[10px] leading-relaxed" style={{ background: "#0d1a14", border: `1px solid ${small ? "#3a3a1a" : directional ? "#1a3a2a" : "#252a33"}`, color: C.text }}>
      <b style={{ color: col }}>Measured on our own data, not assumed.</b> Across {r.n} replayed breakouts in the last {replay.sessions} sessions, the average{" "}
      {replay.horizon_sessions}-day forward return was <b style={{ color: col }}>{r.mean_fwd_return_pct ?? "—"}%</b> vs {b.mean_fwd_return_pct ?? "—"}% for every stock-day
      {edge != null && t != null && <> (difference {edge > 0 ? "+" : ""}{edge}%, t = {t})</>}; median {r.median_fwd_return_pct}%.
      {small && <b style={{ color: C.amber }}> Small sample — do not read anything into this yet.</b>}
      {!small && directional && <b style={{ color: C.green }}> A statistically meaningful directional edge over the baseline.</b>}
      {!small && !directional && !worse && <span style={{ color: C.dim }}> No statistically distinguishable directional edge (needs t ≥ 2).</span>}
      {worse && <b style={{ color: C.red }}> Returns were meaningfully WORSE than an average stock-day.</b>}
      {bigMoves && (
        <span style={{ color: C.faint }}>
          {" "}Note: {r.hit_rate_pct}% rose +{replay.hit_threshold_pct}% vs {b.hit_rate_pct}% baseline ({r.hit_lift}×) — but that counts big moves, and this pattern&apos;s average move is
          {" "}{r.move_ratio ?? "—"}× the baseline in <i>either</i> direction. It shows volatility, not direction.
        </span>
      )}
    </div>
  );
}

/* ---------- NIFTY 30-minute hammer (ChartBank intraday strategy) ---------- */
interface IntradayResp {
  symbol: string;
  last_bar?: string;
  bars?: number;
  note?: string;
  error?: string;
  signals: (Setup & { hammer_time: string; seen_at: string })[];
}

function IntradayHammer() {
  const [d, setD] = useState<IntradayResp | null>(null);
  useEffect(() => { getJSON<IntradayResp>("/api/positional/intraday-hammer").then(setD).catch(() => setD({ symbol: "NIFTY-I", signals: [], error: "request failed" })); }, []);
  return (
    <div className="mb-5 p-3" style={{ background: C.panel, border: `1px solid ${C.line}` }}>
      <div className="text-[11px] font-bold uppercase tracking-wider mb-1" style={{ color: C.blue }}>NIFTY futures · 30-minute hammer (ChartBank intraday strategy)</div>
      <p className="text-[10px] mb-2" style={{ color: C.dim }}>
        Completed 30-minute bars from our own collected minute candles. Coiling = the hammer just closed (aggressive entry); Breakout = the next bar closed above its high (safe entry).
        Stop just under the hammer low; target = nearest resistance. Signals are historical reads on those bars, not order instructions.
      </p>
      {!d ? <p className="text-[10px]" style={{ color: C.faint }}>loading…</p>
        : d.error ? <p className="text-[10px]" style={{ color: C.red }}>{d.error}</p>
        : d.signals.length === 0 ? <p className="text-[10px]" style={{ color: C.faint }}>No hammer in the last 10 sessions{d.last_bar ? ` (last bar ${d.last_bar})` : ""}.</p>
        : (
          <div className="overflow-x-auto">
            <table>
              <thead><tr>{["Hammer bar", "State", "Score", "Entry", "Stop", "Target", "R:R", "RSI", "Vol×"].map((h) => <th key={h}>{h}</th>)}</tr></thead>
              <tbody>
                {d.signals.map((x) => (
                  <tr key={x.hammer_time}>
                    <td>{x.hammer_time.slice(0, 16).replace("T", " ")}</td>
                    <td style={{ color: x.state === "breakout" ? C.green : C.amber, fontWeight: 600 }}>{x.state}</td>
                    <td style={{ fontWeight: 700 }}>{x.score}</td>
                    <td style={{ color: C.amber }}>{inr(x.entry)}</td>
                    <td style={{ color: C.blue }}>{inr(x.stop)}</td>
                    <td style={{ color: C.green }}>{x.target != null ? inr(x.target) : "—"}</td>
                    <td>{x.rr ?? "—"}</td>
                    <td style={{ color: (x.rsi ?? 100) < 30 ? C.green : C.dim }}>{x.rsi ?? "—"}</td>
                    <td style={{ color: (x.vol_ratio ?? 0) >= 1.5 ? C.green : C.dim }}>{x.vol_ratio ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </div>
  );
}

/* ---------- main ---------- */
export default function PositionalEngine() {
  const [data, setData] = useState<ScanResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [sync, setSync] = useState<SyncStatus | null>(null);
  const [tab, setTab] = useState<SubTab>("regime");
  const [view, setView] = useState<"list" | "chart">("list");
  const [stateFilter, setStateFilter] = useState<StateKey>("breakout");
  const [shown, setShown] = useState(12);

  const load = useCallback(async () => {
    setLoading(true);
    try { setData(await getJSON<ScanResponse>("/api/positional/scan")); }
    catch (e) { setData({ error: e instanceof Error ? e.message : String(e) }); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!sync?.running) return;
    const t = setInterval(async () => {
      const s = await getJSON<SyncStatus>("/api/positional/sync").catch(() => null);
      if (s) {
        setSync(s);
        if (!s.running) load();
      }
    }, 2500);
    return () => clearInterval(t);
  }, [sync?.running, load]);

  const startSync = async () => {
    const res = await fetch(`${API_BASE}/api/positional/sync`, { method: "POST" }).then((r) => r.json()).catch(() => null);
    if (res) setSync(res);
  };

  const subTabs: { key: SubTab; label: string; count?: number }[] = [
    { key: "regime", label: "Regime" },
    { key: "sectors", label: "Sector Leaders" },
    ...PATTERN_ORDER.map((k) => ({ key: k as SubTab, label: PATTERN_META[k].label, count: data?.counts?.[k]?.total })),
  ];

  const isPattern = (t: SubTab): t is PatternKey => t !== "regime" && t !== "sectors";
  const rows = isPattern(tab) ? (data?.setups?.[tab] ?? []).filter((s) => s.state === stateFilter) : [];
  const regime = data?.regime;
  const regimeColor = regime?.label === "BULLISH" ? C.green : regime?.label === "BEARISH" ? C.red : C.amber;

  return (
    <div>
      <div className="t-panel p-4 mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-[11px] font-bold uppercase tracking-wider" style={{ color: C.blue }}>Positional Engine</div>
          <p className="text-[10px] mt-0.5 max-w-3xl" style={{ color: C.dim }}>
            Market regime, sector leadership and chart-pattern setups over the whole NSE EQ board, from free end-of-day data.
            Pattern rules are our own definitions and the stats below are measured on our own cached history. Suggest-only — no orders.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {data?.asof && <span className="text-[10px]" style={{ color: C.faint }}>data to {data.asof} · {data.universe} stocks · {data.sessions} sessions</span>}
          {regime && (
            <span className="px-3 py-1 text-[11px] font-bold tracking-wider" style={{ border: `1px solid ${regimeColor}`, color: regimeColor }}>{regime.label}</span>
          )}
          <button onClick={startSync} disabled={sync?.running}
            className="t-btn t-btn-green flex items-center gap-1.5 px-3 py-[6px] text-[10px] font-semibold uppercase tracking-wider disabled:opacity-50">
            <RefreshCw className={`w-3 h-3 ${sync?.running ? "animate-spin" : ""}`} /> {sync?.running ? "Syncing…" : "Sync data"}
          </button>
        </div>
      </div>
      {sync && (sync.running || sync.error) && (
        <p className="text-[10px] mb-3" style={{ color: sync.error ? C.red : C.dim }}>
          {sync.error ? `Sync failed: ${sync.error}` : `Sync: ${sync.message}`}
        </p>
      )}

      <div className="flex flex-wrap gap-1 mb-4" style={{ borderBottom: `1px solid ${C.line}` }}>
        {subTabs.map((t) => (
          <button key={t.key} onClick={() => { setTab(t.key); setShown(12); }}
            className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider"
            style={{ color: tab === t.key ? "#fff" : C.dim, borderBottom: `2px solid ${tab === t.key ? C.blue : "transparent"}` }}>
            {t.label}{t.count != null && <span className="ml-1.5 px-1.5 text-[9px]" style={{ background: "#0d0f14", color: C.dim }}>{t.count}</span>}
          </button>
        ))}
      </div>

      {loading && <p className="text-[11px] py-6 text-center" style={{ color: C.faint }}>Scanning…</p>}
      {data?.error && <div className="t-panel p-4" style={{ borderColor: C.red }}><p className="text-[11px]" style={{ color: C.red }}>{data.error}</p></div>}

      {data && !data.error && tab === "regime" && regime && (
        <div>
          <div className="p-4 mb-4" style={{ background: C.panel, border: `1px solid ${regimeColor}` }}>
            <div className="text-[9px] uppercase tracking-[0.2em]" style={{ color: C.dim }}>Market regime (breadth of {regime.breadth.universe} liquid stocks)</div>
            <div className="text-[22px] font-bold tracking-wider" style={{ color: regimeColor }}>{regime.label}</div>
            <p className="text-[11px] mt-1" style={{ color: C.text }}>{regime.posture}</p>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-4">
            {([
              ["Above SMA50", `${regime.breadth.pct_above_sma50}%`],
              ["Above SMA100", regime.breadth.pct_above_sma100 != null ? `${regime.breadth.pct_above_sma100}%` : "—"],
              ["Advancers / Decliners", `${regime.breadth.advancers} / ${regime.breadth.decliners}`],
              ["60d highs / lows", `${regime.breadth.new_60d_highs} / ${regime.breadth.new_60d_lows}`],
              ["Median 20d return", `${regime.breadth.median_20d_return_pct}%`],
            ] as [string, string][]).map(([l, v]) => (
              <div key={l} className="p-3" style={{ background: C.panel, border: `1px solid ${C.line}` }}>
                <div className="text-[8px] uppercase tracking-wider" style={{ color: C.dim }}>{l}</div>
                <div className="text-[16px] font-bold" style={{ color: C.text }}>{v}</div>
              </div>
            ))}
          </div>
          {data.replay && (
            <div style={{ background: C.panel, border: `1px solid ${C.line}` }}>
              <div className="px-3 py-2 text-[9px] uppercase tracking-wider" style={{ color: C.dim, borderBottom: `1px solid ${C.line}` }}>
                Pattern scorecard — replayed breakouts, next {data.replay.horizon_sessions} sessions (baseline: mean return {data.replay.baseline.mean_fwd_return_pct ?? "—"}%, average move {data.replay.baseline.mean_abs_move_pct ?? "—"}%)
              </div>
              <table>
                <thead><tr>{["Pattern", "Breakouts", "Mean return", "vs baseline", "t-stat", "Move size", `+${data.replay.hit_threshold_pct}% rate`, "Median return"].map((h) => <th key={h}>{h}</th>)}</tr></thead>
                <tbody>
                  {PATTERN_ORDER.filter((k) => data.replay!.patterns[k]).map((k) => {
                    const r = data.replay!.patterns[k];
                    return (
                      <tr key={k}>
                        <td style={{ fontWeight: 600 }}>{PATTERN_META[k].label}</td>
                        <td style={{ color: r.n < 50 ? C.amber : C.text }}>{r.n}{r.n < 50 && " (small)"}</td>
                        <td style={{ fontWeight: 600 }}>{r.mean_fwd_return_pct ?? "—"}%</td>
                        <td style={{ color: (r.t_stat ?? 0) >= 2 ? C.green : (r.t_stat ?? 0) <= -2 ? C.red : C.dim }}>{r.vs_baseline_pct != null ? `${r.vs_baseline_pct > 0 ? "+" : ""}${r.vs_baseline_pct}%` : "—"}</td>
                        <td style={{ color: Math.abs(r.t_stat ?? 0) >= 2 ? C.text : C.faint }}>{r.t_stat ?? "—"}</td>
                        <td style={{ color: C.faint }}>{r.move_ratio != null ? `${r.move_ratio}×` : "—"}</td>
                        <td style={{ color: C.faint }}>{r.hit_rate_pct}%</td>
                        <td>{r.median_fwd_return_pct}%</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <p className="text-[9px] mt-3" style={{ color: C.faint }}>{(data.notes ?? []).join(" ")}</p>
        </div>
      )}

      {data && !data.error && tab === "sectors" && (
        !data.sectors_available ? (
          <p className="text-[11px] py-6 text-center" style={{ color: C.faint }}>Sector list (NIFTY 500 industries) could not be fetched right now — try again shortly.</p>
        ) : (
          <div style={{ background: C.panel, border: `1px solid ${C.line}` }}>
            <table>
              <thead><tr>{["#", "Sector", "Strength", "1M", "3M", "> SMA50", "Stocks", "Setups"].map((h) => <th key={h}>{h}</th>)}</tr></thead>
              <tbody>
                {(data.sectors ?? []).map((s, i) => (
                  <tr key={s.sector}>
                    <td style={{ color: C.dim }}>{i + 1}</td>
                    <td style={{ fontWeight: 600 }}>{s.sector}</td>
                    <td style={{ minWidth: 110 }}>
                      <div className="flex items-center gap-2">
                        <div style={{ width: 70, height: 6, background: "#0d0f14" }}><div style={{ width: `${s.strength}%`, height: 6, background: s.strength >= 66 ? C.green : s.strength >= 33 ? C.amber : C.red }} /></div>
                        <span className="text-[10px]">{s.strength}</span>
                      </div>
                    </td>
                    <td style={{ color: s.ret_1m_pct >= 0 ? C.green : C.red }}>{s.ret_1m_pct}%</td>
                    <td style={{ color: s.ret_3m_pct >= 0 ? C.green : C.red }}>{s.ret_3m_pct}%</td>
                    <td>{s.pct_above_sma50}%</td>
                    <td style={{ color: C.dim }}>{s.stocks}</td>
                    <td style={{ color: s.setups ? C.blue : C.faint }}>{s.setups}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {data && !data.error && isPattern(tab) && (
        <div>
          <p className="text-[10px] mb-3" style={{ color: C.dim }}>{PATTERN_META[tab].blurb}</p>
          {tab === "hammer" && <IntradayHammer />}
          <EvidenceBanner pattern={tab} replay={data.replay} />
          <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
            <div className="flex gap-1">
              {(["breakout", "coiling", "breakdown"] as const)
                .filter((f) => f !== "breakdown" || (data.counts?.[tab]?.breakdown ?? 0) > 0)
                .map((f) => {
                  const c = data.counts?.[tab];
                  const n = f === "breakout" ? c?.breakout ?? 0 : f === "breakdown" ? c?.breakdown ?? 0 : (c?.total ?? 0) - (c?.breakout ?? 0) - (c?.breakdown ?? 0);
                  return (
                    <button key={f} onClick={() => { setStateFilter(f); setShown(12); }}
                      className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider"
                      style={{ background: stateFilter === f ? "#fff" : C.panel, color: stateFilter === f ? "#0e1117" : C.dim, border: `1px solid ${C.line}` }}>
                      {f === "breakdown" ? "Breakdown / bearish" : f} ({n})
                    </button>
                  );
                })}
            </div>
            <div className="flex gap-1">
              {(["list", "chart"] as const).map((v) => (
                <button key={v} onClick={() => setView(v)} className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider"
                  style={{ background: view === v ? "#fff" : C.panel, color: view === v ? "#0e1117" : C.dim, border: `1px solid ${C.line}` }}>
                  {v} view
                </button>
              ))}
            </div>
          </div>

          {rows.length === 0 ? (
            <p className="text-[11px] py-6 text-center" style={{ color: C.faint }}>No {stateFilter} setups right now.</p>
          ) : view === "list" ? (
            <div className="overflow-x-auto" style={{ background: C.panel, border: `1px solid ${C.line}` }}>
              <table>
                <thead><tr>{["Symbol", "Score", "RS", "Close", "Chg", "Entry", "Stop", "Target", "R:R", "Vol×", "Detail"].map((h) => <th key={h}>{h}</th>)}</tr></thead>
                <tbody>
                  {rows.map((s) => (
                    <tr key={s.symbol}>
                      <td style={{ fontWeight: 600 }}>{s.symbol}</td>
                      <td style={{ fontWeight: 700, color: s.score >= 8 ? C.green : s.score >= 6 ? C.amber : C.dim }}>{s.score}</td>
                      <td style={{ color: C.dim }}>{s.rs_pct ?? "—"}</td>
                      <td>{inr(s.close)}</td>
                      <td style={{ color: (s.change_pct ?? 0) >= 0 ? C.green : C.red }}>{s.change_pct != null ? `${s.change_pct}%` : "—"}</td>
                      <td style={{ color: C.amber }}>{inr(s.entry)}</td>
                      <td style={{ color: C.blue }}>{inr(s.stop)}</td>
                      <td style={{ color: C.green }}>{s.target != null ? inr(s.target) : "—"}</td>
                      <td style={{ color: (s.rr ?? 0) >= 2 ? C.green : C.dim }}>{s.rr ?? "—"}</td>
                      <td style={{ color: (s.vol_ratio ?? 0) >= 1.5 ? C.green : C.dim }}>{s.vol_ratio ?? "—"}</td>
                      <td className="text-[9px]" style={{ color: C.faint }}>
                        {s.pole_gain_pct != null && `pole +${s.pole_gain_pct}% · retrace ${s.retrace_pct}% · ${s.flag_bars} bars`}
                        {s.depth_pct != null && `box ${s.depth_pct}% deep · ${s.touches} touches`}
                        {s.contractions_pct && `ranges ${s.contractions_pct.join(" → ")}%`}
                        {s.sessions_listed != null && `${s.sessions_listed} sessions listed`}
                        {s.cup_depth_pct != null && `cup ${s.cup_depth_pct}% deep over ${s.cup_bars} bars · handle ${s.handle_bars} bars${s.handle_vol_up ? " · handle volume up" : ""}`}
                        {s.variant && s.prior_trend_pct != null && `${s.variant} · prior trend ${s.prior_trend_pct}% · ${s.touches} touches${s.vol_dry_up ? " · volume dried up" : ""}`}
                        {s.rsi_low1 != null && `RSI ${s.rsi_low1} → ${s.rsi_low2} while price made a lower low`}
                        {s.rsi_high1 != null && `RSI ${s.rsi_high1} → ${s.rsi_high2} while price made a higher high`}
                        {s.rsi != null && s.near_support_pct != null && `RSI ${s.rsi} · ${s.near_support_pct}% above support`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                {rows.slice(0, shown).map((s) => <SetupCard key={s.symbol} s={s} />)}
              </div>
              {rows.length > shown && (
                <button onClick={() => setShown((n) => n + 12)} className="mt-3 px-4 py-[6px] text-[10px] font-semibold uppercase tracking-wider"
                  style={{ background: C.panel, border: `1px solid ${C.line}`, color: C.text }}>
                  Show more ({rows.length - shown} left)
                </button>
              )}
            </>
          )}
          <p className="text-[9px] mt-3" style={{ color: C.faint }}>
            Score (1–10) counts how many quality tests a setup passes — it is not a probability. Entry is the pattern trigger level, stop the pattern low; check both against a live quote.
          </p>
        </div>
      )}
    </div>
  );
}
