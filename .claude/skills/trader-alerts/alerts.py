"""
Alert on price levels, stops and the close.

Reads the live tick cache the collector already writes (one file, refreshed every
second) rather than calling the broker, so it costs no API budget and cannot
contribute to the rate-limit pressure that makes the live agent miss decisions.

    python alerts.py --list
    python alerts.py --watch NIFTY-I --above 26000 --below 25500
    python alerts.py --positions                 # stop/target proximity on open trades
    python alerts.py --close-warning 15          # warn N minutes before square-off
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

CACHE_CANDIDATES = [r"D:\tmp\td_live_prices.json", "/tmp/td_live_prices.json"]
SQUAREOFF = dtime(15, 25)
MAX_AGE_SECS = 90


def _cache() -> tuple[dict, str]:
    for p in CACHE_CANDIDATES:
        if os.path.exists(p):
            age = time.time() - os.path.getmtime(p)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f), f"{p} ({age:.0f}s old)"
            except (OSError, json.JSONDecodeError) as e:
                return {}, f"{p} unreadable: {e}"
    return {}, "no tick cache found (the collector is not running)"


def _stale(src: str) -> bool:
    return "old" in src and float(src.split("(")[-1].split("s")[0]) > MAX_AGE_SECS


def _positions() -> list:
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:5050/api/paper/positions", timeout=5) as r:
            return [p for p in json.load(r).get("positions", []) if p.get("status") == "OPEN"]
    except Exception:
        return []


def main() -> None:
    ap = argparse.ArgumentParser(description="Price, stop and close alerts from the live tick cache.")
    ap.add_argument("--list", action="store_true", help="Show everything the cache currently holds.")
    ap.add_argument("--watch", help="Symbol to test levels against, e.g. NIFTY-I.")
    ap.add_argument("--above", type=float)
    ap.add_argument("--below", type=float)
    ap.add_argument("--positions", action="store_true", help="Stop/target proximity on open trades.")
    ap.add_argument("--close-warning", type=int, default=0, metavar="MIN")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    cache, src = _cache()
    fired, notes = [], []
    if not cache:
        print(f"ERROR: {src}")
        sys.exit(2)
    if _stale(src):
        notes.append(f"Tick cache is STALE ({src}). Prices below may be old; the collector may be down.")

    if a.list:
        print(f"source: {src}\n")
        for k, v in sorted(cache.items()):
            print(f"  {k:30} {v.get('price')}   {str(v.get('ts',''))[11:19]}")

    if a.watch:
        row = cache.get(a.watch) or cache.get(a.watch.upper())
        if not row:
            print(f"ERROR: {a.watch} is not in the cache. Known: {', '.join(sorted(cache)[:8])} ...")
            sys.exit(2)
        px = float(row.get("price") or 0)
        print(f"\n{a.watch}: {px}   ({src})")
        if a.above is not None and px >= a.above:
            fired.append(f"{a.watch} {px} is AT OR ABOVE {a.above}")
        if a.below is not None and px <= a.below:
            fired.append(f"{a.watch} {px} is AT OR BELOW {a.below}")

    if a.positions:
        pos = _positions()
        if not pos:
            notes.append("No open positions (or the backend is not reachable).")
        for p in pos:
            cur, stop, tgt = p.get("current_premium"), p.get("sl"), p.get("target")
            if cur is None or not stop:
                continue
            to_stop = (cur - stop) / cur * 100 if cur else 0
            line = (f"{p.get('symbol')} {p.get('direction')} @ {p.get('entry_premium')} -> {cur} "
                    f"| stop {stop} ({to_stop:+.1f}% away) | target {tgt}")
            print("  " + line)
            if to_stop <= 2.0:
                fired.append(f"{p.get('symbol')} is within {to_stop:.1f}% of its stop")

    if a.close_warning:
        now = datetime.now()
        mins = (SQUAREOFF.hour * 60 + SQUAREOFF.minute) - (now.hour * 60 + now.minute)
        if 0 < mins <= a.close_warning:
            fired.append(f"{mins} minute(s) to square-off at {SQUAREOFF:%H:%M}")
        elif mins <= 0:
            notes.append(f"Square-off time {SQUAREOFF:%H:%M} has passed.")

    for n in notes:
        print(f"\nNOTE: {n}")
    if fired:
        print("\nALERTS")
        for f in fired:
            print(f"  ** {f}")
    elif a.watch or a.positions or a.close_warning:
        print("\nNo alerts.")
    sys.exit(0 if fired else 1)


if __name__ == "__main__":
    main()
