---
name: trader-risk
description: >-
  Checks an NSE F&O order against every risk limit this platform declares and
  returns PASS or BLOCK with all failing reasons. Use before any order, when
  asked whether a trade is allowed, or to explain why the agent refused a trade.
  Covers R:R, loss per trade, premium cap, trades per day, daily loss, concurrent
  positions, duplicates, entry window, strategy state and the live-fire backstop.
argument-hint: "[--entry N --stop N --exit N] or [--orders FILE]"
---

# trader-risk

The same gate `strategy/intraday_agent.py` runs before it opens anything
(`risk/pretrade.py`), exposed at a prompt. A trade refused in the agent log can
be reproduced here and understood.

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-risk"

python check.py --entry 120 --stop 105 --exit 140 --lots 1 --symbol NIFTY
python check.py --entry 120 --stop 105 --exit 140 --live     # read today's real book
python check.py --orders orders.json                          # batch, in file order
python check.py --entry 120 --stop 105 --exit 140 --json
```

Exit code 0 means every order passed, 1 means at least one was blocked.

## What it reads

| source | what |
| --- | --- |
| `config/risk_profiles.py` | premium cap, trades/day, per-trade budget, entry window |
| `config/settings.py` | capital, concurrent cap, daily loss ceiling (rupees) |
| `GET /api/paper/positions` | today's book, only with `--live` |

It writes nothing and places nothing.

## Step 1: decide which book to check against

Without `--live` the book is empty, so trades-per-day, daily-loss, duplicate and
concurrent checks all see a clean slate — useful for "would this order ever be
allowed?", wrong for "may I place this now?". With `--live` it reads the running
backend. If the backend is unreachable it says so and treats the book as empty
rather than quietly passing those checks.

## Step 2: read every reason, not the first

| check | blocks when |
| --- | --- |
| `shape` | entry, stop, exit, lot size or symbol missing or non-positive |
| `stop_side` | stop is not below entry (this platform only buys options) |
| `reward_risk` | (exit − entry) ÷ (entry − stop) is below `MIN_RR` |
| `max_loss_per_trade` | (entry − stop) × units exceeds the profile's budget |
| `max_premium` | entry premium above the profile cap |
| `max_concurrent` | the concurrent-position limit is already reached |
| `duplicate` | that option is already open |
| `max_trades_day` | the per-day count is already reached |
| `max_daily_loss` | booked P&L + open risk + this trade's risk breaches the ceiling |
| `entry_window` | outside market hours, inside the opening or closing skip, or past the afternoon cut-off |
| `strategy_state` | the strategy is SUSPENDED in the registry |
| `live_autofire` | `TRADE_MODE` is live without the explicit confirmation flag |

## Step 3: hand off

A PASS means the order *may* be shown for approval, never that it is approved.
`trader-broker` still presents it and still waits for you.

## Rules

- Every check runs. The verdict carries all failing reasons, because "blocked
  for three reasons" and "missed by a hair" are different situations.
- **Exits are never blocked.** A gate that can trap you in a position is worse
  than no gate. Pass `"is_exit": true` on an order in a batch file to say so.
- Thresholds come from existing config. This skill invents no numbers and
  changes none.
- It fails safe: if the gate errors, the order is BLOCKED.

## What it never does

It never places an order, never edits an order to make it pass, never relaxes a
limit, and never blocks an exit.

## Safety

- A PASS is about limits, not about whether the strategy works.
  `models/strategy_registry.py` and the Strategy Lab answer that, and the option
  engine is in `PAPER_TESTING` with a failing profit factor.
- Without `--live`, three of the checks are structurally unable to fail. Do not
  read that as a clean book.
