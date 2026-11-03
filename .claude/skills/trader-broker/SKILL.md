---
name: trader-broker
description: >-
  Presents a finished NSE F&O order ticket with its risk-gate result, as the last
  step before a trade. Use when asked to place, send, submit or show an order
  ticket. It never sends anything — it prints the ticket and hands you the
  dashboard step or the exact endpoint.
argument-hint: "[--entry N --stop N --exit N --lots 1]"
---

# trader-broker

The last step before a trade, and deliberately the least capable file in this
set: it builds the ticket, runs the same gate (`risk/pretrade.py`) the live agent
runs, and then stops. **There is no code path in it that reaches a broker.**

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-broker"

python ticket.py --entry 120 --stop 105 --exit 140 --symbol NIFTY --lots 1
python ticket.py --entry 120 --stop 105 --exit 140 --option-symbol NIFTY04NOV2625900CE
python ticket.py --entry 120 --stop 105 --exit 140 --json
```

Exit 0 when the ticket passed the gate (still unsent), 1 when blocked.

## What it reads

| source | what |
| --- | --- |
| `risk/pretrade.py` | the full pre-trade gate |
| `config/risk_profiles.py`, `config/settings.py` | the limits |
| `GET /api/paper/positions` | today's book, so per-day and daily-loss checks are real |
| `TRADE_MODE` | whether this would be paper or live |

It writes nothing, places nothing, and cancels nothing.

## Step 1: build the ticket

Entry, stop and the exit actually taken, plus lots. The lot table is the
exchange's: NIFTY 65, BANKNIFTY 35, FINNIFTY 65, SENSEX 20.

## Step 2: read the gate result

Every check is printed. A BLOCK stops the ticket here; the reasons are the
things to fix or the reason to abandon the trade.

## Step 3: place it yourself, deliberately

| `TRADE_MODE` | what you do |
| --- | --- |
| `paper` | enter it from the dashboard's Live page, or `POST /api/paper/enter` |
| `angelone` / `mstock` | the agent stages an approval; you release it from the dashboard |

This mirrors what the platform already does: in paper mode the agent fires into
the paper book, and in live mode it stages a request in `models/approval.py` that
a human releases. A skill that placed orders would bypass that design.

## Rules

- **No trade goes through until you say yes.** This skill cannot say yes for you.
- A ticket that fails the gate is not presented as placeable.
- Paper is the default and stays the default. `TRADE_MODE` is changed in `.env`
  by you, never here.
- The ticket shows risk in rupees **and** premium outlay — a position can sit
  inside the loss budget and still tie up more capital than intended.

## What it never does

It never places, modifies or cancels an order; never approves a staged request;
never changes `TRADE_MODE`; and never touches broker credentials.

## Safety

- Live execution additionally requires `INTRADAY_AGENT_LIVE_AUTOFIRE_CONFIRMED`,
  a second deliberately alarming flag, before the agent will auto-fire anything.
  That backstop exists so flipping `TRADE_MODE` alone can never place an
  unapproved real order. Do not set it to make this skill more convenient.
- The strategy behind most tickets here is not profitable on the evidence: the
  option engine is `PAPER_TESTING`, profit factor 0.52 over 78 trades, recorded
  out-of-sample backtest −7.38% of premium per trade. A PASS is about limits,
  not about edge.
- If the backend is unreachable the book is treated as EMPTY and the tool says
  so. Three of the checks cannot fail in that state — do not read it as a clean
  book.
