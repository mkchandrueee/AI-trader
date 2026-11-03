---
name: trader-exit
description: >-
  Sets the stop and the exit for a NIFTY/BANKNIFTY/SENSEX ATM option trade BEFORE
  entry, from the two legs' OHLC. Use when asked where the stop goes, where to
  take profit, what the reward-to-risk is, or to plan a trade before placing it.
  Returns the same levels the live agent would act on.
argument-hint: "[--ce O H L C --pe O H L C]"
---

# trader-exit

Wraps `strategy/math_decision_strategy.analyse_option_pair` — the deterministic
candle-quality engine `strategy/intraday_agent.py` runs — so these are the real
levels, not a second opinion that could disagree with the agent.

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-exit"

python exit.py --ce 100 112 97 108 --pe 90 94 83 85
python exit.py --ce 100 112 97 108 --pe 90 94 83 85 --json
```

Exit 0 when the pair is tradable, 1 when it is not, 2 on bad input.

## What it reads

| source | what |
| --- | --- |
| `strategy/math_decision_strategy.py` | side selection, buffer, stop, partial, target, R:R |
| `DEFAULT_CFG` in that module | confidence and margin thresholds, buffer and exit distances |

It writes nothing and fetches nothing — you supply the two candles.

## Step 1: give it both legs

Four numbers per leg, open high low close, for the **same** 5-minute candle on
the ATM call and the ATM put. Use the fixed 09:15–09:20 opening candle, or the
latest fully closed one. A still-forming candle gives a level that moves.

## Step 2: read the two different exits

| field | meaning |
| --- | --- |
| `entry` | breakout above the winning leg's high plus the buffer |
| `stop` | that candle's low |
| `exit taken` | where the **whole lot** actually leaves — `partial` |
| `far target` | the published reference tool's target; **the agent never exits here** |

## Step 3: judge on the R:R you will actually get

`R:R taken` uses the exit that happens, not the far target. These differ by about
2× and the distinction is not cosmetic: a review of 67 real closed trades found
the agent exiting at `partial` while the gate was measuring R:R to `target`, so
trades with a real R:R near 0.5 were being admitted. The gate now measures
`rr_partial`, and so does this skill.

## Example

```
side        : CALL  (confidence 78, tier strong)
entry       : 114.25     stop : 97.0
exit taken  : 124.25     far target : 134.25
R:R taken   : 0.58       R:R to far : 1.16
```

Taken R:R 0.58 is below `MIN_RR` 1.0, so `trader-risk` will block this despite
the far-target R:R looking acceptable. That is the gate working.

## Rules

- Levels are set before entry and are not moved afterwards.
- The stop is the candle low, not a percentage.
- A pair with no clear side returns `NO TRADE` and names the blockers.

## What it never does

It never places an order, never sizes a position, and never widens a stop to
improve the R:R.

## Safety

- A tradable plan is not a good trade. The option engine sits in `PAPER_TESTING`
  with a profit factor of 0.52 over 78 trades, and its recorded out-of-sample
  backtest is −7.38% of premium per trade. Check the Strategy Lab.
- Stops are a plan, not a guarantee: a gap or a fast move can fill well past the
  level, and option premiums gap more than the underlying.
