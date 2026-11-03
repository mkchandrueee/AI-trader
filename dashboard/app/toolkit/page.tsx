"use client";

import { useCallback, useEffect, useState } from "react";
import Sidebar from "@/components/Sidebar";
import { API_BASE } from "@/lib/api";
import {
  Wrench, ScanSearch, Newspaper, Crosshair, Target, History, ShieldCheck,
  Calculator, Receipt, BellRing, FileText, Gavel, Play, AlertTriangle,
} from "lucide-react";

const GREEN = "#00e87b";
const RED = "#ff3e3e";
const AMBER = "#e8a200";
const BLUE = "#4aa3ff";
const MUTED = "#5a6270";
const DIM = "#3d4450";
const TEXT = "#c8cdd5";
const BORDER = "#252a33";
const INK = "#0e1117";

type Field = { key: string; label: string; type: "text" | "number"; def?: string | number; hint?: string };

interface Tool {
  id: string;
  title: string;
  icon: typeof Wrench;
  blurb: string;
  method: "GET" | "POST";
  path: string;
  fields: Field[];
  cost?: string;       // shown as a warning chip when the tool spends something
}

const TOOLS: Tool[] = [
  {
    id: "scanner", title: "Scanner", icon: ScanSearch, method: "GET", path: "/api/toolkit/scanner",
    blurb: "Opening-range, VWAP, level, box and hammer setups across the cached NSE board.",
    fields: [
      { key: "pattern", label: "Pattern", type: "text", hint: "orb | vwap | level | box | hammer" },
      { key: "state", label: "State", type: "text", hint: "breakout | breakdown | coiling" },
      { key: "min_score", label: "Min score", type: "number", def: 0 },
    ],
  },
  {
    id: "news", title: "News", icon: Newspaper, method: "GET", path: "/api/toolkit/news",
    blurb: "Indian headlines with keyword sentiment: ET, Livemint, RBI, SEBI, Google News India.",
    fields: [
      { key: "hours", label: "Hours", type: "number", def: 24 },
      { key: "symbol", label: "Symbol", type: "text", hint: "optional, e.g. RELIANCE" },
    ],
  },
  {
    id: "entry", title: "Entry", icon: Crosshair, method: "POST", path: "/api/toolkit/entry",
    blurb: "One look at the live ATM CE/PE pair, using the agent's own call.",
    cost: "Spends throttled AngelOne calls. Market hours only.",
    fields: [
      { key: "symbol", label: "Symbol", type: "text", def: "NIFTY" },
      { key: "mode", label: "Mode", type: "text", def: "latest", hint: "latest | opening" },
    ],
  },
  {
    id: "exit", title: "Exit planner", icon: Target, method: "POST", path: "/api/toolkit/exit",
    blurb: "Stop and exit before entry, from both legs' OHLC. Same engine the agent runs.",
    fields: [
      { key: "ce", label: "CE O,H,L,C", type: "text", def: "100,112,97,108" },
      { key: "pe", label: "PE O,H,L,C", type: "text", def: "90,94,83,85" },
    ],
  },
  {
    id: "timemachine", title: "Time machine", icon: History, method: "POST", path: "/api/toolkit/timemachine",
    blurb: "Replays a plan over recent sessions. Same-bar ties go to the stop.",
    fields: [
      { key: "symbol", label: "Symbol", type: "text", def: "ADANIENT" },
      { key: "entry", label: "Entry", type: "number", def: 2500 },
      { key: "stop", label: "Stop", type: "number", def: 2480 },
      { key: "exit", label: "Exit", type: "number", def: 2540 },
      { key: "sessions", label: "Sessions", type: "number", def: 12 },
    ],
  },
  {
    id: "risk", title: "Risk gate", icon: ShieldCheck, method: "POST", path: "/api/toolkit/risk",
    blurb: "PASS or BLOCK against every limit, with all reasons. The agent's own gate.",
    fields: [
      { key: "symbol", label: "Symbol", type: "text", def: "NIFTY" },
      { key: "entry", label: "Entry", type: "number", def: 120 },
      { key: "stop", label: "Stop", type: "number", def: 105 },
      { key: "exit", label: "Exit", type: "number", def: 140 },
      { key: "lots", label: "Lots", type: "number", def: 1 },
    ],
  },
  {
    id: "budget", title: "Budget", icon: Calculator, method: "POST", path: "/api/toolkit/budget",
    blurb: "Rupee budget to whole lots. Floored, never rounded up.",
    fields: [
      { key: "symbol", label: "Symbol", type: "text", def: "NIFTY" },
      { key: "entry", label: "Entry", type: "number", def: 120 },
      { key: "stop", label: "Stop", type: "number", def: 105 },
      { key: "max_loss", label: "Max loss ₹", type: "number", hint: "blank = risk profile's own budget" },
    ],
  },
  {
    id: "broker", title: "Broker ticket", icon: Receipt, method: "POST", path: "/api/toolkit/broker",
    blurb: "Builds the ticket, runs the gate, and stops. It cannot place an order.",
    fields: [
      { key: "symbol", label: "Symbol", type: "text", def: "NIFTY" },
      { key: "entry", label: "Entry", type: "number", def: 120 },
      { key: "stop", label: "Stop", type: "number", def: 105 },
      { key: "exit", label: "Exit", type: "number", def: 140 },
      { key: "lots", label: "Lots", type: "number", def: 1 },
    ],
  },
  {
    id: "alerts", title: "Alerts", icon: BellRing, method: "GET", path: "/api/toolkit/alerts",
    blurb: "Price levels, stop proximity and square-off, from the collector's tick cache.",
    fields: [
      { key: "watch", label: "Watch", type: "text", def: "NIFTY-I" },
      { key: "above", label: "Above", type: "number" },
      { key: "below", label: "Below", type: "number" },
      { key: "close_warning", label: "Close warn (min)", type: "number" },
    ],
  },
  {
    id: "analyst", title: "Results", icon: FileText, method: "GET", path: "/api/toolkit/analyst",
    blurb: "Results headlines plus the measured price move. No filings are reachable for NSE.",
    fields: [
      { key: "symbol", label: "Symbol", type: "text", def: "RELIANCE" },
      { key: "hours", label: "News hours", type: "number", def: 72 },
      { key: "days", label: "Price sessions", type: "number", def: 7 },
    ],
  },
  {
    id: "sebi", title: "SEBI actions", icon: Gavel, method: "GET", path: "/api/toolkit/sebi",
    blurb: "Regulatory actions naming listed companies. Not insider disclosures — none are reachable.",
    fields: [
      { key: "kind", label: "Kind", type: "text", hint: "adjudication | settlement | recovery | exemption | manipulation" },
      { key: "q", label: "Contains", type: "text" },
    ],
  },
];

