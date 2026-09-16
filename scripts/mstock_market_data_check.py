"""
mStock Market Data Check — run this yourself, not through an AI assistant.
──────────────────────────────────────────────────────────────────────────
Verifies data/mstock_market_data.py actually works against your real
account: fetches a small sample of historical NIFTY-I candles, then
connects the live WebSocket ticker for ~15 seconds and prints a few real
ticks. Read-only — places no orders, modifies nothing.

This opens its OWN mStock session (separate from your dashboard's mStock
Connect card and from broker/mstock_adapter.py's order-execution session).
If mStock enforces one active session per login, running this could log
out your dashboard connection — reconnect there afterward if so.

The instrument-master field names this depends on (instrument_token,
expiry format, instrument_type values) are inferred from Kite Connect's
conventions, not yet confirmed against mStock's real response — this
script's whole point is to catch a mismatch here, safely, before anything
touches the live tick collector. If either step below fails or prints
something that looks wrong (e.g. no rows, a KeyError, an obviously wrong
price), share the output and I'll fix data/mstock_symbols.py /
data/mstock_market_data.py's field names accordingly.

Usage:
  python scripts/mstock_market_data_check.py
"""
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from data.mstock_market_data import MStockMarketData


def main():
    md = MStockMarketData()
    print("Attempting mStock market-data login...")
    if not md.authenticate():
        print("FAILED to authenticate — check the logs above for the reason.")
        return 1
    print("Authenticated.\n")

    # ── Historical candles ──────────────────────────────────────────────
    print("Fetching last 5 days of 1-min NIFTY-I candles (small sample)...")
    df = md.fetch_historical_bars(
        "NIFTY-I", datetime.now() - timedelta(days=5), datetime.now(), interval="1min",
    )
    if df.empty:
        print("EMPTY — either genuinely no data in this window, or symbol/token resolution failed.")
        print("Check the ERROR-level log lines above for which.")
    else:
        print(f"Got {len(df)} rows. Columns: {list(df.columns)}")
        print(df.tail(3).to_string())
    print()

    # ── Live ticks ────────────────────────────────────────────────────────
    print("Connecting WebSocket and subscribing to NIFTY-I for ~15s...")
    if not md.ws_connect():
        print("FAILED to connect WebSocket.")
        return 1

    ticks_seen = []
    md.ws_subscribe(["NIFTY-I"])
    md.ws_start_streaming(lambda tick: ticks_seen.append(tick))

    for _ in range(15):
        time.sleep(1)
        if len(ticks_seen) >= 3:
            break

    md.ws_stop_streaming()
    md.ws_disconnect()

    if not ticks_seen:
        print("\nNo ticks received in 15s. During market hours this likely means the")
        print("instrument-token resolution or tick parsing is off — check ERROR lines above.")
        return 1

    print(f"\nReceived {len(ticks_seen)} tick(s). Sample:")
    for t in ticks_seen[:3]:
        print(f"  {t}")

    print("\nDONE. If the price/volume/oi values above look sane for NIFTY futures, this is working.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
