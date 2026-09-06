"use client";

import { useEffect, useState, useCallback } from "react";
import Sidebar from "@/components/Sidebar";
import { fetchJSON } from "@/lib/api";
import { RefreshCw } from "lucide-react";

interface NewsArticle {
  title: string;
  url: string;
  source: string;
  published_at: string;
  summary: string;
  symbols: string[];
  sentiment_label: "bullish" | "bearish" | "neutral";
  sentiment_score: number;
  impact_level: "low" | "medium" | "high" | "critical";
}

interface MarketSentiment {
  score: number;
  label: string;
  article_count: number;
  bullish_count: number;
  bearish_count: number;
  neutral_count: number;
  high_impact_count: number;
  has_critical_event: boolean;
  should_block_trading: boolean;
  top_headlines: string[];
}

interface NewsBriefResponse {
  articles: NewsArticle[];
  sentiment: MarketSentiment | null;
  error?: string;
}

const sentimentColor: Record<string, string> = {
  bullish: "#00e87b", bearish: "#ff3e3e", neutral: "#5a6270",
};

const impactColor: Record<string, string> = {
  critical: "#ff3e3e", high: "#e8c300", medium: "#4da6ff", low: "#3d4450",
};

export default function NewsBriefPage() {
  const [data, setData] = useState<NewsBriefResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "bullish" | "bearish">("all");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchJSON<NewsBriefResponse>("/api/news/brief?hours=24");
      if (res.error) throw new Error(res.error);
      setData(res);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 5 * 60 * 1000); // refresh every 5 min
    return () => clearInterval(id);
  }, [load]);

  const articles = (data?.articles || []).filter(
    (a) => filter === "all" || a.sentiment_label === filter
  );

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col min-h-screen overflow-hidden">
        <main className="flex-1 p-5 overflow-y-auto">
          <div className="flex items-center justify-between mb-5">
            <div>
              <h1 className="text-sm font-bold uppercase tracking-wider" style={{ color: "#00e87b" }}>
                News Brief
              </h1>
              <p className="text-[10px] mt-0.5" style={{ color: "#5a6270" }}>
                Free RSS: Economic Times, LiveMint, RBI, SEBI, Google News — keyword-based sentiment, not a trading signal.
              </p>
            </div>
            <button onClick={load} className="t-btn flex items-center gap-1.5 text-[10px] uppercase tracking-wider">
              <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} /> Refresh
            </button>
          </div>

          {error && (
            <div className="t-panel p-4 mb-5" style={{ borderColor: "#ff3e3e" }}>
              <p className="text-[11px]" style={{ color: "#ff3e3e" }}>ERROR: {error}</p>
            </div>
          )}

          {data?.sentiment && (
            <div className="grid grid-cols-2 md:grid-cols-5 gap-[1px] mb-5">
              <div className="t-panel p-3">
                <div className="text-[9px] uppercase" style={{ color: "#5a6270" }}>Sentiment</div>
                <div className="text-[14px] font-bold" style={{ color: sentimentColor[data.sentiment.label] || "#c8cdd5" }}>
                  {data.sentiment.label.toUpperCase()}
                </div>
              </div>
              <div className="t-panel p-3">
                <div className="text-[9px] uppercase" style={{ color: "#5a6270" }}>Score</div>
                <div className="text-[14px] font-bold">{data.sentiment.score >= 0 ? "+" : ""}{data.sentiment.score.toFixed(2)}</div>
              </div>
              <div className="t-panel p-3">
                <div className="text-[9px] uppercase" style={{ color: "#5a6270" }}>Bullish / Bearish</div>
                <div className="text-[14px] font-bold">
                  <span style={{ color: "#00e87b" }}>{data.sentiment.bullish_count}</span>
                  {" / "}
                  <span style={{ color: "#ff3e3e" }}>{data.sentiment.bearish_count}</span>
                </div>
              </div>
              <div className="t-panel p-3">
                <div className="text-[9px] uppercase" style={{ color: "#5a6270" }}>High Impact</div>
                <div className="text-[14px] font-bold" style={{ color: data.sentiment.high_impact_count > 0 ? "#e8c300" : "#c8cdd5" }}>
                  {data.sentiment.high_impact_count}
                </div>
              </div>
              <div className="t-panel p-3">
                <div className="text-[9px] uppercase" style={{ color: "#5a6270" }}>Critical Event</div>
                <div className="text-[14px] font-bold" style={{ color: data.sentiment.has_critical_event ? "#ff3e3e" : "#00e87b" }}>
                  {data.sentiment.has_critical_event ? "YES" : "NO"}
                </div>
              </div>
            </div>
          )}

          {/* Filter tabs */}
          <div className="flex gap-[1px] mb-4">
            {(["all", "bullish", "bearish"] as const).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className="px-4 py-[6px] text-[10px] font-semibold tracking-wider uppercase transition-all"
                style={{
                  background: filter === f ? (sentimentColor[f] || "#4da6ff") : "#181c24",
                  color: filter === f ? "#000" : "#5a6270",
                  border: `1px solid ${filter === f ? (sentimentColor[f] || "#4da6ff") : "#252a33"}`,
                }}
              >
                {f}
              </button>
            ))}
          </div>

          <div className="t-panel p-4">
            {loading && !data ? (
              <p className="text-[11px] text-center py-6" style={{ color: "#3d4450" }}>Loading…</p>
            ) : articles.length === 0 ? (
              <p className="text-[11px] text-center py-6" style={{ color: "#3d4450" }}>No articles in the last 24h.</p>
            ) : (
              <div className="space-y-3">
                {articles.map((a) => (
                  <a
                    key={a.url}
                    href={a.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="block p-3 transition-all hover:bg-[#181c24]"
                    style={{ borderLeft: `2px solid ${sentimentColor[a.sentiment_label]}` }}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <p className="text-[12px] font-medium flex-1" style={{ color: "#c8cdd5" }}>{a.title}</p>
                      {a.impact_level !== "low" && (
                        <span
                          className="text-[8px] font-bold uppercase tracking-wider px-1.5 py-[2px] shrink-0"
                          style={{ background: impactColor[a.impact_level], color: "#000" }}
                        >
                          {a.impact_level}
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-3 mt-1 text-[9px]" style={{ color: "#5a6270" }}>
                      <span>{a.source}</span>
                      <span>{new Date(a.published_at).toLocaleString("en-IN")}</span>
                      <span style={{ color: sentimentColor[a.sentiment_label] }}>{a.sentiment_label}</span>
                      {a.symbols.length > 0 && <span>{a.symbols.join(", ")}</span>}
                    </div>
                  </a>
                ))}
              </div>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
