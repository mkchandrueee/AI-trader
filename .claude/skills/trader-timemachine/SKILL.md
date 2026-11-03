---
name: trader-timemachine
description: >-
  Replays a stop/exit plan against real recorded NSE bars to show what would have
  happened on recent sessions. Use when asked what if, how a plan would have gone,
  whether a stop is too tight, or to sanity-check levels before trading them. A
  simulation over past sessions, never a prediction.
argument-hint: "[--symbol RELIANCE --entry N --stop N --exit N]"
---

# trader-timemachine

Takes a plan — entry, stop, exit — and replays it bar by bar on each recent
session in `data/intraday_cache`, charging costs from
`config/measured_costs.py`.

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-timemachine"

python timemachine.py --symbol RELIANCE --entry 1250 --stop 1240 --exit 1270
python timemachine.py --symbol ADANIENT --entry 2500 --stop 2480 --exit 2540 --sessions 20
python timemachine.py --symbol RELIANCE --entry 1250 --stop 1240 --exit 1270 --json
```

Exit 0 on a result, 1 when the entry was never touched, 2 when there are no bars.

## What it reads

| source | what |
| --- | --- |
| `data/intraday_cache` via `data.intraday_bars_store.load_bars` | 5-minute bars |
| `config/measured_costs.py` | the round-trip cost charged on every filled session |

It writes nothing.

## Step 1: give it a complete plan

Entry, stop and exit. Direction is inferred: exit above entry is long, below is
short. The entry fills on the first bar whose range covers the level.

## Step 2: read the outcome per session

| result | meaning |
| --- | --- |
| `exit` | the target was reached first |
| `stop` | the stop was hit first |
| `open at cap` | neither, within the 24-bar (≈2h) hold cap; marked at that bar's close |
| `no fill` | the entry level was never touched that session |

## Step 3: weigh it honestly

`mean net %` is per **filled** session, after costs. A plan that fills twice in
twelve sessions has a mean built on two numbers — look at the per-session rows,
not just the average.

## Rules

- **Same-bar ties go to the stop.** When one bar's range spans both levels there
  is no way to know which came first, and assuming the good one is how backtests
  lie. This makes results pessimistic by design.
- The hold cap is 24 bars, matching the agent's own timeout.
- Costs are charged on every filled session, never waived.

## What it never does

It never places an order, never adjusts the plan to improve the result, and never
reports a gross figure without costs.

## Safety

- **A simulation, not a prediction.** Past sessions are a sample of what those
  sessions did, not evidence about tomorrow.
- **Equity costs here are ASSUMED, not measured**, and the tool says so in its
  own output. This platform collects no equity quotes — only NIFTY options and
  futures — so the equity slippage figure has never been checked against real
  spreads. Treat equity results as optimistic by an unknown amount.
- A handful of filled sessions is not a sample. If you are choosing between
  plans on a few sessions' difference, you are choosing noise.
