---
name: trader-news
description: >-
  Indian market headlines with keyword sentiment, from Economic Times, Livemint,
  RBI, SEBI and Google News India. Use when asked what the news is, whether
  anything happened to a stock, what the market mood is, or to check headlines
  before taking a position. Free RSS; no API key and no broker call.
argument-hint: "[--hours 6 --symbol RELIANCE]"
---

# trader-news

Wraps `data/news_sentiment.py`, which already reads eight Indian feeds. Covers
market and economy news, RBI press releases and SEBI notices — the sources that
move NSE, not US wire services.

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-news"

python news.py                        # last 24h across the market
python news.py --hours 6
python news.py --symbol RELIANCE      # headlines naming a symbol
python news.py --negative-only
python news.py --json
```

Exit 0 when articles matched, 1 when none did, 2 when the feeds could not be read.

## What it reads

| source | what |
| --- | --- |
| Economic Times (markets, economy) | market and macro headlines |
| Livemint (markets, money) | same |
| RBI press releases | policy, liquidity operations, rates |
| SEBI RSS | regulatory notices, orders and enforcement |
| Google News India (markets, earnings) | broad sweep including results |

It writes nothing. The backend's own route stores articles; this skill does not.

## Step 1: pick a window

`--hours` defaults to 24. Before an open, 12–24h catches the overnight. Intraday,
`--hours 2` is usually the honest window for "has anything just happened".

## Step 2: read the tone

The header prints an average sentiment and a label. This is **keyword** scoring:
it reads wording, not consequences. A headline saying a stock "plunges" scores
negative whether that is news or already priced in.

## Step 3: use it as a veto, not a signal

News is most useful for standing down — an RBI policy surprise or a SEBI order
naming a stock you were about to trade. The risk profiles already carry
`news_block_threshold`, which is the place to encode that, not a discretionary
read of these headlines.

## Rules

- `--symbol` matches the engine's extracted symbols and the headline text. It is
  a text match, so a common word as a ticker will over-match.
- Sentiment is keyword-based and has no model behind it.
- Feeds are public and best-effort; a source can be slow or briefly unavailable,
  and the article count will simply be lower.

## What it never does

It never trades, never scores a setup, and never claims a headline caused a move.

## Safety

- Headlines are not analysis. A negative tone is a reason to look, not to act.
- Sentiment here has never been validated against outcomes on this platform.
  Do not treat the average tone as predictive.
