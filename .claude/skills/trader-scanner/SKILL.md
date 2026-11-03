---
name: trader-scanner
description: >-
  Scans the cached NSE 5-minute board for intraday setups — opening-range breaks,
  VWAP reclaims, previous-day level breaks, box breaks and 30-minute hammers. Use
  when asked what is setting up today, to find candidates to trade, or to check a
  pattern across the board. Indian equities only; reads cached bars, never the
  broker.
argument-hint: "[--pattern orb --state breakout --min-score 7]"
---

# trader-scanner

Wraps `strategy/intraday_scanner.py`'s own `scan()` — the same detectors the
Delivery tab's Intraday Engine runs — over `data/intraday_cache`. No network and
no broker call, so it is safe during market hours and still works after close.

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-scanner"

python scan.py                              # whole board
python scan.py --pattern orb --state breakout
python scan.py --min-score 7 --limit 10
python scan.py --json
```

Exit 0 when something matched, 1 when nothing did, 2 when there are no cached bars.

## What it reads

| source | what |
| --- | --- |
| `data/intraday_cache` via `data.intraday_bars_store.load_all` | 5-minute bars per symbol |
| `strategy/intraday_scanner.py` | the detectors, scoring, regime and staleness logic |

It writes nothing.

## Step 1: make sure the cache is current

The header prints `as of <bar>` and a `stale` count. Bars are written by the live
collector during market hours; after a gap, run
`python scripts/export_intraday_bars.py` or let the backend sync. A high `stale`
count means most symbols stopped printing before the reference bar — normal after
hours, a warning during a session.

## Step 2: read the setups

Each row is a symbol, pattern, state (`breakout`, `breakdown`, `coiling`), the
detector's score, and the entry/stop/target the detector itself computed.

## Step 3: hand off

A setup is a candidate, not a trade. Run `trader-entry` to wait for confirmation,
`trader-budget` to size it, then `trader-risk`. Nothing reaches `trader-broker`
without passing the risk gate.

## Rules

- Incomplete bars are dropped by the scanner itself, so a still-forming candle
  can never produce a signal.
- Scores are the detector's own; this skill applies no extra ranking.
- It reports what the cache holds. It does not fetch, and so cannot be the reason
  a live session hits AngelOne's rate limit.

## What it never does

It never places an order, never sizes a position, and never claims a setup is
profitable.

## Safety

- **These patterns have no measured edge on this platform.** The Intraday
  Engine's own train/test replay found the best out-of-sample pattern/entry pair
  at −0.148% per trade against 0.155% costs, with 0 of 240 configurations
  profitable on either split. Treat output as research, not as signals. See the
  Strategy Lab (`/lab`) for the stored run.
- Stale cache produces confident-looking rows from old sessions. Check the
  `as of` stamp before acting on anything.
