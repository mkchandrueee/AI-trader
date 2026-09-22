#!/usr/bin/env python3
"""
Backfill the last N trading days of NIFTY-I + ATM option 1-min candles via
AngelOne (market data source -- mStock is order execution only, see
CLAUDE.md's mStock section) — for populating the Charts page (and anything
else that reads minute_candles) when the DB is empty or has gaps older than
today (backfill_today.py only covers today).

Usage: python scripts/backfill_history.py --days 10
"""
import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from data.market_data_adapter import MarketDataAdapter
from database.db import upsert_candles, read_sql
from utils.logger import get_logger

logger = get_logger("backfill_history")


def backfill_history(days: int, strikes_each_side: int = 3):
    end = datetime.now()
    start = end - timedelta(days=days)
    print(f"Backfilling {days} calendar days: {start.date()} -> {end.date()}")

    td = MarketDataAdapter()
    if not td.authenticate():
        print("ERROR: AngelOne authentication failed.")
        return 1

    print("Fetching NIFTY-I 1-min candles...")
    nifty_bars = td.fetch_historical_bars("NIFTY-I", start, end, "1min")
    if nifty_bars.empty:
        print("ERROR: No NIFTY-I bars returned — aborting (can't resolve ATM without a price).")
        return 1
    nifty_bars = nifty_bars.copy()
    nifty_bars["symbol"] = "NIFTY-I"
    upsert_candles(nifty_bars)
    print(f"  Upserted {len(nifty_bars)} NIFTY-I bars ({nifty_bars['timestamp'].min()} -> {nifty_bars['timestamp'].max()})")

    current_price = float(nifty_bars.iloc[-1]["close"])
    atm = round(current_price / 50) * 50
    print(f"  Current NIFTY: {current_price:.1f}, ATM: {atm}")

    from backtest.option_resolver import get_nearest_expiry, build_option_symbol
    expiry = get_nearest_expiry(date.today())
    if expiry is None:
        print("ERROR: Could not resolve nearest NIFTY expiry — skipping option backfill.")
        return
    print(f"  Nearest expiry: {expiry}")

    symbols = []
    for offset in range(-strikes_each_side, strikes_each_side + 1):
        strike = atm + offset * 50
        symbols.append(build_option_symbol(expiry, strike, "CE"))
        symbols.append(build_option_symbol(expiry, strike, "PE"))

    total = 0
    for i, sym in enumerate(symbols):
        if i > 0:
            time.sleep(0.35)  # AngelOne historical REST: ~3 req/sec
        try:
            opt_bars = td.fetch_historical_bars(sym, start, end, "1min")
            if opt_bars.empty:
                print(f"    {sym}: no data")
                continue
            opt_bars = opt_bars.copy()
            opt_bars["symbol"] = sym
            upsert_candles(opt_bars)
            total += len(opt_bars)
            print(f"    {sym}: {len(opt_bars)} bars")
        except Exception as e:
            print(f"    {sym}: ERROR {e}")
    print(f"  Option candles upserted: {total} total bars across {len(symbols)} contracts")

    final = read_sql(
        "SELECT timestamp::date as day, COUNT(*) as bars FROM minute_candles "
        "WHERE symbol = 'NIFTY-I' GROUP BY 1 ORDER BY 1"
    )
    print(f"\nminute_candles now has NIFTY-I data for {len(final)} day(s):")
    for _, r in final.iterrows():
        print(f"  {r['day']}: {r['bars']} bars")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill N days of NIFTY-I + ATM option candles")
    parser.add_argument("--days", type=int, default=10, help="Calendar days to look back (default 10)")
    parser.add_argument("--strikes", type=int, default=3, help="Strikes each side of ATM to backfill (default 3)")
    args = parser.parse_args()
    # Propagate failure: the AI Models page reports a job green/red purely on
    # exit code, so a backfill that authenticated nothing and wrote nothing
    # must not exit 0 — that would show "DONE" over a silent no-op.
    sys.exit(backfill_history(args.days, args.strikes) or 0)