type Res = { loading: boolean; data?: Record<string, unknown>; error?: string };

export default function ToolkitPage() {
  const [active, setActive] = useState<string>(TOOLS[0].id);
  const [inputs, setInputs] = useState<Record<string, Record<string, string>>>({});
  const [results, setResults] = useState<Record<string, Res>>({});

  useEffect(() => {
    const seed: Record<string, Record<string, string>> = {};
    for (const t of TOOLS) {
      seed[t.id] = {};
      for (const f of t.fields) if (f.def !== undefined) seed[t.id][f.key] = String(f.def);
    }
    setInputs(seed);
  }, []);

  const tool = TOOLS.find(t => t.id === active)!;
  const res = results[active];

  const run = useCallback(async (t: Tool) => {
    setResults(r => ({ ...r, [t.id]: { loading: true } }));
    const vals = inputs[t.id] ?? {};
    try {
      let url = `${API_BASE}${t.path}`;
      let init: RequestInit = {};
      if (t.method === "GET") {
        const q = new URLSearchParams();
        for (const [k, v] of Object.entries(vals)) if (v !== "") q.set(k, v);
        if (t.id === "alerts") q.set("positions", "1");
        url += `?${q.toString()}`;
      } else {
        const body: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(vals)) {
          if (v === "") continue;
          // CE/PE arrive as "o,h,l,c" text; everything numeric is coerced here so
          // the API never has to guess what a field meant.
          if (k === "ce" || k === "pe") body[k] = v.split(",").map(x => Number(x.trim()));
          else if (["entry", "stop", "exit", "lots", "sessions", "max_loss", "capital"].includes(k)) body[k] = Number(v);
          else body[k] = v;
        }
        init = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
      }
      const r = await fetch(url, init);
      const data = await r.json();
      setResults(x => ({ ...x, [t.id]: { loading: false, data, error: data?.error } }));
    } catch (e) {
      setResults(x => ({ ...x, [t.id]: { loading: false, error: e instanceof Error ? e.message : String(e) } }));
    }
  }, [inputs]);

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col min-h-screen overflow-hidden">
        <main className="flex-1 p-5 overflow-y-auto">
          <div className="mb-5">
            <h1 className="text-sm font-bold uppercase tracking-wider flex items-center gap-2" style={{ color: GREEN }}>
              <Wrench className="w-4 h-4" /> Toolkit
            </h1>
            <p className="text-[10px] mt-0.5" style={{ color: MUTED }}>
              The eleven trader tools, adapted for Indian markets. Each one calls the same module as its
              <span style={{ color: TEXT }}> /trader-* </span> skill, so this page and the skills cannot disagree.
              Nothing here places an order.
            </p>
          </div>

          <div className="flex gap-1 mb-5 flex-wrap" style={{ borderBottom: `1px solid ${BORDER}` }}>
            {TOOLS.map(t => (
              <button key={t.id} onClick={() => setActive(t.id)}
                className="px-3 py-2 text-[10px] font-semibold uppercase tracking-wider flex items-center gap-1.5"
                style={{
                  color: active === t.id ? GREEN : MUTED,
                  borderBottom: active === t.id ? `2px solid ${GREEN}` : "2px solid transparent",
                }}>
                <t.icon className="w-3 h-3" /> {t.title}
              </button>
            ))}
          </div>

          <div className="t-panel p-4 mb-5">
            <div className="flex items-start justify-between gap-4 flex-wrap mb-3">
              <div>
                <h3 className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: TEXT }}>
                  {tool.title}
                </h3>
                <p className="text-[10px] mt-0.5" style={{ color: MUTED }}>{tool.blurb}</p>
                <p className="text-[9px] mt-1" style={{ color: DIM }}>
                  {tool.method} {tool.path} · skill: /{`trader-${tool.id === "analyst" ? "analyst" : tool.id}`}
                </p>
              </div>
              <button onClick={() => run(tool)} disabled={res?.loading}
                className="t-btn t-btn-green flex items-center gap-1.5 px-4 py-[7px] text-[11px] font-semibold uppercase tracking-wider disabled:opacity-50">
                <Play className="w-3 h-3" /> {res?.loading ? "Running…" : "Run"}
              </button>
            </div>

            {tool.cost && (
              <p className="text-[10px] mb-3 p-2 flex items-start gap-1.5"
                 style={{ background: INK, color: AMBER, border: `1px solid ${AMBER}` }}>
                <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-[1px]" /> {tool.cost}
              </p>
            )}

            <div className="flex flex-wrap gap-3">
              {tool.fields.map(f => (
                <div key={f.key}>
                  <label className="block text-[9px] uppercase tracking-wider mb-1" style={{ color: MUTED }}>
                    {f.label}
                  </label>
                  <input
                    type={f.type} value={inputs[tool.id]?.[f.key] ?? ""}
                    onChange={e => setInputs(s => ({ ...s, [tool.id]: { ...s[tool.id], [f.key]: e.target.value } }))}
                    className="px-2 py-[6px] text-[11px] w-36"
                    style={{ background: INK, border: `1px solid ${BORDER}`, color: TEXT }} />
                  {f.hint && <p className="text-[8px] mt-[2px]" style={{ color: DIM }}>{f.hint}</p>}
                </div>
              ))}
            </div>
          </div>

          {res?.error && (
            <div className="t-panel p-4 mb-5" style={{ borderColor: RED }}>
              <p className="text-[11px]" style={{ color: RED }}>ERROR: {res.error}</p>
            </div>
          )}

          {res?.data && !res.error && <Result tool={tool} data={res.data} />}
        </main>
      </div>
    </div>
  );
}

