"""
Position sizing for NSE F&O: a rupee budget -> a number of LOTS.

Shared by .claude/skills/trader-budget and backend/skills_api.py so the Toolkit
page and the skill can never disagree about a position size.

The hard part is not the arithmetic, it is that lots are indivisible. NIFTY
trades in 65-unit lots, BANKNIFTY in 35, SENSEX in 20, so a budget converts to a
number of lots, which is often 0 -- and 0 is the honest answer rather than a
rounded-up 1. Measured on this platform's MEDIUM profile: 1% of Rs.50,000 is
Rs.500, while one NIFTY lot on a typical Rs.15 stop risks Rs.975.
"""
from __future__ import annotations

from typing import Optional

# Contract specifications, not guesses. They change by exchange circular, so they
# live in one place with a date.
LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 35, "FINNIFTY": 65, "SENSEX": 20}
LOT_SIZES_AS_OF = "2026-11-03"


def size_position(entry: float, stop: float, symbol: str = "NIFTY",
                  max_loss: Optional[float] = None,
                  capital: Optional[float] = None) -> dict:
    """
    How many whole lots fit the budget. Lots are FLOORED, never rounded up.

    `max_loss` defaults to the active risk profile's `max_capital_per_trade`
    share of `capital` (which defaults to INITIAL_CAPITAL).
    """
    from config.risk_profiles import RiskLevel, get_risk_profile
    from config.settings import INITIAL_CAPITAL

    symbol = (symbol or "NIFTY").upper()
    lot_units = LOT_SIZES.get(symbol)
    profile = get_risk_profile(RiskLevel.MEDIUM)
    capital = capital if capital is not None else INITIAL_CAPITAL
    budget = max_loss if max_loss is not None else profile.max_capital_per_trade * capital
    risk_per_unit = entry - stop

    out = {"symbol": symbol, "entry": entry, "stop": stop, "lot_units": lot_units,
           "capital": capital, "max_loss_budget": round(budget, 2),
           "risk_per_unit": round(risk_per_unit, 2)}

    if lot_units is None:
        out["error"] = (f"No lot size on file for {symbol}. Known: {', '.join(sorted(LOT_SIZES))} "
                        f"(as of {LOT_SIZES_AS_OF}).")
        return out
    if risk_per_unit <= 0:
        out["error"] = f"stop {stop} is not below entry {entry}; this is a long option buy."
        return out

    risk_per_lot = risk_per_unit * lot_units
    lots = int(budget // risk_per_lot)
    out.update({"risk_per_lot": round(risk_per_lot, 2), "lots": lots,
                "units": lots * lot_units,
                "risk_taken": round(lots * risk_per_lot, 2),
                "premium_outlay": round(lots * lot_units * entry, 2)})

    if lots == 0:
        max_stop = budget / lot_units
        need_capital = (risk_per_lot / profile.max_capital_per_trade) if max_loss is None else None
        out["verdict"] = "NO TRADE"
        out["why"] = (f"One lot risks Rs.{risk_per_lot:,.0f}, above the Rs.{budget:,.0f} budget. "
                      f"Lots are indivisible, so there is no smaller position.")
        out["what_would_change_it"] = [
            f"A stop within Rs.{max_stop:.2f} a unit (entry {entry} -> stop {entry - max_stop:.2f})."
        ] + ([f"Capital of about Rs.{need_capital:,.0f} at this profile's "
              f"{profile.max_capital_per_trade:.1%} per-trade budget."] if need_capital else []) + [
            f"A per-trade budget of at least Rs.{risk_per_lot:,.0f}."
        ]
    else:
        out["verdict"] = f"{lots} lot(s)"
        out["headroom"] = round(budget - lots * risk_per_lot, 2)
    return out
