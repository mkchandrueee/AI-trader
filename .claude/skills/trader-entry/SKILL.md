---
name: trader-entry
description: >-
  Watches the live NIFTY/BANKNIFTY/SENSEX ATM CE-PE pair for an entry signal,
  using the same call the live agent makes. Use when asked whether a trade has
  triggered, to watch for an entry, or to check what the engine sees right now.
  Needs a live AngelOne session and market hours.
argument-hint: "[--symbol NIFTY --mode latest --watch 5]"
---

# trader-entry

Wraps `strategy/premarket.live_confirmation()` — the call
`strategy/intraday_agent.py` makes every cycle — so a signal here is the signal
the agent would act on, not a parallel opinion.

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-entry"

python entry.py --symbol NIFTY --mode latest      # one look
python entry.py --symbol NIFTY --mode opening     # the fixed 09:15-09:20 candle
python entry.py --symbol BANKNIFTY --watch 5 --interval 60
python entry.py --symbol NIFTY --json
```

Exit 0 on a signal, 1 if no signal in the looks taken, 2 on error.

## What it reads

| source | what |
| --- | --- |
| AngelOne historical candles, via `data/live_bars.py`'s shared throttle | the ATM legs |
| `strategy/premarket.py` | ATM resolution, candle fetch, engine call, cross-checks |

It writes nothing and places nothing.

## Step 1: respect the rate limit

This makes real broker calls. AngelOne refused roughly **half** of this
platform's historical calls on 2026-09-24 when demand spiked, and every refusal
costs the live agent a decision. So:

- `--interval` is floored at 30 seconds and defaults to 60, matching the agent's
  own cycle.
- Retry-on-refusal is deliberately off, for the same reason the agent keeps it
  off: an extra retry doubles spend during the spike it is trying to survive.
- Do not wrap this in a shell loop. Use `--watch`.

## Step 2: read the state

| state | meaning |
| --- | --- |
| `SIGNAL` | tradable now; side, entry, stop, exit and the R:R actually taken |
| `WAIT` | engine saw the pair but it is not tradable; blockers are named |
| `STALE` | the candle is not today's — the market is closed or data has not arrived |
| `ERROR` | no session, no ATM resolution, or the broker refused |

## Step 3: hand off

A signal is not permission. Size with `trader-budget`, clear `trader-risk`, then
present with `trader-broker`. The agent does this itself in paper mode; this
skill is for watching and understanding it.

## Rules

- `today_only` is on: it never walks back to a previous session to manufacture a
  reading.
- `opening` mode always means the fixed 09:15–09:20 candle.
- One run is one look unless `--watch` says otherwise.

## What it never does

It never places an order, never approves anything, and never retries a refused
broker call.

## Safety

- **Run it during market hours only.** Outside them every look returns `STALE`
  and still spends a broker call.
- A `SIGNAL` says the engine's conditions are met, not that the trade is
  worthwhile. The engine's measured out-of-sample expectancy is negative; see
  the Strategy Lab before acting.
- This platform's credential discipline applies: the session comes from the
  running backend or from `.env`, and credentials are never passed on the
  command line.
