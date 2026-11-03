"""
Indian market news with keyword sentiment.

Wraps data/news_sentiment.py, which already reads Economic Times, Livemint, RBI,
SEBI and Google News India. Free RSS, no key, no broker.

    python news.py                       # last 24h, whole market
    python news.py --hours 6
    python news.py --symbol RELIANCE     # headlines mentioning a symbol
    python news.py --negative-only --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def run(hours: int, symbol: str | None) -> dict:
    from data.news_sentiment import NewsSentimentEngine

    engine = NewsSentimentEngine()
    articles = engine.fetch_all(max_age_hours=hours)
    if symbol:
        s = symbol.upper()
        articles = [a for a in articles
                    if s in (a.symbols or []) or s in (a.title or "").upper()]
    return {"hours": hours, "symbol": symbol, "articles": articles}


def main() -> None:
    ap = argparse.ArgumentParser(description="Indian market news + keyword sentiment.")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--symbol", help="Filter to headlines naming this NSE symbol.")
    ap.add_argument("--negative-only", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        r = run(a.hours, a.symbol)
    except Exception as e:
        print(f"ERROR: could not fetch news ({type(e).__name__}: {e}).")
        sys.exit(2)

    arts = r["articles"]
    if a.negative_only:
        arts = [x for x in arts if (x.sentiment_score or 0) < 0]
    arts = sorted(arts, key=lambda x: x.published_at, reverse=True)[: a.limit]

    if a.json:
        print(json.dumps([{"title": x.title, "url": x.url, "source": x.source,
                           "published_at": x.published_at.isoformat(),
                           "symbols": x.symbols, "sentiment": x.sentiment_label,
                           "score": round(x.sentiment_score, 2)} for x in arts], indent=2))
        sys.exit(0 if arts else 1)

    if not arts:
        print(f"No articles in the last {a.hours}h"
              + (f" naming {a.symbol}." if a.symbol else "."))
        sys.exit(1)

    scores = [x.sentiment_score for x in arts if x.sentiment_score is not None]
    avg = sum(scores) / len(scores) if scores else 0.0
    mood = "negative" if avg < -0.05 else "positive" if avg > 0.05 else "mixed"
    print(f"{len(arts)} article(s), last {a.hours}h"
          + (f", naming {a.symbol}" if a.symbol else "")
          + f" — average tone {avg:+.2f} ({mood})\n")
    for x in arts:
        tag = {"positive": "+", "negative": "-"}.get(x.sentiment_label, "o")
        print(f"[{tag}] {x.published_at:%d %b %H:%M}  {x.source:18} {x.title[:92]}")
    print("\nKeyword sentiment, not a model. It reads wording, not consequences.")
    sys.exit(0)


if __name__ == "__main__":
    main()
