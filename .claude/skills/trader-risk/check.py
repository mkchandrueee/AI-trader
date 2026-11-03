"""
Pre-trade risk check: PASS or BLOCK, with every reason.

A thin CLI over risk/pretrade.py, which is the same gate
strategy/intraday_agent.py runs before it opens anything. Running it here asks
exactly the question the agent asks, so a trade refused in the log can be
reproduced and understood at a prompt.

    python check.py --entry 120 --stop 105 --exit 140 --lots 1 --symbol NIFTY
    python check.py --orders orders.json          # a batch, checked in file order
    python check.py --entry 120 --stop 105 --exit 140 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 35, "FINNIFTY": 65, "SENSEX": 20}


def _limits():
    from config.risk_profiles import RiskLevel, get_risk_profile
    from config.settings import INITIAL_CAPITAL, MAX_CONCURRENT_POSITIONS, MAX_DAILY_LOSS
    from risk.pretrade import Limits
    import os
    try:
        profile = get_risk_profile(RiskLevel(os.getenv("RISK_LEVEL", "medium").lower()))
    except (ValueError, KeyError):
        profile = get_risk_profile(RiskLevel.MEDIUM)
    return Limits.from_config(profile, min_rr=1.0, capital=INITIAL_CAPITAL,
                              max_concurrent=MAX_CONCURRENT_POSITIONS,
                              max_daily_loss=MAX_DAILY_LOSS)


def _book(live: bool):
    """
    Today's book. Read from the running backend when it is up, so the per-day and
    daily-loss checks see reality; an empty book otherwise, which is stated rather
    than passed off as "nothing open".
    """
    from risk.pretrade import Book
    if not live:
        return Book(), "empty book (--live not set)"
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:5050/api/paper/positions", timeout=5) as r:
            rows = json.load(r).get("positions", [])
        return Book.from_paper_book(rows), f"live backend, {len(rows)} row(s)"
    except Exception as e:
        return Book(), f"backend unreachable ({type(e).__name__}); treating the book as EMPTY"


def check_one(order: dict, limits, book, now) -> "object":
    from risk import pretrade
    return pretrade.evaluate(order=order, book=book, limits=limits, now=now,
                             is_exit=bool(order.get("is_exit")))


def main() -> None:
    ap = argparse.ArgumentParser(description="PASS/BLOCK a trade against this platform's risk limits.")
    ap.add_argument("--entry", type=float)
    ap.add_argument("--stop", type=float)
    ap.add_argument("--exit", dest="exit_px", type=float, help="The exit actually taken (partial).")
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--option-symbol", default=None)
    ap.add_argument("--orders", help="JSON file: a list of orders, checked in file order.")
    ap.add_argument("--live", action="store_true", help="Read today's book from the running backend.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    limits = _limits()
    book, book_src = _book(a.live)
    now = datetime.now()

    if a.orders:
        orders = json.loads(Path(a.orders).read_text(encoding="utf-8"))
    else:
        if a.entry is None or a.stop is None or a.exit_px is None:
            ap.error("give --entry, --stop and --exit, or --orders FILE")
        units = LOT_SIZES.get(a.symbol.upper(), 65) * a.lots
        orders = [{"symbol": a.symbol.upper(),
                   "option_symbol": a.option_symbol or f"{a.symbol.upper()}-ATM",
                   "entry": a.entry, "stop": a.stop, "partial": a.exit_px, "lot_size": units}]

    results, blocked = [], 0
    for o in orders:
        v = check_one(o, limits, book, now)
        results.append({"order": o, "verdict": v.to_dict()})
        if not v.allowed:
            blocked += 1

    if a.json:
        print(json.dumps({"book": book_src, "results": results}, indent=2))
        sys.exit(1 if blocked else 0)

    print(f"book: {book_src}")
    for r in results:
        o, v = r["order"], r["verdict"]
        head = "PASS " if v["allowed"] else "BLOCK"
        print(f"\n[{head}] {o.get('option_symbol')}  entry {o.get('entry')} stop {o.get('stop')} "
              f"exit {o.get('partial')} x{o.get('lot_size')}")
        for c in v["checks"]:
            mark = {"pass": " ok ", "block": "BLOCK", "warn": "warn", "n/a": " -- "}[c["status"]]
            print(f"   [{mark}] {c['name']:20} {c['detail']}")
    print(f"\n{len(results) - blocked} passed, {blocked} blocked.")
    sys.exit(1 if blocked else 0)


if __name__ == "__main__":
    main()
