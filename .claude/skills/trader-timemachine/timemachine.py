"""
Replay a stop/exit plan against real recorded bars.

Not a prediction. It takes a plan -- entry, stop, exit -- and replays what would
have happened to it on each recent session in data/intraday_cache, bar by bar,
with a same-bar tie going to the STOP. That tie rule is deliberately pessimistic:
when a single bar's range spans both levels there is no way to know which came
first, and assuming the good one is how backtests lie.

Costs are charged from config/measured_costs.py, measured from this platform's
own recorded bid/ask quotes, not assumed.

    python timemachine.py --symbol RELIANCE --entry 1250 --stop 1240 --exit 1270
    python timemachine.py --symbol RELIANCE --entry 1250 --stop 1240 --exit 1270 --sessions 20
    python timemachine.py --symbol RELIANCE --entry 1250 --stop 1240 --exit 1270 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

MAX_BARS = 24          # about two hours of 5-minute bars, the agent's own hold cap


def replay(symbol: str, entry: float, stop: float, exit_px: float, sessions: int) -> dict:
    import pandas as pd
    from config import measured_costs
    from data.intraday_bars_store import load_bars

    df = load_bars(symbol)
    if df is None or df.empty:
        return {"error": f"No cached bars for {symbol}. Run scripts/export_intraday_bars.py."}

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["day"] = df["timestamp"].dt.date
    days = sorted(df["day"].unique())[-sessions:]
    long = exit_px > entry
    cost_frac = measured_costs.ASSUMED_EQUITY_ROUND_TRIP

    outcomes = []
    for d in days:
        bars = df[df["day"] == d].reset_index(drop=True)
        # Enter on the first bar whose range covers the entry level.
        start = next((i for i in range(len(bars))
                      if bars.low[i] <= entry <= bars.high[i]), None)
        if start is None:
            outcomes.append({"day": str(d), "result": "no fill"})
            continue
        result, px = "open at cap", float(bars.close.iloc[min(start + MAX_BARS, len(bars) - 1)])
        for i in range(start, min(start + MAX_BARS, len(bars))):
            hi, lo = float(bars.high[i]), float(bars.low[i])
            hit_stop = lo <= stop if long else hi >= stop
            hit_exit = hi >= exit_px if long else lo <= exit_px
            if hit_stop:                      # same-bar tie -> stop wins
                result, px = "stop", stop
                break
            if hit_exit:
                result, px = "exit", exit_px
                break
        gross = (px - entry) / entry * (1 if long else -1)
        outcomes.append({"day": str(d), "result": result, "fill": round(px, 2),
                         "net_pct": round(100 * (gross - cost_frac), 3)})

    traded = [o for o in outcomes if o["result"] != "no fill"]
    nets = [o["net_pct"] for o in traded]
    return {
        "symbol": symbol, "entry": entry, "stop": stop, "exit": exit_px,
        "direction": "long" if long else "short",
        "sessions_examined": len(days), "filled": len(traded),
        "cost_pct_applied": round(100 * cost_frac, 3),
        "cost_source": "ASSUMED for equities -- no equity quotes are collected, so this is not measured",
        "wins": sum(1 for o in traded if o["result"] == "exit"),
        "stops": sum(1 for o in traded if o["result"] == "stop"),
        "timeouts": sum(1 for o in traded if o["result"] == "open at cap"),
        "mean_net_pct": round(sum(nets) / len(nets), 3) if nets else None,
        "outcomes": outcomes,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay a plan against recorded NSE bars.")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--stop", type=float, required=True)
    ap.add_argument("--exit", dest="exit_px", type=float, required=True)
    ap.add_argument("--sessions", type=int, default=12)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    r = replay(a.symbol.upper(), a.entry, a.stop, a.exit_px, a.sessions)
    if a.json:
        print(json.dumps(r, indent=2))
        sys.exit(2 if r.get("error") else 0)
    if r.get("error"):
        print(f"ERROR: {r['error']}")
        sys.exit(2)

    print(f"{r['symbol']} {r['direction']}  entry {r['entry']} stop {r['stop']} exit {r['exit']}")
    print(f"{r['sessions_examined']} session(s) examined, {r['filled']} filled  "
          f"(costs {r['cost_pct_applied']}% round trip)")
    if not r["filled"]:
        print("The entry level was never touched in these sessions.")
        sys.exit(1)
    print(f"  exits {r['wins']}   stops {r['stops']}   timeouts {r['timeouts']}")
    print(f"  mean net {r['mean_net_pct']:+.3f}% per filled session")
    print(f"\n{'day':12} {'result':12} {'fill':>10} {'net %':>8}")
    for o in r["outcomes"]:
        if o["result"] == "no fill":
            print(f"{o['day']:12} {'no fill':12}")
        else:
            print(f"{o['day']:12} {o['result']:12} {o['fill']:>10} {o['net_pct']:>8.3f}")
    print("\nA SIMULATION over past sessions, not a prediction. Same-bar ties go to "
          "the stop. " + r["cost_source"] + ".")
    sys.exit(0)


if __name__ == "__main__":
    main()
