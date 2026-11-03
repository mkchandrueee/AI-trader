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

from backtest.plan_replay import MAX_BARS, replay_plan  # noqa: E402

# The replay lives in backtest/plan_replay.py so this skill and the Toolkit page
# share one implementation of the same-bar tie rule and the cost handling.
replay = replay_plan


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