/* ── per-tool rendering ───────────────────────────────────────────────────── */

function Result({ tool, data }: { tool: Tool; data: Record<string, any> }) {
  return (
    <>
      {tool.id === "risk" || tool.id === "broker" ? <Verdict data={data} /> : null}
      {tool.id === "scanner" ? <Scanner data={data} /> : null}
      {tool.id === "budget" ? <Budget data={data} /> : null}
      {tool.id === "timemachine" ? <TimeMachine data={data} /> : null}
      {tool.id === "news" || tool.id === "analyst" ? <Headlines data={data} /> : null}
      {tool.id === "sebi" ? <Sebi data={data} /> : null}
      {tool.id === "alerts" ? <Alerts data={data} /> : null}
      {tool.id === "entry" || tool.id === "exit" ? <Plan data={data} /> : null}

      <details className="t-panel p-3 mt-4">
        <summary className="text-[10px] cursor-pointer" style={{ color: MUTED }}>Raw response</summary>
        <pre className="text-[9px] mt-2 overflow-x-auto" style={{ color: DIM }}>
          {JSON.stringify(data, null, 2)}
        </pre>
      </details>
    </>
  );
}

function Panel({ title, children, note }: { title: string; children: React.ReactNode; note?: string | null }) {
  return (
    <div className="t-panel p-4">
      <h3 className="text-[11px] font-semibold uppercase tracking-wider mb-2" style={{ color: MUTED }}>{title}</h3>
      {children}
      {note && <p className="text-[9px] mt-3 p-2" style={{ background: INK, color: AMBER }}>{note}</p>}
    </div>
  );
}

