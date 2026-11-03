"""
Scan the NSE board for intraday setups.

Wraps strategy/intraday_scanner.py's own scan() -- the same detectors the
Delivery tab's Intraday Engine uses -- over the cached 5-minute bars in
data/intraday_cache. No broker call and no network: it reads what the collector
already stored, so it works after hours and cannot disturb a live session's
AngelOne rate limit.

    python scan.py                      # every pattern, cached board
    python scan.py --pattern orb        # one pattern
    python scan.py --state breakout     # breakout | breakdown | coiling
    python scan.py --min-score 7 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def run(pattern: str | None, state: str | None, min_score: int) -> dict:
    from data.intraday_bars_store import load_all
    from strategy import intraday_scanner as isc

    frames = load_all()
    if not frames:
        return {"error": "No cached 5-minute bars. Trigger a sync (POST /api/intraday/sync on a running "
                         "backend), or run scripts/export_intraday_bars.py."}

    # data/intraday_bars_store sanitises symbols into filenames, so the index comes
    # back as NIFTY_I, not NIFTY-I. scan() uses index_symbol to decide which series
    # may NOT set the freshness reference -- NIFTY futures print to 15:40 while
    # stocks stop at 15:25, so if the index is not recognised every stock is marked
    # stale and the universe collapses to one. Resolve the key as stored.
    index_symbol = next((k for k in frames if k.upper().replace("-", "_") == "NIFTY_I"), "NIFTY-I")

    result = isc.scan(frames, datetime.now(), index_symbol=index_symbol)
    rows = []
    for p, found in result["setups"].items():
        if pattern and p != pattern:
            continue
        for d in found:
            if state and d.get("state") != state:
                continue
            if (d.get("score") or 0) < min_score:
                continue
            rows.append({**d, "pattern": p})
    rows.sort(key=lambda d: (-(d.get("score") or 0), d.get("symbol", "")))
    result["_rows"] = rows
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Scan cached NSE 5-minute bars for intraday setups.")
    ap.add_argument("--pattern", help="orb | vwap | level | box | hammer")
    ap.add_argument("--state", help="breakout | breakdown | coiling")
    ap.add_argument("--min-score", type=int, default=0)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    r = run(a.pattern, a.state, a.min_score)
    if r.get("error"):
        print(f"ERROR: {r['error']}")
        sys.exit(2)

    rows = r["_rows"][: a.limit]
    if a.json:
        print(json.dumps({"asof_bar": r["asof_bar"], "universe": r["universe"],
                          "regime": r["regime"], "counts": r["counts"], "setups": rows},
                         indent=2, default=str))
        sys.exit(0 if rows else 1)

    reg = r["regime"]
    print(f"as of {r['asof_bar']}   universe {r['universe']}   "
          f"regime {reg.get('label', reg)}   stale {len(r['stale'])}")
    counts = ", ".join(f"{p}:{c['total']}" for p, c in r["counts"].items())
    print(f"setups  {counts}")

    if not rows:
        # "Nothing matched" at the close looks like a broken scanner. It is not:
        # the detectors refuse to open a fresh setup on a bar closing after
        # LAST_SIGNAL_TIME, so a cache whose newest bar is late in the session
        # correctly yields nothing. Say which case this is.
        from strategy.intraday_scanner import LAST_SIGNAL_TIME
        import pandas as pd
        asof = pd.Timestamp(r["asof_bar"]) if r["asof_bar"] else None
        if asof is not None and asof.time() > LAST_SIGNAL_TIME:
            print(f"\nNothing matched - the newest cached bar is {asof:%H:%M}, past the "
                  f"{LAST_SIGNAL_TIME:%H:%M} cut-off for opening a fresh setup. This is the "
                  f"detectors declining by design, not an empty board. Scan again on "
                  f"intraday bars, or sync during market hours.")
        else:
            print("\nNothing matched.")
        sys.exit(1)
    print(f"\n{'symbol':14} {'pattern':8} {'state':10} {'score':>5} {'entry':>9} {'stop':>9} {'target':>9}")
    for d in rows:
        print(f"{d.get('symbol',''):14} {d.get('pattern',''):8} {d.get('state',''):10} "
              f"{d.get('score',''):>5} {d.get('entry',''):>9} {d.get('stop',''):>9} {d.get('target',''):>9}")
    print(f"\n{len(rows)} setup(s). CANDIDATES, not trades. The Intraday Engine's own replay found "
          f"no pattern profitable out of sample after costs -- see the Strategy Lab before acting.")
    sys.exit(0)


if __name__ == "__main__":
    main()
