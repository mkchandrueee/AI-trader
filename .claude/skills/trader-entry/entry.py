"""
Watch the live ATM pair for an entry signal.

Wraps strategy/premarket.live_confirmation(), the same call the live agent makes
each cycle, so a signal here is the signal the agent would act on.

NEEDS A LIVE ANGELONE SESSION. It makes throttled broker calls, so:
  * run it during market hours, and
  * do not poll it in a tight loop -- AngelOne refuses this platform's calls at
    roughly half rate when demand spikes (measured 2026-09-24), and every refused
    call costs the live agent a decision.
Each run is ONE look. The --watch loop below deliberately spaces its looks.

    python entry.py --symbol NIFTY --mode latest
    python entry.py --symbol BANKNIFTY --mode opening
    python entry.py --symbol NIFTY --watch 5 --interval 60
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def look(symbol: str, mode: str) -> dict:
    from strategy.premarket import live_confirmation
    # retry_on_refusal stays False: this is a repeated look, like the agent's own
    # loop, and an extra retry doubles API spend during exactly the rate-limit
    # spike it is trying to survive.
    return live_confirmation(symbol=symbol, timeframe="5min", mode=mode,
                             today_only=True, retry_on_refusal=False)


def describe(d: dict) -> tuple[str, str]:
    if d.get("error"):
        return "ERROR", d["error"]
    if not d.get("is_live"):
        return "STALE", f"candle is from {d.get('session_date')}, not today"
    if not d.get("tradable"):
        return "WAIT", ", ".join(d.get("blockers") or []) or "no clear side"
    rr = d.get("rr_partial")
    return "SIGNAL", (f"{str(d.get('side')).upper()} entry {d.get('entry')} stop {d.get('stop')} "
                      f"exit {d.get('partial')} (R:R taken {rr})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch the live ATM CE/PE pair for an entry signal.")
    ap.add_argument("--symbol", default="NIFTY", help="NIFTY | BANKNIFTY | SENSEX")
    ap.add_argument("--mode", default="latest", choices=("latest", "opening"))
    ap.add_argument("--watch", type=int, default=1, help="How many looks to take (default 1).")
    ap.add_argument("--interval", type=int, default=60, help="Seconds between looks (minimum 30).")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    interval = max(30, a.interval)      # never poll faster than the agent itself
    for i in range(max(1, a.watch)):
        if i:
            time.sleep(interval)
        try:
            d = look(a.symbol.upper(), a.mode)
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            sys.exit(2)
        state, detail = describe(d)
        if a.json:
            print(json.dumps({"state": state, "detail": detail, "decision": d}, indent=2, default=str))
        else:
            print(f"[{state:6}] {a.symbol.upper()} {a.mode}: {detail}")
        if state == "SIGNAL":
            if not a.json:
                print("\nA signal is not permission. Size it (trader-budget), clear it "
                      "(trader-risk), then present it (trader-broker).")
            sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
