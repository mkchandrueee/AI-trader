"""
Set the stop and the exit BEFORE entry.

Wraps strategy/math_decision_strategy.analyse_option_pair -- the deterministic
candle-quality engine the live agent uses -- so the levels here are the same
levels the agent would act on, not a second opinion.

Given the two ATM legs' OHLC it returns: which side (if any), the breakout entry,
the stop at the losing candle's low, the exit actually taken (`partial`), the
reference tool's far target, and the R:R of the trade as taken.

    python exit.py --ce 100 112 97 108 --pe 90 94 83 85
    python exit.py --ce 100 112 97 108 --pe 90 94 83 85 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def plan(ce: tuple, pe: tuple) -> dict:
    from strategy.math_decision_strategy import analyse_option_pair

    d = analyse_option_pair(tuple(ce), tuple(pe))
    return d if isinstance(d, dict) else dict(d.__dict__)


def main() -> None:
    ap = argparse.ArgumentParser(description="Stop and exit for an ATM CE/PE pair, before entry.")
    ap.add_argument("--ce", nargs=4, type=float, required=True, metavar=("O", "H", "L", "C"))
    ap.add_argument("--pe", nargs=4, type=float, required=True, metavar=("O", "H", "L", "C"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        d = plan(a.ce, a.pe)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        sys.exit(2)

    if a.json:
        print(json.dumps(d, indent=2, default=str))
        sys.exit(0 if d.get("tradable") else 1)

    side = d.get("side")
    if not d.get("tradable"):
        blockers = ", ".join(d.get("blockers") or []) or "no clear side"
        print(f"NO TRADE - {blockers}")
        sys.exit(1)

    entry, stop = d.get("entry"), d.get("stop")
    partial, target = d.get("partial"), d.get("target")
    risk = (entry - stop) if (entry is not None and stop is not None) else None

    print(f"side        : {str(side).upper()}  (confidence {d.get('confidence')}, tier {d.get('tier')})")
    print(f"entry       : {entry}   breakout above the winning leg's high plus the buffer")
    print(f"stop        : {stop}   the candle low")
    print(f"exit taken  : {partial}   <- the WHOLE lot leaves here")
    print(f"far target  : {target}   reference tool only; the agent does not exit here")
    if risk:
        print(f"risk/unit   : {risk:.2f}")
        print(f"R:R taken   : {d.get('rr_partial')}   (on the exit above, not the far target)")
        print(f"R:R to far  : {d.get('rr')}")
    for w in d.get("warnings") or []:
        print(f"warning     : {w}")
    print("\nLevels are set before entry and are not moved afterwards. "
          "Size with trader-budget, then clear trader-risk.")
    sys.exit(0)


if __name__ == "__main__":
    main()
