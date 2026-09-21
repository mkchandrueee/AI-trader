"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "@/lib/api";
import { RefreshCw } from "lucide-react";

type PatternKey = "orb" | "vwap" | "level" | "box" | "hammer";
type StateKey = "breakout" | "coiling" | "breakdown";
type SubTab = "regime" | "sectors" | PatternKey;

interface Setup {
  symbol: string;
  pattern: PatternKey;
  state: StateKey;
  score: number;
  entry: number;
  stop: number;
  target: number;
  close: number;
  risk_pct: number | null;
  rr: number | null;
  box: [number, number, number] | null; // [bars_back, top, bottom]
  vol_ratio?: number | null;
  day_change_pct?: number | null;
  above_vwap?: boolean;
  or_high?: number;
  or_low?: number;
  extended_pct?: number;
  vwap?: number;
  bars_below?: number;
  bars_above?: number;
  level?: string;
  level_price?: number;
  depth_pct?: number;
  vol_dry_up?: boolean;
  rsi?: number | null;
  near_support_pct?: number | null;
}

interface Sector { sector: string; stocks: number; day_change_pct: number; pct_above_vwap: number; setups: number; strength: number }

interface IndexRow { close: number; vwap: number; above_vwap: boolean; day_change_pct: number | null; bar_time: string }

interface ReplayRow {
  n: number;
  days: number;
  win_rate_pct: number | null;
  avg_r: number | null;
  avg_pnl_pct: number | null;
  target_first_pct: number | null;
  stop_first_pct: number | null;
  hit_rate_pct: number | null;
  hit_lift: number | null;
  avg_gross_pct?: number | null;
  gap_past_target?: number;
  gap_past_stop?: number;
}
interface Replay {
  sessions: number;
  forward_bars: number;
  hit_threshold_pct: number;
  cost_pct: number;
  sim_max_bars: number;
  baseline: { long_hit_rate_pct: number | null; short_hit_rate_pct: number | null; bars: number };
  patterns: Record<PatternKey, ReplayRow>;
  asof: string;
}

interface Recon { ok: boolean | null; bar?: string; broker_close?: number; db_close?: number; diff_pct?: number; tolerance_pct?: number; note?: string }

interface SyncInfo {
  running: boolean;
  last_sync: string | null;
  next_due: string | null;
  duration_s: number | null;
  ok: number;
  total: number;
  failed: string[];
  stale?: string[];
  sources: Record<string, number>;
  errors: string[];
  mode: string;
  message: string;
  age_s: number | null;
  market_open: boolean;
  reconcile?: Recon | null;
}

interface ScanResponse {
  asof_bar?: string | null;
  universe?: number;
  regime?: {
    label: "BULLISH" | "BEARISH" | "CHOPPY" | "UNKNOWN";
    posture?: string;
    index?: IndexRow | null;
    breadth: Record<string, number | null>;
  };
  sectors?: Sector[];
  setups?: Record<PatternKey, Setup[]>;
  counts?: Record<PatternKey, { total: number; breakout: number; breakdown: number }>;
  movers?: (IndexRow & { symbol: string })[];
  replay?: Replay | null;
  sync?: SyncInfo;
  error?: string;
}

interface ChartData {
  times: string[];
  open: number[]; high: number[]; low: number[]; close: number[]; volume: number[]; vwap: number[];
  session_starts: number[];
}

const META: Record<PatternKey, { label: string; blurb: string }> = {
  orb: { label: "Opening Range", blurb: "The 09:15–09:30 high/low. A completed 5-minute close beyond it (within the last hour) is the trigger; target = one opening-range height beyond the break, stop = the other side of the range." },
  vwap: { label: "VWAP", blurb: "Most of the last 8 bars on one side of the day's VWAP, then a decisive cross back over it in the last 3 bars. Stop = the 6-bar swing, target = 1.5× the risk. VWAP here is built from completed bars." },
  level: { label: "Prev-Day Levels", blurb: "A completed close through yesterday's high (long) or low (short) within the last 30 minutes. Stop = the 6-bar swing or 0.4% back through the level, target = 1.5× the risk." },
  box: { label: "Box Break", blurb: "A tight one-hour consolidation (under 0.6% deep, flat top touched twice). A close outside it is the trigger; target = the box height beyond the break." },
  hammer: { label: "Hammer 30m", blurb: "ChartBank's intraday strategy on 30-minute candles: a T-shaped candle after a decline, near support. Coiling = the hammer just completed (aggressive); Breakout = the next 30-minute candle closed above its high (safe). SL just under the hammer low." },
};
const ORDER: PatternKey[] = ["orb", "vwap", "level", "box", "hammer"];

