---
name: trader-budget
description: >-
  Sizes an NSE F&O position to a rupee budget, in whole lots. Use when asked how
  many lots to take, how big a position should be, whether a trade fits the risk
  budget, or why a trade is being refused for size. Indian lots are indivisible
  (NIFTY 65, BANKNIFTY 35, SENSEX 20), so the answer is often zero lots, and this
  says so rather than rounding up.
argument-hint: "[--entry N --stop N --symbol NIFTY]"
---

# trader-budget

Converts a per-trade rupee budget into a number of **lots**. It never rounds a
position up and never suggests a fractional lot, because neither exists on the
exchange.

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-budget"

python size.py --entry 120 --stop 105 --symbol NIFTY           # profile's own budget
python size.py --entry 120 --stop 105 --max-loss 3000          # explicit rupee budget
python size.py --entry 120 --stop 105 --symbol BANKNIFTY --capital 200000
python size.py --entry 120 --stop 105 --json                   # machine-readable
```

Exit code is 0 when at least one lot fits, 1 when none does, 2 on a malformed order.

## What it reads

| source | what |
| --- | --- |
| `config/risk_profiles.py` | `max_capital_per_trade` for the active profile |
| `config/settings.py` | `INITIAL_CAPITAL` |
| `LOT_SIZES` in `size.py` | contract lot sizes, dated, changed only by exchange circular |

It writes nothing and places nothing.

## Step 1: give it the trade

`--entry` and `--stop` are premiums per unit, in rupees. The stop must be below
the entry: this platform only buys options, never writes them.

## Step 2: read the verdict

| result | meaning |
| --- | --- |
| `N lot(s)` | N lots fit the budget, with the leftover headroom shown |
| `NO TRADE` | one lot already exceeds the budget; there is no smaller position |

On `NO TRADE` it lists exactly what would change the answer: a tighter stop (with
the number), the capital that would make one lot fit at this profile's
percentage, or the per-trade budget that would.

## Example: the case this platform actually hits

MEDIUM profile, `INITIAL_CAPITAL` 50,000, `max_capital_per_trade` 1% → ₹500.

| quantity | value |
| --- | --- |
| risk per unit | ₹15 (entry 120, stop 105) |
| NIFTY lot | 65 units |
| risk per lot | ₹975 |
| budget | ₹500 |
| lots | **0** |

```
budget Rs.500  ->  NO TRADE
  One lot risks Rs.975, above the Rs.500 budget. Lots are indivisible, so there
  is no smaller position.
  what would change it:
    - A stop within Rs.7.69 a unit (entry 120.0 -> stop 112.31).
    - Capital of about Rs.97,500 at this profile's 1.0% per-trade budget.
    - A per-trade budget of at least Rs.975.
```

This is a real, current conflict in this platform's configuration, not a
contrived example — a 1% budget on ₹50,000 cannot fund one NIFTY lot on any
normal stop.

## Rules

- Lots are floored, never rounded. 0.99 lots is zero lots.
- The stop must be below the entry.
- Lot sizes come from the table, not from the broker, so they are inspectable and
  dated. Check them against the current exchange circular before a live trade.
- The budget defaults to the risk profile's own `max_capital_per_trade`; an
  explicit `--max-loss` overrides it for that call only and changes no config.

## What it never does

It never places an order, never edits a stop to make a position fit, and never
proposes a position larger than the budget allows.

## Safety

- Sizing is not permission. `trader-risk` still has to pass the order, and
  `trader-broker` still needs your approval.
- Premium outlay is reported alongside risk: a position can be inside the loss
  budget and still tie up more capital than intended.
