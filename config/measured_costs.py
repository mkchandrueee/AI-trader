"""
Measured Transaction Costs
──────────────────────────
Costs in this project have been assumed rather than measured, and the assumption
has been optimistic every time it was checked: the intraday charge figure was 3x
too low until it was corrected against the real schedule, and the option backtest
(scripts/backtest_math_engine.py) still models a flat Rs.40 round trip with NO
spread at all -- as does the live paper P&L in backend/app.py.

Spread is not a small correction on NIFTY options. We record `bid_price` and
`ask_price` on every tick, so it does not have to be guessed: this module
measures it from our own `tick_data` and caches the result with the window it
came from, so a number can always be traced back to the sample that produced it.

WHAT CAN AND CANNOT BE MEASURED HERE

  NIFTY options   measurable. This is what the live agent trades.
  NIFTY futures   measurable.
  NSE equities    NOT measurable -- we subscribe to no equity instruments, so
                  there are no equity quotes in tick_data. The Intraday and
                  Positional engines' slippage stays an assumption, and is
                  labelled as one rather than quietly borrowing the option
                  figure, which would be wrong by an order of magnitude
                  (equities quote in ticks of a rupee on a Rs.1000+ price;
                  options quote in 0.05 on a Rs.100 premium).

METHOD

The median, not the mean. Deep-OTM strikes quote spreads of tens of percent on a
Rs.2 premium, and they dominate an average: the first attempt at this figure came
out at 18.169% before the distribution was looked at. Restricting to premiums at
or above MIN_PREMIUM -- the band the agent actually trades -- and taking the
median gives a figure that describes a fill the agent could realistically get.

A measured spread is the FULL bid-ask. Crossing it once costs half; a round trip
(cross in, cross out) costs the full spread, which is what `round_trip_pct`
reports.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger("measured_costs")

_CACHE = Path(__file__).resolve().parent.parent / "models" / "saved" / "measured_costs.json"

LOOKBACK_DAYS = 30
MIN_PREMIUM = 20.0          # the agent's own tradeable band, not lottery strikes
MIN_TICKS = 5000            # below this the measurement is not worth trusting

# Used only when there is no measurement on file. Deliberately pessimistic: an
# unmeasured cost should never flatter a backtest.
FALLBACK_OPTION_ROUND_TRIP = 0.005      # 0.5% of premium
FALLBACK_FUTURES_ROUND_TRIP = 0.0005    # 0.05%

# NSE equities: no quote data exists for these, so this stays an assumption.
# Stated here rather than buried in a scanner constant so it is visible as one.
ASSUMED_EQUITY_ROUND_TRIP = 0.0005
ASSUMED_EQUITY_REASON = ("No equity instruments are subscribed, so tick_data holds no equity quotes. "
                         "Cannot be measured with the data this platform collects.")


def _measure(kind: str) -> Optional[dict]:
    """One instrument class, straight from our own recorded quotes."""
    from database.db import read_sql

    where = {
        "option": "(symbol LIKE 'NIFTY%%CE' OR symbol LIKE 'NIFTY%%PE')",
        "futures": "symbol = 'NIFTY-I'",
    }[kind]
    min_prem = MIN_PREMIUM if kind == "option" else 0.0

    df = read_sql(f"""
        WITH t AS (
            SELECT price, ask_price - bid_price AS spread
            FROM tick_data
            WHERE {where}
              AND timestamp >= now() - interval '{LOOKBACK_DAYS} days'
              AND bid_price > 0 AND ask_price > 0 AND ask_price >= bid_price
              AND price >= {min_prem}
        )
        SELECT COUNT(*)                                                                   AS ticks,
               AVG(price)                                                                 AS avg_price,
               AVG(spread)                                                                AS avg_spread_pts,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY spread / price)                AS median_frac,
               PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY spread / price)                AS p90_frac
        FROM t
    """, {})

    if df.empty or not df.iloc[0]["ticks"] or int(df.iloc[0]["ticks"]) < MIN_TICKS:
        n = int(df.iloc[0]["ticks"]) if not df.empty and df.iloc[0]["ticks"] else 0
        logger.warning(f"Not enough {kind} quotes to measure a spread: {n} ticks (need {MIN_TICKS}).")
        return None

    r = df.iloc[0]
    return {
        "kind": kind,
        "ticks": int(r["ticks"]),
        "avg_price": round(float(r["avg_price"]), 2),
        "avg_spread_points": round(float(r["avg_spread_pts"]), 4),
        "round_trip_pct": round(float(r["median_frac"]), 6),
        "p90_round_trip_pct": round(float(r["p90_frac"]), 6),
        "lookback_days": LOOKBACK_DAYS,
        "min_premium": min_prem,
        "measured_at": datetime.now().isoformat(timespec="seconds"),
    }


def refresh() -> dict:
    """Re-measure both instrument classes and write the cache. Needs the DB."""
    out = {}
    for kind in ("option", "futures"):
        m = _measure(kind)
        if m:
            out[kind] = m
            logger.info(f"Measured {kind} spread: {100 * m['round_trip_pct']:.3f}% round trip "
                        f"over {m['ticks']:,} quotes (p90 {100 * m['p90_round_trip_pct']:.3f}%).")
    if out:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    return out


def _cached() -> dict:
    if not _CACHE.exists():
        return {}
    try:
        with open(_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.error(f"Measured-cost cache at {_CACHE} is unreadable; falling back to assumptions.")
        return {}


def option_round_trip_pct() -> float:
    """Round-trip spread cost as a fraction of premium. Measured if we have it."""
    m = _cached().get("option")
    return m["round_trip_pct"] if m else FALLBACK_OPTION_ROUND_TRIP


def futures_round_trip_pct() -> float:
    m = _cached().get("futures")
    return m["round_trip_pct"] if m else FALLBACK_FUTURES_ROUND_TRIP


def option_spread_rupees(premium: float) -> float:
    """Rupees per unit lost to the spread on a round trip at this premium."""
    return premium * option_round_trip_pct()


def provenance() -> dict:
    """Where each figure came from -- what the Strategy Lab shows next to it."""
    c = _cached()
    out = {}
    for kind, fallback in (("option", FALLBACK_OPTION_ROUND_TRIP), ("futures", FALLBACK_FUTURES_ROUND_TRIP)):
        m = c.get(kind)
        out[kind] = {
            "value": m["round_trip_pct"] if m else fallback,
            "measured": bool(m),
            "source": (f"{m['ticks']:,} of our own quotes over {m['lookback_days']} days, "
                       f"median, premium >= {m['min_premium']:g} (measured {m['measured_at'][:16].replace('T', ' ')})")
                      if m else "No measurement on file -- using a deliberately pessimistic fallback.",
        }
    out["equity"] = {
        "value": ASSUMED_EQUITY_ROUND_TRIP,
        "measured": False,
        "source": ASSUMED_EQUITY_REASON,
    }
    return out


if __name__ == "__main__":
    print(json.dumps(refresh(), indent=2))
