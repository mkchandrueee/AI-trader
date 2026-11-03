---
name: trader-analyst
description: >-
  Indian company results — the headlines reported and the measured price reaction
  from this platform's own EOD data. Use when asked about a company's results,
  earnings, quarterly numbers, or how a stock reacted to them. Note: no structured
  filings are available for NSE/BSE from a script, so it reports headlines and a
  verified move, never fabricated financials.
argument-hint: "[--symbol RELIANCE] or [--upcoming]"
---

# trader-analyst

The US original reads structured filings from SEC EDGAR. **India has no
equivalent this platform can reach**: NSE's corporate endpoints are Akamai-gated
and return empty payloads to scripted clients, and BSE's announcements API
returns HTTP 403 (both measured 2026-11-03).

So this skill does not produce an EPS table. It gives the two things that are
real: results **headlines**, and the **measured price reaction** from the
platform's own EOD store.

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-analyst"

python facts.py --symbol RELIANCE
python facts.py --symbol TCS --days 14 --hours 120
python facts.py --upcoming                 # results chatter across the market
python facts.py --symbol INFY --json
```

Exit 0 when headlines matched, 1 when none did, 2 when the feeds failed.

## What it reads

| source | what |
| --- | --- |
| Google News India earnings feed, via `data/news_sentiment.py` | results headlines |
| `data/eod_store.py`'s `load_panel()` | the actual close-to-close move |

It writes nothing.

## Step 1: separate the two halves

The **price reaction** is a fact computed from cached bhavcopy closes: start,
end, percent change, high and low over the window. The **headlines** are
third-party reporting with keyword sentiment attached.

## Step 2: do not expect numbers

There is no revenue line, no EPS, no segment table, and the tool says so in its
own output every run. If a figure is needed, read the company's filing directly.

## Example

```
RESULTS - RELIANCE   (headlines over 72h)
  price reaction: 1215.0 -> 1201.8 (-1.09% over 7 session(s), high 1215.0 low 1184.5)
```

## Rules

- `--days` controls the price window, `--hours` the news window. They are
  independent.
- Symbol matching is on extracted symbols plus the headline text, so a short or
  common ticker will over-match.
- A missing symbol in the EOD panel is reported, not silently skipped.

## What it never does

It never quotes a financial figure it has not read from a source, never
summarises a filing it cannot access, and never trades.

## Safety

- **No structured filings are available here.** Any revenue or EPS number in a
  conversation about this skill's output came from somewhere else — check it.
- The price reaction is close-to-close over cached sessions. It is not
  attribution: a stock can fall through its results week for unrelated reasons.
- Headline sentiment is keyword-based and has never been validated against
  outcomes on this platform.
