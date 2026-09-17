"""
mStock Option-Candle Check — run this yourself, not through an AI assistant.
──────────────────────────────────────────────────────────────────────────
Verifies the ONE piece of data/mstock_market_data.py that has never been
exercised against a real account: fetch_historical_bars() for an actual
OPTION contract (not just NIFTY-I futures, which
scripts/mstock_market_data_check.py already covers). Resolves today's
NIFTY ATM CE/PE two ways -- AngelOne's live instrument master (the proven
path strategy/premarket.py already uses) and mStock's own instrument
master via the DB-internal alias format -- then fetches the same window's
candles from both brokers and prints them side by side so you can eyeball
whether mStock's premiums/timestamps roughly agree with AngelOne's.

Read-only — places no orders, modifies nothing. Opens mStock's own
market-data session (separate from the dashboard's Connect card and from
broker/mstock_adapter.py's order-execution session) — if mStock enforces
one session per login, this could log out the dashboard connection;
reconnect there afterward if so.

What to look for:
  - Both sides return rows, with similar row counts for the window.
  - Close prices are in the same ballpark (small bid/ask-driven
    differences are fine; a wildly different number, or one side being
    ~100x the other, means a scaling/token/symbol bug).
  - Timestamps land on the same 5-minute grid.
If mStock's side is empty or errors, share the output — the ERROR-level
log lines above the table will say whether it was symbol resolution or
the historical-chart call itself that failed, so the fix targets the
right function (data/mstock_symbols.py's option_symbol_for(), or
data/mstock_market_data.py's fetch_historical_bars()).

Usage:
  python scripts/mstock_option_candle_check.py
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from data.market_data_adapter import MarketDataAdapter
from data.mstock_market_data import MStockMarketData
from backtest.option_resolver import build_option_symbol
from strategy.premarket import _resolve_atm_symbols, _current_spot


def main():
    print("Resolving today's NIFTY ATM CE/PE via AngelOne's live instrument master...")
    spot = _current_spot("NIFTY")
    if spot is None:
        print("FAILED: could not get a current/previous-close NIFTY price.")
        return 1
    resolved = _resolve_atm_symbols("NIFTY", spot)
    if resolved is None:
        print("FAILED: could not resolve this week's ATM contracts via AngelOne.")
        return 1
    print(f"  spot={spot}  atm={resolved['atm']}  expiry={resolved['expiry']}")
    print(f"  AngelOne symbols: CE={resolved['ce_symbol']}  PE={resolved['pe_symbol']}")

    mstock_ce = build_option_symbol(resolved["expiry"], resolved["atm"], "CE")
    mstock_pe = build_option_symbol(resolved["expiry"], resolved["atm"], "PE")
    print(f"  mStock DB-alias symbols: CE={mstock_ce}  PE={mstock_pe}\n")

    angel = MarketDataAdapter()
    if not angel.authenticate():
        print("WARNING: AngelOne session unavailable — can't cross-check, only showing mStock's own result.")
        angel = None

    mstock = MStockMarketData()
    print("Attempting mStock market-data login...")
    if not mstock.authenticate():
        print("FAILED to authenticate to mStock — check the logs above for the reason.")
        return 1
    print("Authenticated.\n")

    start = datetime.now() - timedelta(hours=2)
    end = datetime.now()

    for label, angel_sym, mstock_sym in (("CE", resolved["ce_symbol"], mstock_ce),
                                          ("PE", resolved["pe_symbol"], mstock_pe)):
        print(f"── {label} ──────────────────────────────────────────")
        print(f"mStock ({mstock_sym}):")
        m_df = mstock.fetch_historical_bars(mstock_sym, start, end, "5min")
        if m_df.empty:
            reason = m_df.attrs.get("error")
            print(f"  EMPTY{f' — error: {reason}' if reason else ' — genuinely no candle in this window, or resolution failed silently'}")
        else:
            print(f"  {len(m_df)} rows")
            print(m_df.tail(5).to_string(index=False))

        if angel is not None:
            print(f"\nAngelOne ({angel_sym}), same window:")
            a_df = angel.fetch_historical_bars(angel_sym, start, end, "5min")
            if a_df.empty:
                print("  EMPTY")
            else:
                print(f"  {len(a_df)} rows")
                print(a_df.tail(5).to_string(index=False))
        print()

    print("DONE. Compare the two tables above per each leg — see the module docstring for what to look for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
