"""
Indian results: what was reported, and how the stock actually reacted.

WHAT THIS CANNOT DO, stated up front. The US original reads structured filings
from SEC EDGAR. India has no equivalent this platform can reach: NSE's corporate
endpoints sit behind Akamai and return empty payloads to scripted clients
(measured 2026-11-03: /api/corporates-pit returned {"acqNameList":[],"data":[]}),
and BSE's announcements API returned HTTP 403. So there are no structured
earnings numbers here -- no revenue line, no EPS, no segment table.

What IS reachable, and what this gives you instead:
  * results HEADLINES, from the Google News India earnings feed already wired
    into data/news_sentiment.py, and
  * the measured PRICE REACTION from this platform's own EOD store, which is a
    fact rather than a summary.

A headline plus a verified move is honest. A fabricated EPS table would not be.

    python facts.py --symbol RELIANCE
    python facts.py --symbol TCS --days 14 --json
    python facts.py --upcoming                 # results chatter across the market
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def headlines(symbol: str | None, hours: int) -> list:
    from data.news_sentiment import NewsSentimentEngine
    engine = NewsSentimentEngine()
    arts = engine.fetch_all(max_age_hours=hours)
    kw = ("result", "results", "earnings", "profit", "revenue", "quarter",
          "q1", "q2", "q3", "q4", "ebitda", "margin")
    out = []
    for a in arts:
        text = (a.title or "").lower()
        if not any(k in text for k in kw):
            continue
        if symbol and symbol.upper() not in (a.symbols or []) and symbol.upper() not in text.upper():
            continue
        out.append(a)
    return sorted(out, key=lambda x: x.published_at, reverse=True)


def reaction(symbol: str, days: int) -> dict:
    """The move that actually happened, from our own EOD store. A fact, not a view."""
    try:
        from data.eod_store import load_panel
        panel = load_panel(sessions=days + 2)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if panel is None or panel.empty:
        return {"error": "No EOD sessions cached. Run data/eod_store.py's sync() first."}
    df = panel[panel["symbol"] == symbol.upper()].sort_values("date")
    if df.empty:
        return {"error": f"{symbol.upper()} is not in the cached EOD panel."}
    df = df.tail(days + 1)
    if len(df) < 2:
        return {"error": "Not enough sessions to measure a move."}
    first, last = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
    best, worst = float(df["close"].max()), float(df["close"].min())
    return {"sessions": len(df) - 1, "from": first, "to": last,
            "change_pct": round(100 * (last - first) / first, 2),
            "high": best, "low": worst}


def main() -> None:
    ap = argparse.ArgumentParser(description="Indian results headlines + the measured price reaction.")
    ap.add_argument("--symbol", help="NSE symbol, e.g. RELIANCE.")
    ap.add_argument("--upcoming", action="store_true", help="Results chatter across the market.")
    ap.add_argument("--days", type=int, default=7, help="Sessions for the price reaction.")
    ap.add_argument("--hours", type=int, default=72, help="News window.")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not a.symbol and not a.upcoming:
        ap.error("give --symbol SYM or --upcoming")

    try:
        arts = headlines(a.symbol, a.hours)[: a.limit]
    except Exception as e:
        print(f"ERROR: could not read the news feeds ({type(e).__name__}: {e}).")
        sys.exit(2)

    react = reaction(a.symbol, a.days) if a.symbol else None

    if a.json:
        print(json.dumps({
            "symbol": a.symbol, "window_hours": a.hours,
            "headlines": [{"title": x.title, "source": x.source, "url": x.url,
                           "published_at": x.published_at.isoformat(),
                           "sentiment": x.sentiment_label} for x in arts],
            "price_reaction": react,
            "limitation": "No structured filings: NSE corporate endpoints are Akamai-gated "
                          "and BSE's announcements API returns 403 to scripted clients.",
        }, indent=2))
        sys.exit(0 if arts else 1)

    title = a.symbol.upper() if a.symbol else "market-wide"
    print(f"RESULTS - {title}   (headlines over {a.hours}h)")
    if react:
        if react.get("error"):
            print(f"  price reaction: {react['error']}")
        else:
            print(f"  price reaction: {react['from']} -> {react['to']} "
                  f"({react['change_pct']:+.2f}% over {react['sessions']} session(s), "
                  f"high {react['high']} low {react['low']})")
    print()
    if not arts:
        print("  No results headlines in this window.")
    for x in arts:
        tag = {"positive": "+", "negative": "-"}.get(x.sentiment_label, "o")
        print(f"  [{tag}] {x.published_at:%d %b %H:%M}  {x.source:16} {x.title[:92]}")

    print("\nHeadlines and a measured move - NOT a filing summary. There are no structured")
    print("Indian earnings numbers here: NSE's corporate endpoints are Akamai-gated and BSE's")
    print("announcements API returns 403, so no revenue or EPS figure is available to quote.")
    sys.exit(0 if arts else 1)


if __name__ == "__main__":
    main()
