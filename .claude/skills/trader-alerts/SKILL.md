---
name: trader-alerts
description: >-
  Alerts on NSE price levels, stop proximity on open positions, and the 15:25
  square-off. Use when asked to watch a level, to check whether a position is
  near its stop, how long until square-off, or what prices are live right now.
  Reads the collector's tick cache; makes no broker call.
argument-hint: "[--watch NIFTY-I --above N --below N] or [--positions]"
---

# trader-alerts

Reads the tick cache `scripts/collect_ticks.py` already writes every second,
rather than calling AngelOne. It therefore costs no API budget and cannot add to
the rate-limit pressure that makes the live agent miss decisions.

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-alerts"

python alerts.py --list                               # everything the cache holds
python alerts.py --watch NIFTY-I --above 26000 --below 25500
python alerts.py --positions                          # stop proximity on open trades
python alerts.py --close-warning 15                   # warn 15 min before square-off
```

Exit 0 when an alert fired, 1 when none did, 2 when the cache is missing or the
symbol is unknown.

## What it reads

| source | what |
| --- | --- |
| `D:\tmp\td_live_prices.json` (or `/tmp/...`) | last price per subscribed symbol, with receipt time |
| `GET /api/paper/positions` | open positions, for `--positions` |

It writes nothing and places nothing.

## Step 1: check the cache is fresh

Every run prints the cache age. Over 90 seconds it prints a STALE warning —
the collector writes every second during market hours, so a stale cache means it
is down, the market is closed, or the machine slept. Prices are still shown,
clearly labelled, rather than hidden.

## Step 2: choose what to watch

| mode | what it does |
| --- | --- |
| `--watch SYM --above/--below` | fires when the last price is at or past a level |
| `--positions` | fires when an open position is within 2% of its stop |
| `--close-warning MIN` | fires inside MIN minutes of the 15:25 square-off |

Modes combine; every fired alert is listed together at the end.

## Step 3: act on it yourself

An alert is a notification. Nothing here exits a position or places an order.

## Rules

- Levels are tested against the **last traded price** in the cache, not a quote.
  The bid-ask around it is real: the measured NIFTY option round-trip spread is
  0.237% of premium.
- A level already breached when you run it fires immediately. This is a check,
  not a trigger that waits for a crossing.
- Symbol names are the platform's internal aliases (`NIFTY-I`, and option
  symbols as the collector stores them). `--list` shows exactly what exists.

## What it never does

It never places or cancels an order, never moves a stop, and never exits a
position — including on a square-off warning.

## Safety

- **A stale cache looks exactly like a quiet market.** Always read the age line
  before trusting a price. During market hours, a stale cache means the collector
  needs attention.
- Stop proximity is computed from the cached last price, which can lag a fast
  move. Being "2% from the stop" is not a guarantee of 2% of room.
- Square-off at 15:25 is enforced by the agent, not by this skill. If the agent
  is not running, nothing here will close anything for you.