const C = { green: "#00e87b", red: "#ff3e3e", amber: "#e8c300", blue: "#4da6ff", dim: "#5a6270", faint: "#3d4450", text: "#c8cdd5", panel: "#181c24", line: "#252a33" };

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!(res.headers.get("content-type") || "").includes("json")) {
    throw new Error(res.status === 404
      ? "The backend is running an older build without the Intraday routes — restart it to load them."
      : `Unexpected response from the backend (HTTP ${res.status}).`);
  }
  return res.json();
}

const inr = (n: number) => `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
const hhmm = (iso?: string | null) => (iso ? iso.slice(11, 16) : "—");

/* ---------- 5-minute candlestick chart (plain SVG) ---------- */
function IntradayChart({ symbol, setup }: { symbol: string; setup: Setup }) {
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
        getJSON<ChartData & { error?: string }>(`/api/intraday/chart?symbol=${encodeURIComponent(symbol)}&sessions=2`)
          .then((d) => { if (live) { if (d.error) setErr(true); else setData(d); } })
          .catch(() => live && setErr(true));
      }
    });
    io.observe(el);
    return () => { live = false; io.disconnect(); };
  }, [symbol]);

  const W = 560, H = 230, PAD_R = 62, PAD_T = 8, VOL_H = 30, PRICE_H = H - VOL_H - PAD_T - 4;

  const g = useMemo(() => {
    if (!data) return null;
    const n = data.close.length;
    const step = (W - PAD_R) / n;
    const showTarget = setup.target > Math.min(...data.low) * 0.97 && setup.target < Math.max(...data.high) * 1.03;
    const lows = [...data.low, setup.stop];
    const highs = [...data.high, setup.entry, ...(showTarget ? [setup.target] : [])];
    const min = Math.min(...lows), max = Math.max(...highs);
    const pad = (max - min) * 0.05 || 1;
    const lo = min - pad, hi = max + pad;
    const y = (p: number) => PAD_T + ((hi - p) / (hi - lo)) * PRICE_H;
    const x = (i: number) => i * step + step / 2;
    const vmax = Math.max(...data.volume, 1);
    return { n, step, y, x, vmax, showTarget };
  }, [data, setup.entry, setup.stop, setup.target, PRICE_H]);

  return (
    <div ref={ref} style={{ background: "#0e1117", border: `1px solid ${C.line}` }}>
      {!data || !g ? (
        <div className="flex items-center justify-center text-[10px]" style={{ height: H, color: C.faint }}>{err ? "chart unavailable" : "loading chart…"}</div>
      ) : (
        <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block" }}>
          {data.session_starts.slice(1).map((i) => (
            <line key={i} x1={g.x(i) - g.step / 2} x2={g.x(i) - g.step / 2} y1={PAD_T} y2={H} stroke={C.line} strokeDasharray="2 3" />
          ))}
          {setup.box && (
            <rect x={g.x(Math.max(0, g.n - 1 - setup.box[0]))} width={g.x(g.n - 1) - g.x(Math.max(0, g.n - 1 - setup.box[0]))}
              y={g.y(setup.box[1])} height={Math.max(1, g.y(setup.box[2]) - g.y(setup.box[1]))}
              fill="#4da6ff" opacity={0.12} stroke="#4da6ff" strokeOpacity={0.4} strokeDasharray="3 3" />
          )}
          {data.volume.map((v, i) => (
            <rect key={`v${i}`} x={g.x(i) - g.step * 0.35} width={g.step * 0.7} y={H - 2 - (v / g.vmax) * VOL_H} height={(v / g.vmax) * VOL_H}
              fill={data.close[i] >= data.open[i] ? C.green : C.red} opacity={0.3} />
          ))}
          {data.close.map((_, i) => {
            const up = data.close[i] >= data.open[i];
            const col = up ? C.green : C.red;
            const top = g.y(Math.max(data.open[i], data.close[i]));
            const bot = g.y(Math.min(data.open[i], data.close[i]));
            return (
              <g key={i}>
                <line x1={g.x(i)} x2={g.x(i)} y1={g.y(data.high[i])} y2={g.y(data.low[i])} stroke={col} strokeWidth={0.8} />
                <rect x={g.x(i) - g.step * 0.34} width={g.step * 0.68} y={top} height={Math.max(0.8, bot - top)} fill={col} />
              </g>
            );
          })}
          <polyline points={data.vwap.map((p, i) => `${g.x(i).toFixed(1)},${g.y(p).toFixed(1)}`).join(" ")} fill="none" stroke={C.amber} strokeWidth={1} opacity={0.85} />
          {([[setup.entry, "Entry", C.amber], [setup.stop, "Stop", C.blue], ...(g.showTarget ? [[setup.target, "Target", C.green]] : [])] as [number, string, string][]).map(([p, label, col]) => (
            <g key={label}>
              <line x1={0} x2={W - PAD_R} y1={g.y(p)} y2={g.y(p)} stroke={col} strokeDasharray="4 3" strokeWidth={1} />
              <rect x={W - PAD_R + 2} y={g.y(p) - 7} width={PAD_R - 3} height={14} fill={col} />
              <text x={W - PAD_R + 5} y={g.y(p) + 3} fontSize={8} fill="#0e1117" fontWeight={700}>{label} {p.toFixed(1)}</text>
            </g>
          ))}
          <g>
            <rect x={W - PAD_R + 2} y={g.y(setup.close) - 7} width={PAD_R - 3} height={14} fill={C.dim} />
            <text x={W - PAD_R + 5} y={g.y(setup.close) + 3} fontSize={8} fill="#0e1117" fontWeight={700}>{setup.close.toFixed(1)}</text>
          </g>
        </svg>
      )}
    </div>
  );
}

function detail(s: Setup): string {
  if (s.pattern === "orb") return `range ${s.or_low}–${s.or_high}${s.extended_pct != null && s.state !== "coiling" ? ` · ${s.extended_pct > 0 ? "+" : ""}${s.extended_pct}% past trigger` : ""}`;
  if (s.pattern === "vwap") return `VWAP ${s.vwap} · ${s.bars_below ?? s.bars_above} of last 8 bars on the other side`;
  if (s.pattern === "level") return `${s.level} ${s.level_price}${s.extended_pct != null && s.state !== "coiling" ? ` · ${s.extended_pct > 0 ? "+" : ""}${s.extended_pct}% past` : ""}`;
  if (s.pattern === "box") return `box ${s.depth_pct}% deep${s.vol_dry_up ? " · volume dried up" : ""}`;
  return `RSI ${s.rsi ?? "—"} · ${s.near_support_pct ?? "—"}% above support · 30-min`;
}

function SetupCard({ s }: { s: Setup }) {
  const col = s.state === "breakout" ? C.green : s.state === "breakdown" ? C.red : C.amber;
  return (
    <div className="p-3" style={{ background: C.panel, border: `1px solid ${C.line}` }}>
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-2">
          <b className="text-[13px]" style={{ color: "#fff" }}>{s.symbol}</b>
          <span className="px-1.5 py-[1px] text-[8px] uppercase tracking-wider" style={{ border: `1px solid ${col}66`, color: col }}>{s.state === "breakdown" ? "short" : s.state}</span>
        </div>
        <span className="px-1.5 py-[1px] text-[10px] font-bold" style={{ background: "#0d0f14", border: `1px solid ${C.line}`, color: C.text }}>{s.score}</span>
      </div>
      <div className="text-[10px] mb-2" style={{ color: C.dim }}>
        {inr(s.close)} {s.day_change_pct != null && <span style={{ color: s.day_change_pct >= 0 ? C.green : C.red }}>{s.day_change_pct >= 0 ? "+" : ""}{s.day_change_pct}%</span>}
        {s.vol_ratio != null && <> · vol {s.vol_ratio}×</>}{s.rr != null && <> · R:R {s.rr}</>}{s.risk_pct != null && <> · risk {s.risk_pct}%</>}
      </div>
      <IntradayChart symbol={s.symbol} setup={s} />
      <div className="text-[9px] mt-1" style={{ color: C.faint }}>{detail(s)}</div>
    </div>
  );
}

function Evidence({ pattern, replay }: { pattern: PatternKey; replay?: Replay | null }) {
  const r = replay?.patterns?.[pattern];
  if (!replay || !r) {
    return <div className="p-3 mb-4 text-[10px]" style={{ background: C.panel, border: `1px solid ${C.line}`, color: C.dim }}>Scorecard not ready yet — it is measured in the background once enough sessions are loaded.</div>;
  }
  const small = r.n < 30;
  const edge = !small && (r.avg_pnl_pct ?? 0) > 0 && (r.avg_r ?? 0) > 0;
  const col = small ? C.amber : edge ? C.green : C.red;
  return (
    <div className="p-3 mb-4 text-[10px] leading-relaxed" style={{ background: "#0d1a14", border: `1px solid ${small ? "#3a3a1a" : edge ? "#1a3a2a" : "#3a1a1a"}`, color: C.text }}>
      <b style={{ color: col }}>Measured on our own data, not assumed.</b> Replaying this exact rule on the last {replay.sessions} sessions of the same stocks: {r.n} confirmed setups
      over {r.days} session-days, entered at the next bar&apos;s open with the setup&apos;s own stop and target (held at most {replay.sim_max_bars * 5} min, {replay.cost_pct}% costs assumed):
      win rate {r.win_rate_pct}%, average {r.avg_r}R, target reached before stop {r.target_first_pct}% / stop first {r.stop_first_pct}%.
      {" "}Average P&amp;L per trade: <b style={{ color: (r.avg_gross_pct ?? 0) > 0 ? C.green : C.red }}>{r.avg_gross_pct ?? "—"}%</b> before costs,{" "}
      <b style={{ color: col }}>{r.avg_pnl_pct}%</b> after.
      {(r.gap_past_target ?? 0) + (r.gap_past_stop ?? 0) > 0 && <> {r.gap_past_stop} gapped through the stop and {r.gap_past_target} past the target at the entry bar (counted as costs only).</>}
      {small && <b style={{ color: C.amber }}> Small sample — do not read anything into this yet.</b>}
      {!small && !edge && (r.avg_gross_pct ?? 0) <= 0.02 && <span style={{ color: C.red }}> No directional edge even before costs — a win rate here says nothing about profit.</span>}
      {!small && !edge && (r.avg_gross_pct ?? 0) > 0.02 && <span style={{ color: C.amber }}> There is a small pre-cost edge, but costs consume it.</span>}
      {r.hit_lift != null && r.hit_lift >= 1.2 && !edge && (
        <span style={{ color: C.faint }}> ({r.hit_rate_pct}% saw a {replay.hit_threshold_pct}% move in the trade&apos;s direction vs a random bar&apos;s baseline, {r.hit_lift}× — that counts big moves, so it rises with volatility even when direction is a coin-flip.)</span>
      )}
    </div>
  );
}

/* ---------- main ---------- */
export default function IntradayEngine() {
  const [data, setData] = useState<ScanResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<SubTab>("regime");
  const [view, setView] = useState<"list" | "chart">("list");
  const [stateFilter, setStateFilter] = useState<StateKey>("breakout");
  const [shown, setShown] = useState(10);
  const [fetchedAt, setFetchedAt] = useState(0);
  const [now, setNow] = useState(0);

  const load = useCallback(async () => {
    try {
      setData(await getJSON<ScanResponse>("/api/intraday/scan"));
      setFetchedAt(Date.now());
    } catch (e) {
      setData({ error: e instanceof Error ? e.message : String(e) });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  useEffect(() => { setNow(Date.now()); const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);

  const syncNow = async () => {
    await fetch(`${API_BASE}/api/intraday/sync`, { method: "POST" }).catch(() => null);
    setTimeout(load, 1500);
  };

  const sync = data?.sync;
  const age = sync?.age_s != null ? sync.age_s + Math.max(0, Math.round((now - fetchedAt) / 1000)) : null;
  const stale = sync?.stale?.length ?? 0;
  const recon = sync?.reconcile;
  let dot = C.dim, label = "connecting";
  if (sync) {
    if (sync.running) { dot = C.blue; label = "SYNCING"; }
    else if (!sync.ok) { dot = C.red; label = "NO DATA"; }
    else if (!sync.market_open) { dot = C.dim; label = "MARKET CLOSED · LAST SESSION"; }
    else if ((age ?? 0) > 420 || stale > 3 || recon?.ok === false) { dot = C.amber; label = "LIVE · CHECK SYNC"; }
    else { dot = C.green; label = "LIVE"; }
  }

  const regime = data?.regime;
  const regimeColor = regime?.label === "BULLISH" ? C.green : regime?.label === "BEARISH" ? C.red : C.amber;
  const isPattern = (t: SubTab): t is PatternKey => t !== "regime" && t !== "sectors";
  const rows = isPattern(tab) ? (data?.setups?.[tab] ?? []).filter((s) => s.state === stateFilter) : [];

  const tabs: { key: SubTab; label: string; count?: number }[] = [
    { key: "regime", label: "Regime" },
    { key: "sectors", label: "Sector Leaders" },
    ...ORDER.map((k) => ({ key: k as SubTab, label: META[k].label, count: data?.counts?.[k]?.total })),
  ];

  return (
    <div>
      <div className="t-panel p-4 mb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-[11px] font-bold uppercase tracking-wider" style={{ color: C.blue }}>Intraday Engine</div>
            <p className="text-[10px] mt-0.5 max-w-3xl" style={{ color: C.dim }}>
              NIFTY 50 stocks + NIFTY futures on <b>completed 5-minute bars</b> pulled from the brokers (mStock first, AngelOne fallback) and refreshed a few seconds after every bar closes.
              A candle still forming is never analysed. Rules are our own plus ChartBank&apos;s 30-minute hammer; every scorecard is measured on our own history. Suggest-only — no orders.
            </p>
          </div>
          <div className="flex items-center gap-3">
            {regime && <span className="px-3 py-1 text-[11px] font-bold tracking-wider" style={{ border: `1px solid ${regimeColor}`, color: regimeColor }}>{regime.label}</span>}
            <button onClick={syncNow} disabled={sync?.running}
              className="t-btn t-btn-green flex items-center gap-1.5 px-3 py-[6px] text-[10px] font-semibold uppercase tracking-wider disabled:opacity-50">
              <RefreshCw className={`w-3 h-3 ${sync?.running ? "animate-spin" : ""}`} /> {sync?.running ? "Syncing…" : "Sync now"}
            </button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-3 text-[10px]" style={{ color: C.dim }}>
          <span className="flex items-center gap-1.5 font-bold tracking-wider" style={{ color: dot }}>
            <span style={{ width: 8, height: 8, borderRadius: 8, background: dot, display: "inline-block" }} />{label}
          </span>
          {data?.asof_bar && <span>as of the <b style={{ color: C.text }}>{hhmm(data.asof_bar)}</b> bar</span>}
          {age != null && <span>synced <b style={{ color: age > 420 && sync?.market_open ? C.amber : C.text }}>{age}s</b> ago{sync?.duration_s != null && ` (took ${sync.duration_s}s)`}</span>}
          {sync && <span><b style={{ color: sync.ok === sync.total ? C.text : C.amber }}>{sync.ok}/{sync.total}</b> symbols</span>}
          {sync && Object.keys(sync.sources).length > 0 && <span>source: {Object.entries(sync.sources).map(([k, v]) => `${k} ${v}`).join(" · ")}</span>}
          {stale > 0 && <span style={{ color: C.amber }}>{stale} stale</span>}
          {sync?.next_due && sync.market_open && <span>next refresh ≈ {hhmm(sync.next_due)}</span>}
          {recon && recon.ok != null && (
            <span style={{ color: recon.ok ? C.green : C.red }}>
              NIFTY-I vs our ticks: {recon.diff_pct != null && recon.diff_pct > 0 ? "+" : ""}{recon.diff_pct}% {recon.ok ? "✓" : `✕ (tolerance ${recon.tolerance_pct}%)`}
            </span>
          )}
          {recon && recon.ok == null && recon.note && <span style={{ color: C.faint }}>{recon.note}</span>}
        </div>
        {sync && sync.errors.length > 0 && (
          <p className="text-[9px] mt-2" style={{ color: sync.ok ? C.faint : C.red }}>{sync.errors.slice(0, 3).join(" · ")}</p>
        )}
      </div>

      <div className="flex flex-wrap gap-1 mb-4" style={{ borderBottom: `1px solid ${C.line}` }}>
        {tabs.map((t) => (
          <button key={t.key} onClick={() => { setTab(t.key); setShown(10); }} className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wider"
            style={{ color: tab === t.key ? "#fff" : C.dim, borderBottom: `2px solid ${tab === t.key ? C.blue : "transparent"}` }}>
            {t.label}{t.count != null && <span className="ml-1.5 px-1.5 text-[9px]" style={{ background: "#0d0f14", color: C.dim }}>{t.count}</span>}
          </button>
        ))}
      </div>

      {loading && <p className="text-[11px] py-6 text-center" style={{ color: C.faint }}>Loading…</p>}
      {data?.error && <div className="t-panel p-4" style={{ borderColor: C.amber }}><p className="text-[11px]" style={{ color: C.amber }}>{data.error}</p></div>}

      {data && !data.error && tab === "regime" && regime && (
        <div>
          <div className="p-4 mb-4" style={{ background: C.panel, border: `1px solid ${regimeColor}` }}>
            <div className="text-[9px] uppercase tracking-[0.2em]" style={{ color: C.dim }}>Intraday regime ({regime.breadth.universe} stocks + NIFTY futures)</div>
            <div className="text-[22px] font-bold tracking-wider" style={{ color: regimeColor }}>{regime.label}</div>
            <p className="text-[11px] mt-1" style={{ color: C.text }}>{regime.posture}</p>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-4">
            {([
              ["NIFTY futures", regime.index ? `${regime.index.above_vwap ? "above" : "below"} VWAP · ${regime.index.day_change_pct ?? "—"}%` : "—"],
              ["Stocks above VWAP", `${regime.breadth.pct_above_vwap}%`],
              ["Advancers / decliners", `${regime.breadth.advancers} / ${regime.breadth.decliners}`],
              ["Median day change", `${regime.breadth.median_day_change_pct}%`],
              ["ORB breakouts / breakdowns", `${regime.breadth.orb_breakouts} / ${regime.breadth.orb_breakdowns}`],
            ] as [string, string][]).map(([l, v]) => (
              <div key={l} className="p-3" style={{ background: C.panel, border: `1px solid ${C.line}` }}>
                <div className="text-[8px] uppercase tracking-wider" style={{ color: C.dim }}>{l}</div>
                <div className="text-[14px] font-bold" style={{ color: C.text }}>{v}</div>
              </div>
            ))}
          </div>
          {data.replay && (
            <div className="mb-4 overflow-x-auto" style={{ background: C.panel, border: `1px solid ${C.line}` }}>
              <div className="px-3 py-2 text-[9px] uppercase tracking-wider" style={{ color: C.dim, borderBottom: `1px solid ${C.line}` }}>
                Pattern scorecard — last {data.replay.sessions} sessions, next-bar entry, own stop &amp; target, {data.replay.cost_pct}% costs
              </div>
              <table>
                <thead><tr>{["Pattern", "Trades", "Win %", "Avg R", "Target first", "Stop first", "Before costs", "After costs"].map((h) => <th key={h}>{h}</th>)}</tr></thead>
                <tbody>
                  {ORDER.filter((k) => data.replay!.patterns[k]).map((k) => {
                    const r = data.replay!.patterns[k];
                    return (
                      <tr key={k}>
                        <td style={{ fontWeight: 600 }}>{META[k].label}</td>
                        <td style={{ color: r.n < 30 ? C.amber : C.text }}>{r.n}{r.n < 30 && " (small)"}</td>
                        <td>{r.win_rate_pct ?? "—"}%</td>
                        <td style={{ color: (r.avg_r ?? 0) > 0 ? C.green : C.red, fontWeight: 600 }}>{r.avg_r ?? "—"}</td>
                        <td>{r.target_first_pct ?? "—"}%</td>
                        <td>{r.stop_first_pct ?? "—"}%</td>
                        <td style={{ color: (r.avg_gross_pct ?? 0) > 0 ? C.green : C.red }}>{r.avg_gross_pct ?? "—"}%</td>
                        <td style={{ color: (r.avg_pnl_pct ?? 0) > 0 ? C.green : C.red, fontWeight: 600 }}>{r.avg_pnl_pct ?? "—"}%</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {(data.movers?.length ?? 0) > 0 && (
            <div style={{ background: C.panel, border: `1px solid ${C.line}` }}>
              <div className="px-3 py-2 text-[9px] uppercase tracking-wider" style={{ color: C.dim, borderBottom: `1px solid ${C.line}` }}>Biggest movers vs previous close</div>
              <table>
                <thead><tr>{["Symbol", "Close", "Day", "VWAP"].map((h) => <th key={h}>{h}</th>)}</tr></thead>
                <tbody>
                  {data.movers!.map((m) => (
                    <tr key={m.symbol}>
                      <td style={{ fontWeight: 600 }}>{m.symbol}</td>
                      <td>{inr(m.close)}</td>
                      <td style={{ color: (m.day_change_pct ?? 0) >= 0 ? C.green : C.red }}>{m.day_change_pct}%</td>
                      <td style={{ color: m.above_vwap ? C.green : C.red }}>{m.above_vwap ? "above" : "below"} {inr(m.vwap)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {data && !data.error && tab === "sectors" && (
        (data.sectors?.length ?? 0) === 0 ? (
          <p className="text-[11px] py-6 text-center" style={{ color: C.faint }}>Sector list not available yet (NIFTY 500 industry mapping could not be fetched).</p>
        ) : (
          <div style={{ background: C.panel, border: `1px solid ${C.line}` }}>
            <table>
              <thead><tr>{["#", "Sector", "Strength", "Day change", "Above VWAP", "Stocks", "Setups"].map((h) => <th key={h}>{h}</th>)}</tr></thead>
              <tbody>
                {data.sectors!.map((s, i) => (
                  <tr key={s.sector}>
                    <td style={{ color: C.dim }}>{i + 1}</td>
                    <td style={{ fontWeight: 600 }}>{s.sector}</td>
                    <td>
                      <div className="flex items-center gap-2">
                        <div style={{ width: 70, height: 6, background: "#0d0f14" }}><div style={{ width: `${s.strength}%`, height: 6, background: s.strength >= 66 ? C.green : s.strength >= 33 ? C.amber : C.red }} /></div>
                        <span className="text-[10px]">{s.strength}</span>
                      </div>
                    </td>
                    <td style={{ color: s.day_change_pct >= 0 ? C.green : C.red }}>{s.day_change_pct}%</td>
                    <td>{s.pct_above_vwap}%</td>
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
          <p className="text-[10px] mb-3" style={{ color: C.dim }}>{META[tab].blurb}</p>
          <Evidence pattern={tab} replay={data.replay} />
          <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
            <div className="flex gap-1">
              {(["breakout", "coiling", "breakdown"] as const).map((f) => {
                const c = data.counts?.[tab];
                const n = f === "breakout" ? c?.breakout ?? 0 : f === "breakdown" ? c?.breakdown ?? 0 : (c?.total ?? 0) - (c?.breakout ?? 0) - (c?.breakdown ?? 0);
                return (
                  <button key={f} onClick={() => { setStateFilter(f); setShown(10); }} className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider"
                    style={{ background: stateFilter === f ? "#fff" : C.panel, color: stateFilter === f ? "#0e1117" : C.dim, border: `1px solid ${C.line}` }}>
                    {f === "breakout" ? "Long breakout" : f === "breakdown" ? "Short breakdown" : "Coiling"} ({n})
                  </button>
                );
              })}
            </div>
            <div className="flex gap-1">
              {(["list", "chart"] as const).map((v) => (
                <button key={v} onClick={() => setView(v)} className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider"
                  style={{ background: view === v ? "#fff" : C.panel, color: view === v ? "#0e1117" : C.dim, border: `1px solid ${C.line}` }}>{v} view</button>
              ))}
            </div>
          </div>

          {rows.length === 0 ? (
            <p className="text-[11px] py-6 text-center" style={{ color: C.faint }}>No {stateFilter === "breakdown" ? "short" : stateFilter} setups on the {hhmm(data.asof_bar)} bar.</p>
          ) : view === "list" ? (
            <div className="overflow-x-auto" style={{ background: C.panel, border: `1px solid ${C.line}` }}>
              <table>
                <thead><tr>{["Symbol", "Score", "Day", "Close", "Entry", "Stop", "Target", "R:R", "Risk", "Vol×", "Detail"].map((h) => <th key={h}>{h}</th>)}</tr></thead>
                <tbody>
                  {rows.map((s) => (
                    <tr key={s.symbol}>
                      <td style={{ fontWeight: 600 }}>{s.symbol}</td>
                      <td style={{ fontWeight: 700, color: s.score >= 8 ? C.green : s.score >= 6 ? C.amber : C.dim }}>{s.score}</td>
                      <td style={{ color: (s.day_change_pct ?? 0) >= 0 ? C.green : C.red }}>{s.day_change_pct != null ? `${s.day_change_pct}%` : "—"}</td>
                      <td>{inr(s.close)}</td>
                      <td style={{ color: C.amber }}>{inr(s.entry)}</td>
                      <td style={{ color: C.blue }}>{inr(s.stop)}</td>
                      <td style={{ color: C.green }}>{inr(s.target)}</td>
                      <td style={{ color: (s.rr ?? 0) >= 1.5 ? C.green : C.dim }}>{s.rr ?? "—"}</td>
                      <td style={{ color: C.dim }}>{s.risk_pct != null ? `${s.risk_pct}%` : "—"}</td>
                      <td style={{ color: (s.vol_ratio ?? 0) >= 1.5 ? C.green : C.dim }}>{s.vol_ratio ?? "—"}</td>
                      <td className="text-[9px]" style={{ color: C.faint }}>{detail(s)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">{rows.slice(0, shown).map((s) => <SetupCard key={s.symbol} s={s} />)}</div>
              {rows.length > shown && (
                <button onClick={() => setShown((n) => n + 10)} className="mt-3 px-4 py-[6px] text-[10px] font-semibold uppercase tracking-wider" style={{ background: C.panel, border: `1px solid ${C.line}`, color: C.text }}>
                  Show more ({rows.length - shown} left)
                </button>
              )}
            </>
          )}
          <p className="text-[9px] mt-3" style={{ color: C.faint }}>
            Score (1–10) counts how many quality checks a setup passes — it is not a probability. Entry is the trigger price on a completed bar: if the setup is already extended past it, the entry has moved. Check a live quote before acting.
          </p>
        </div>
      )}
    </div>
  );
}