function Verdict({ data }: { data: Record<string, any> }) {
  const v = data.verdict;
  if (!v) return null;
  return (
    <Panel title={data.ticket ? "Ticket + risk gate" : "Risk gate"}
           note={data.sent === false ? `NOT SENT. ${data.next_step}` : null}>
      {data.ticket && (
        <div className="grid grid-cols-2 gap-x-4 gap-y-[2px] text-[10px] mb-3" style={{ maxWidth: 520 }}>
          <span style={{ color: DIM }}>instrument</span>
          <span style={{ color: TEXT }}>{data.ticket.option_symbol} · {data.ticket.lot_size} units ({data.lots} lot)</span>
          <span style={{ color: DIM }}>risk / outlay</span>
          <span style={{ color: TEXT }}>₹{data.risk_rupees?.toLocaleString("en-IN")} · ₹{data.premium_outlay?.toLocaleString("en-IN")}</span>
          <span style={{ color: DIM }}>trade mode</span>
          <span style={{ color: data.trade_mode === "paper" ? GREEN : AMBER }}>{String(data.trade_mode).toUpperCase()}</span>
        </div>
      )}
      <p className="text-[12px] font-bold mb-2" style={{ color: v.allowed ? GREEN : RED }}>
        {v.allowed ? "PASS" : "BLOCK"}
        <span className="text-[9px] font-normal ml-2" style={{ color: DIM }}>book: {data.book}</span>
      </p>
      <div className="grid gap-[2px]">
        {v.checks.map((c: any, i: number) => {
          const col = c.status === "pass" ? GREEN : c.status === "block" ? RED : c.status === "warn" ? AMBER : DIM;
          return (
            <div key={i} className="flex gap-2 text-[10px] py-[3px] px-2" style={{ background: INK }}>
              <span className="shrink-0 w-[40px] text-right font-semibold" style={{ color: col }}>
                {c.status === "pass" ? "ok" : c.status === "block" ? "BLOCK" : c.status}
              </span>
              <span className="shrink-0 w-[140px]" style={{ color: TEXT }}>{c.name}</span>
              <span style={{ color: MUTED }}>{c.detail}</span>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

function Scanner({ data }: { data: Record<string, any> }) {
  return (
    <Panel title={`Setups — ${data.universe} symbols, as of ${data.asof_bar}`} note={data.cutoff_note}>
      <p className="text-[10px] mb-2" style={{ color: MUTED }}>
        regime <span style={{ color: TEXT }}>{data.regime?.label ?? "?"}</span> · stale {data.stale?.length ?? 0} ·{" "}
        {Object.entries(data.counts ?? {}).map(([k, v]: [string, any]) => `${k}:${v.total}`).join(", ")}
      </p>
      {(!data.setups || data.setups.length === 0) ? (
        <p className="text-[10px]" style={{ color: DIM }}>No setups matched.</p>
      ) : (
        <table className="w-full text-[10px]">
          <thead><tr style={{ color: DIM }}>
            {["symbol", "pattern", "state", "score", "entry", "stop", "target"].map(h => (
              <th key={h} className={`font-normal ${h === "symbol" || h === "pattern" || h === "state" ? "text-left" : "text-right"}`}>{h}</th>
            ))}
          </tr></thead>
          <tbody>
            {data.setups.map((s: any, i: number) => (
              <tr key={i}>
                <td style={{ color: TEXT }}>{s.symbol}</td>
                <td style={{ color: MUTED }}>{s.pattern}</td>
                <td style={{ color: s.state === "breakout" ? GREEN : s.state === "breakdown" ? RED : AMBER }}>{s.state}</td>
                <td className="text-right" style={{ color: TEXT }}>{s.score}</td>
                <td className="text-right" style={{ color: MUTED }}>{s.entry}</td>
                <td className="text-right" style={{ color: MUTED }}>{s.stop}</td>
                <td className="text-right" style={{ color: MUTED }}>{s.target}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

function Budget({ data }: { data: Record<string, any> }) {
  const none = data.lots === 0;
  return (
    <Panel title="Position size">
      <p className="text-[18px] font-bold mb-1" style={{ color: none ? RED : GREEN }}>{data.verdict ?? data.error}</p>
      <p className="text-[10px] mb-2" style={{ color: MUTED }}>
        {data.risk_per_unit}/unit × {data.lot_units} = ₹{data.risk_per_lot?.toLocaleString("en-IN")} a lot ·
        budget ₹{data.max_loss_budget?.toLocaleString("en-IN")}
      </p>
      {none ? (
        <>
          <p className="text-[10px] mb-1" style={{ color: TEXT }}>{data.why}</p>
          <ul className="text-[10px]" style={{ color: MUTED }}>
            {(data.what_would_change_it ?? []).map((x: string, i: number) => <li key={i}>• {x}</li>)}
          </ul>
        </>
      ) : (
        <div className="grid grid-cols-2 gap-x-4 gap-y-[2px] text-[10px]" style={{ maxWidth: 420 }}>
          <span style={{ color: DIM }}>units</span><span style={{ color: TEXT }}>{data.units}</span>
          <span style={{ color: DIM }}>risk taken</span>
          <span style={{ color: TEXT }}>₹{data.risk_taken?.toLocaleString("en-IN")} (headroom ₹{data.headroom?.toLocaleString("en-IN")})</span>
          <span style={{ color: DIM }}>premium outlay</span><span style={{ color: TEXT }}>₹{data.premium_outlay?.toLocaleString("en-IN")}</span>
        </div>
      )}
    </Panel>
  );
}

function TimeMachine({ data }: { data: Record<string, any> }) {
  return (
    <Panel title={`Replay — ${data.symbol} ${data.direction}`}
           note={`A simulation over past sessions, not a prediction. Same-bar ties go to the stop. ${data.cost_source}.`}>
      <p className="text-[10px] mb-2" style={{ color: MUTED }}>
        {data.sessions_examined} session(s), {data.filled} filled · exits {data.wins} · stops {data.stops} ·
        timeouts {data.timeouts} · costs {data.cost_pct_applied}%
      </p>
      {data.mean_net_pct !== null && (
        <p className="text-[16px] font-bold mb-2" style={{ color: data.mean_net_pct > 0 ? GREEN : RED }}>
          {data.mean_net_pct > 0 ? "+" : ""}{data.mean_net_pct}% mean net per filled session
        </p>
      )}
      <div className="grid gap-[1px]">
        {(data.outcomes ?? []).map((o: any, i: number) => (
          <div key={i} className="flex gap-3 text-[10px] px-2 py-[2px]" style={{ background: INK }}>
            <span className="w-[80px]" style={{ color: DIM }}>{o.day}</span>
            <span className="w-[80px]" style={{ color: o.result === "exit" ? GREEN : o.result === "stop" ? RED : MUTED }}>{o.result}</span>
            <span className="w-[70px] text-right" style={{ color: MUTED }}>{o.fill ?? ""}</span>
            <span className="w-[70px] text-right" style={{ color: (o.net_pct ?? 0) >= 0 ? GREEN : RED }}>
              {o.net_pct !== undefined ? `${o.net_pct}%` : ""}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function Headlines({ data }: { data: Record<string, any> }) {
  const r = data.price_reaction;
  return (
    <Panel title={data.symbol ? `${data.symbol} — results & reaction` : "Headlines"} note={data.limitation ?? null}>
      {r && !r.error && (
        <p className="text-[11px] mb-2" style={{ color: r.change_pct >= 0 ? GREEN : RED }}>
          {r.from} → {r.to} ({r.change_pct >= 0 ? "+" : ""}{r.change_pct}% over {r.sessions} session(s))
          <span style={{ color: DIM }}> high {r.high} low {r.low}</span>
        </p>
      )}
      {r?.error && <p className="text-[10px] mb-2" style={{ color: AMBER }}>{r.error}</p>}
      {data.average_tone !== undefined && data.average_tone !== null && (
        <p className="text-[10px] mb-2" style={{ color: MUTED }}>average tone {data.average_tone}</p>
      )}
      {(data.articles ?? data.headlines ?? []).length === 0 ? (
        <p className="text-[10px]" style={{ color: DIM }}>Nothing in this window.</p>
      ) : (
        <div className="grid gap-[2px]">
          {(data.articles ?? data.headlines).map((a: any, i: number) => (
            <a key={i} href={a.url} target="_blank" rel="noreferrer"
               className="text-[10px] px-2 py-[3px] block" style={{ background: INK, color: TEXT }}>
              <span style={{ color: a.sentiment_label === "positive" ? GREEN : a.sentiment_label === "negative" ? RED : DIM }}>
                ●
              </span>{" "}
              <span style={{ color: DIM }}>{a.published_at?.slice(5, 16).replace("T", " ")}</span>{" "}
              <span style={{ color: MUTED }}>{a.source}</span> {a.title}
            </a>
          ))}
        </div>
      )}
    </Panel>
  );
}

function Sebi({ data }: { data: Record<string, any> }) {
  return (
    <Panel title={`SEBI actions — ${data.items?.length ?? 0} of ${data.total_in_feed}`} note={data.limitation}>
      <div className="grid gap-2">
        {(data.items ?? []).map((x: any, i: number) => (
          <div key={i} className="px-2 py-2" style={{ background: INK }}>
            <div className="text-[10px] mb-[2px]">
              <span style={{ color: AMBER }}>[{x.kinds?.join(",") || "—"}]</span>{" "}
              <span style={{ color: TEXT }}>{x.entity ?? "(no entity parsed)"}</span>
            </div>
            <a href={x.link} target="_blank" rel="noreferrer" className="text-[10px]" style={{ color: MUTED }}>
              {x.title}
            </a>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function Alerts({ data }: { data: Record<string, any> }) {
  return (
    <Panel title="Alerts" note={data.stale ? `Tick cache is STALE (${data.age_secs}s old). The collector may be down.` : null}>
      <p className="text-[10px] mb-2" style={{ color: MUTED }}>
        {data.symbols?.length ?? 0} symbols cached · {data.age_secs ?? "?"}s old
      </p>
      {data.watched && (
        <p className="text-[12px] mb-2" style={{ color: TEXT }}>
          {data.watched.symbol}: <b>{data.watched.price}</b>
          <span className="text-[9px] ml-2" style={{ color: DIM }}>{String(data.watched.ts ?? "").slice(11, 19)}</span>
        </p>
      )}
      {(data.positions ?? []).map((p: any, i: number) => (
        <div key={i} className="text-[10px] px-2 py-[3px]" style={{ background: INK, color: MUTED }}>
          {p.symbol} {p.direction} @ {p.entry} → {p.current} · stop {p.stop}
          {p.pct_to_stop !== null && <span style={{ color: p.pct_to_stop <= 2 ? RED : MUTED }}> ({p.pct_to_stop}% away)</span>}
        </div>
      ))}
      {(data.alerts ?? []).length > 0 ? (
        <div className="mt-2">
          {data.alerts.map((a: string, i: number) => (
            <p key={i} className="text-[11px] font-semibold" style={{ color: AMBER }}>** {a}</p>
          ))}
        </div>
      ) : <p className="text-[10px] mt-2" style={{ color: DIM }}>No alerts.</p>}
      {(data.notes ?? []).map((n: string, i: number) => (
        <p key={i} className="text-[9px] mt-1" style={{ color: AMBER }}>{n}</p>
      ))}
    </Panel>
  );
}

function Plan({ data }: { data: Record<string, any> }) {
  const d = data.decision ?? data;
  const state = data.state;
  if (state && state !== "SIGNAL") {
    return (
      <Panel title="Entry watch">
        <p className="text-[12px] font-bold" style={{ color: state === "ERROR" ? RED : AMBER }}>{state}</p>
        <p className="text-[10px] mt-1" style={{ color: MUTED }}>{data.detail}</p>
      </Panel>
    );
  }
  if (!d.tradable) {
    return (
      <Panel title="Plan">
        <p className="text-[12px] font-bold" style={{ color: RED }}>NO TRADE</p>
        <p className="text-[10px] mt-1" style={{ color: MUTED }}>
          {(d.blockers ?? []).join(", ") || "no clear side"}
        </p>
      </Panel>
    );
  }
  return (
    <Panel title="Plan" note="Levels are set before entry and are not moved afterwards.">
      <p className="text-[13px] font-bold mb-2" style={{ color: GREEN }}>
        {String(d.side).toUpperCase()}
        <span className="text-[9px] font-normal ml-2" style={{ color: DIM }}>
          confidence {d.confidence} · tier {d.tier}
        </span>
      </p>
      <div className="grid grid-cols-2 gap-x-4 gap-y-[2px] text-[10px]" style={{ maxWidth: 520 }}>
        <span style={{ color: DIM }}>entry</span><span style={{ color: TEXT }}>{d.entry}</span>
        <span style={{ color: DIM }}>stop</span><span style={{ color: TEXT }}>{d.stop}</span>
        <span style={{ color: DIM }}>exit taken (whole lot)</span><span style={{ color: GREEN }}>{d.partial}</span>
        <span style={{ color: DIM }}>far target (not used)</span><span style={{ color: MUTED }}>{d.target}</span>
        <span style={{ color: DIM }}>R:R taken</span>
        <span style={{ color: (d.rr_partial ?? 0) >= 1 ? GREEN : RED }}>{d.rr_partial}</span>
        <span style={{ color: DIM }}>R:R to far target</span><span style={{ color: MUTED }}>{d.rr}</span>
      </div>
    </Panel>
  );
}
