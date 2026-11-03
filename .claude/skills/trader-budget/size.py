"""
Position sizing for NSE F&O: how many LOTS, given a max loss per trade.

The hard part in Indian options is not the arithmetic, it is that lots are
indivisible. NIFTY trades in 65-unit lots, BANKNIFTY in 35, SENSEX in 20. A
rupee budget does not convert to a position size; it converts to a number of
lots, which is usually 0 or 1, and "0 lots" is the answer far more often than
people expect. Measured on this platform's own MEDIUM profile on 2026-11-03:
max_capital_per_trade 1% of Rs.50,000 = Rs.500, while one NIFTY lot with a
typical Rs.15 stop risks Rs.975 -- so the honest answer is zero lots, and the
fix is more capital or a wider per-trade budget, not a smaller position.

This tool says that plainly instead of rounding 0.51 lots up to 1.

    python size.py --entry 120 --stop 105 --symbol NIFTY
    python size.py --entry 120 --stop 105 --symbol NIFTY --max-loss 2000
    python size.py --entry 120 --stop 105 --symbol BANKNIFTY --capital 200000
    python size.py --entry 120 --stop 105 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

# Exchange lot sizes. Not guessed -- these are the contract specifications, and
# they change by exchange circular, so they live in one place with a date.
LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 35, "FINNIFTY": 65, "SENSEX": 20}
LOT_SIZES_AS_OF = "2026-11-03"


def size(entry: float, stop: float, symbol: str, max_loss: float | None = None,
         capital: float | None = None) -> dict:
    from config.risk_profiles import RiskLevel, get_risk_profile
    from config.settings import INITIAL_CAPITAL

    symbol = symbol.upper()
    lot_units = LOT_SIZES.get(symbol)
    profile = get_risk_profile(RiskLevel.MEDIUM)
    capital = capital if capital is not None else INITIAL_CAPITAL
    budget = max_loss if max_loss is not None else profile.max_capital_per_trade * capital

    risk_per_unit = entry - stop
    out = {
        "symbol": symbol, "entry": entry, "stop": stop,
        "lot_units": lot_units, "capital": capital, "max_loss_budget": round(budget, 2),
        "risk_per_unit": round(risk_per_unit, 2),
    }

    if lot_units is None:
        out["error"] = (f"No lot size on file for {symbol}. Known: {', '.join(sorted(LOT_SIZES))} "
                        f"(as of {LOT_SIZES_AS_OF}).")
        return out
    if risk_per_unit <= 0:
        out["error"] = f"stop {stop} is not below entry {entry}; this is a long option buy."
        return out

    risk_per_lot = risk_per_unit * lot_units
    lots = int(budget // risk_per_lot)                 # floor: never round a position up
    out.update({
        "risk_per_lot": round(risk_per_lot, 2),
        "lots": lots,
        "units": lots * lot_units,
        "risk_taken": round(lots * risk_per_lot, 2),
        "premium_outlay": round(lots * lot_units * entry, 2),
    })

    if lots == 0:
        # The whole point of this tool. Say WHY, and what would change it.
        max_stop = budget / lot_units
        need_capital = (risk_per_lot / profile.max_capital_per_trade) if max_loss is None else None
        out["verdict"] = "NO TRADE"
        out["why"] = (f"One lot risks Rs.{risk_per_lot:,.0f}, above the Rs.{budget:,.0f} budget. "
                      f"Lots are indivisible, so there is no smaller position.")
        out["what_would_change_it"] = [
            f"A stop within Rs.{max_stop:.2f} a unit (entry {entry} -> stop {entry - max_stop:.2f}).",
        ] + ([f"Capital of about Rs.{need_capital:,.0f} at this profile's "
              f"{profile.max_capital_per_trade:.1%} per-trade budget."] if need_capital else []) + [
            f"A per-trade budget of at least Rs.{risk_per_lot:,.0f}.",
        ]
    else:
        out["verdict"] = f"{lots} lot(s)"
        out["headroom"] = round(budget - lots * risk_per_lot, 2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Size an NSE F&O position to a max loss per trade.")
    ap.add_argument("--entry", type=float, required=True, help="Entry premium per unit (Rs).")
    ap.add_argument("--stop", type=float, required=True, help="Stop premium per unit (Rs).")
    ap.add_argument("--symbol", default="NIFTY", help=f"Underlying: {', '.join(sorted(LOT_SIZES))}.")
    ap.add_argument("--max-loss", type=float, default=None,
                    help="Rupee budget for this trade. Default: the risk profile's share of capital.")
    ap.add_argument("--capital", type=float, default=None, help="Override INITIAL_CAPITAL.")
    ap.add_argument("--json", action="store_true", help="Machine-readable output.")
    a = ap.parse_args()

    r = size(a.entry, a.stop, a.symbol, a.max_loss, a.capital)
    if a.json:
        print(json.dumps(r, indent=2))
        sys.exit(0 if r.get("lots") else 1)

    if r.get("error"):
        print(f"ERROR: {r['error']}")
        sys.exit(2)

    print(f"{r['symbol']}  entry Rs.{r['entry']}  stop Rs.{r['stop']}  "
          f"({r['risk_per_unit']}/unit x {r['lot_units']} = Rs.{r['risk_per_lot']:,.0f} a lot)")
    print(f"budget Rs.{r['max_loss_budget']:,.0f}  ->  {r['verdict']}")
    if r["lots"]:
        print(f"  units        : {r['units']}")
        print(f"  risk taken   : Rs.{r['risk_taken']:,.0f}   (headroom Rs.{r['headroom']:,.0f})")
        print(f"  premium out  : Rs.{r['premium_outlay']:,.0f}")
    else:
        print(f"  {r['why']}")
        print("  what would change it:")
        for line in r["what_would_change_it"]:
            print(f"    - {line}")
    sys.exit(0 if r["lots"] else 1)


if __name__ == "__main__":
    main()
